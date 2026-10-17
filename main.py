import difflib
import inspect
import json
import os
import re
import subprocess
import sys

import ollama
import psutil

# Windows Volume Control Imports
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

from common import MODEL, stem

# Saved-skill execution lives in executor.py
from executor import list_skills, load_skill
from executor import run_skill as execute_skill

HERE = os.path.dirname(os.path.abspath(__file__))

# ==========================================
# 1. REAL WINDOWS TOOLS
# ==========================================

def set_volume(level: int) -> str:
    """Sets master Windows system volume to a percentage (0-100)."""
    try:
        m = re.search(r"\d+(?:\.\d+)?", str(level))   # the model may say "15%" or "15"
        if not m:
            return f"Failed to set volume: '{level}' is not a number."
        level = max(0, min(100, int(float(m.group()))))
        devices = AudioUtilities.GetSpeakers()
        if hasattr(devices, "EndpointVolume"):      # newer pycaw
            volume = devices.EndpointVolume
        else:                                       # older pycaw
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = interface.QueryInterface(IAudioEndpointVolume)
        volume.SetMasterVolumeLevelScalar(level / 100.0, None)
        return f"System volume set to {level}%."
    except Exception as e:
        return f"Failed to set volume: {str(e)}"


SEARCH_DEPTH = 4                                   # folders below Desktop/Documents/Downloads to look in
SKIP_DIRS = {".git", "node_modules", "__pycache__", "appdata", ".venv", "venv"}
BAD_NAME_CHARS = set('<>:"/\\|?*')
RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def bad_filename_reason(name):
    """Why `name` cannot be a plain file name (no folders, no Windows-illegal characters), or None."""
    if not name or not name.strip():
        return "it is empty"
    if any(c in BAD_NAME_CHARS or ord(c) < 32 for c in name):
        return "it contains a folder separator or another character Windows does not allow in file names"
    if name in {".", ".."} or name != name.rstrip(" ."):
        return "it ends with a space or a dot"
    if os.path.splitext(name)[0].lower() in RESERVED_NAMES:
        return "it is a reserved Windows device name"
    return None


def find_files(filename):
    """Case-insensitive search for an exact file name under Desktop, Documents and Downloads."""
    user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    wanted = filename.lower()
    hits = {}
    for sub in ("Desktop", "Documents", "Downloads"):
        root = os.path.join(user_profile, sub)
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.count(os.sep) - base_depth >= SEARCH_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
            for fn in filenames:
                if fn.lower() == wanted:
                    full = os.path.join(dirpath, fn)
                    hits.setdefault(os.path.normcase(os.path.realpath(full)), full)   # OneDrive can overlap folders
    return sorted(hits.values())


def find_and_rename_file(filename: str, new_name: str) -> str:
    """Searches Desktop, Documents, Downloads and renames the file (then verifies)."""
    for label, value in (("filename", filename), ("new name", new_name)):
        reason = bad_filename_reason(str(value or ""))
        if reason:
            return f"Error: the {label} '{value}' is not allowed: {reason}."

    matches = find_files(filename)
    if not matches:
        return f"Error: File '{filename}' not found in Desktop, Documents, or Downloads."
    if len(matches) > 1:
        listing = "; ".join(matches[:5])
        return (f"Error: Found {len(matches)} files named '{filename}' ({listing}). "
                "I need an exact match to avoid renaming the wrong one.")

    target_path = matches[0]
    folder = os.path.dirname(target_path)
    new_path = os.path.join(folder, new_name)
    case_only = os.path.normcase(new_path) == os.path.normcase(target_path)
    if os.path.exists(new_path) and not case_only:
        return f"Error: '{new_path}' already exists. Nothing was renamed."

    try:
        os.rename(target_path, new_path)
    except Exception as e:
        return f"Failed to rename file: {str(e)}"

    if new_name in os.listdir(folder) and (case_only or not os.path.exists(target_path)):
        return f"Success (verified): Renamed '{target_path}' -> '{new_path}'"
    return "Rename was attempted but could not be verified."


def get_battery() -> str:
    """Gets current battery percentage and charging status."""
    battery = psutil.sensors_battery()
    if battery is None:
        return "No battery detected (desktop PC or unmonitored system)."
    plugged = "Charging/Plugged in" if battery.power_plugged else "Discharging"
    return f"Battery is at {battery.percent}% ({plugged})."


def get_running_processes(search_name: str = "") -> str:
    """Lists running background processes, optionally filtered by name."""
    needle = (search_name or "").lower()
    matching = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = proc.info.get("name") or ""
            if needle in name.lower():
                matching.append(f"{name} (PID: {proc.info['pid']})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not matching:
        return f"No active processes found matching '{search_name}'."
    return f"Found {len(matching)} processes: " + ", ".join(matching[:5])


APP_ALLOWLIST = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "chrome": "chrome.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
}


def open_app(app_name: str) -> str:
    """Launches an app from the allowlist."""
    key = str(app_name).lower().strip()
    exe = APP_ALLOWLIST.get(key)
    if not exe:
        return (
            f"I don't know how to do that yet. (I tried to open an app called '{app_name}', "
            f"but the only apps I can open directly are: {', '.join(APP_ALLOWLIST)}.) "
            "You can teach me this task with:  /teach <what the task is>"
        )
    try:
        os.startfile(exe)
        return f"Application '{app_name}' launched."
    except Exception as e:
        return f"Failed to launch '{app_name}': {str(e)}"


def run_skill(skill_name: str = "", parameters: dict = None) -> str:
    """Runs a saved skill (recorded from the user's demonstration)."""
    if not isinstance(parameters, dict):
        parameters = {}
    names = [s["name"] for s in list_skills()]
    if skill_name not in names:
        close = difflib.get_close_matches(str(skill_name), names, n=1, cutoff=0.6)
        if not close:
            return f"Error: no saved skill called '{skill_name}'. Saved skills: {names}"
        print(f"(Using closest saved skill '{close[0]}' for '{skill_name}')")
        skill_name = close[0]
    return execute_skill(skill_name, parameters)


def unknown() -> str:
    """Used when no built-in tool or saved skill fits the request."""
    return (
        "I don't know how to do that yet, and I haven't learned a task like it. "
        "You can teach me with:  /teach <what the task is>"
    )


TOOL_REGISTRY = {
    "unknown": unknown,
    "set_volume": set_volume,
    "find_and_rename_file": find_and_rename_file,
    "get_battery": get_battery,
    "get_running_processes": get_running_processes,
    "open_app": open_app,
    "run_skill": run_skill,
}

# Tools that change something the user cannot easily undo: ask before running.
CONFIRM_TOOLS = {
    "find_and_rename_file": lambda a: f"Rename '{a.get('filename')}' to '{a.get('new_name')}'?",
}

# The router's answer is forced into this shape (the tool name can only be one of ours), so the
# "model answered with prose / markdown / a made-up tool" failures cannot happen.
ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": list(TOOL_REGISTRY)},
        "args": {
            "type": "object",
            "properties": {
                "level": {"type": "number"},
                "filename": {"type": "string"},
                "new_name": {"type": "string"},
                "search_name": {"type": "string"},
                "app_name": {"type": "string"},
                "skill_name": {"type": "string"},
                "parameters": {"type": "object"},
            },
        },
    },
    "required": ["tool", "args"],
}


def clean_args(fn, args):
    """Keep only the arguments the tool really takes (the model may add extra ones)."""
    allowed = inspect.signature(fn).parameters
    return {k: v for k, v in args.items() if k in allowed}


# ==========================================
# 2. SKILL SHORTLIST (keeps the small model's job small)
# ==========================================

STOPWORDS = {"the", "a", "an", "to", "in", "on", "of", "for", "my", "me", "please", "and", "is", "it"}


def words(text):
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOPWORDS:
            continue
        out.add(stem(w))
    return out


def shortlist_skills(user_input, limit=3):
    """Pick the saved skills most related to the request."""
    skills = [s for s in list_skills() if s.get("description")]
    if len(skills) <= limit:
        return skills
    req = words(user_input)

    def score(s):
        return len(req & words(s["name"].replace("_", " ") + " " + s["description"]))

    return sorted(skills, key=score, reverse=True)[:limit]


MATCH_MIN_WORDS = 2
MATCH_MIN_COVERAGE = 0.6


def match_skill(user_input):
    """Return the saved skill that clearly matches the request, or None."""
    req = words(user_input)
    if not req:
        return None
    scored = []
    for s in list_skills():
        if not s.get("description"):
            continue
        skill_words = words(s["name"].replace("_", " ") + " " + s["description"])
        overlap = len(req & skill_words)
        coverage = max(overlap / len(req), overlap / max(1, len(skill_words)))
        scored.append((overlap, coverage, s))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    overlap, coverage, best = scored[0]
    if overlap < MATCH_MIN_WORDS or coverage < MATCH_MIN_COVERAGE:
        return None
    if len(scored) > 1 and scored[1][0] == overlap:
        return None
    return best


def extract_parameters(skill, user_input):
    """Ask the model ONLY for the parameter values (forced JSON shape)."""
    data = load_skill(skill["name"]) or {}
    params = data.get("parameters") or {}
    if not params:
        return {}
    schema = {
        "type": "object",
        "properties": {p: {"type": "string"} for p in params},
        "required": list(params),
    }
    wanted = "; ".join(f"{p} (example: {spec.get('example', '')})" for p, spec in params.items())
    prompt = (
        f"Extract these values from the user's request: {wanted}.\n"
        f"Request: {user_input}\n"
        "If a value is not stated in the request, use an empty string."
    )
    try:
        r = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            format=schema,
            options={"temperature": 0},
        )
        got = json.loads(r["message"]["content"])
        return {k: v for k, v in got.items() if isinstance(v, str) and v.strip()}
    except Exception as e:
        print(f"(Could not extract parameters automatically: {e})")
        return {}


# ==========================================
# 3. SYSTEM PROMPT
# ==========================================

SYSTEM_PROMPT = """
You are a local Windows desktop assistant controller. Analyze the user's request and output a SINGLE JSON object representing the tool to call.

AVAILABLE TOOLS:
1. set_volume(level: number)
2. find_and_rename_file(filename: string, new_name: string)
3. get_battery()
4. get_running_processes(search_name: string)
5. open_app(app_name: string)
6. run_skill(skill_name: string, parameters: object)
7. unknown()   (use this when NO tool or saved skill fits the request)

RULES:
- Output ONLY valid JSON. No conversational text or markdown code blocks.
- If the request matches one of the SAVED SKILLS listed below, use run_skill with that skill's exact name.
- Otherwise use one of the other tools.
- If nothing fits, output {"tool": "unknown", "args": {}}. Do not guess a tool.

EXAMPLES:

User: "Turn volume to 15%"
{"tool": "set_volume", "args": {"level": 15}}

User: "How much battery is left?"
{"tool": "get_battery", "args": {}}

User: "Open Notepad for me"
{"tool": "open_app", "args": {"app_name": "notepad"}}

User: "Find resume.pdf and name it Final_Resume.pdf"
{"tool": "find_and_rename_file", "args": {"filename": "resume.pdf", "new_name": "Final_Resume.pdf"}}

User: "Book me a flight to Paris"
{"tool": "unknown", "args": {}}
"""


def build_system_prompt(user_input):
    skills = shortlist_skills(user_input)
    if not skills:
        return SYSTEM_PROMPT

    lines = []
    for s in skills:
        data = load_skill(s["name"]) or {}
        params = list((data.get("parameters") or {}).keys())
        extra = f" (parameters: {', '.join(params)})" if params else ""
        lines.append(f'- {s["name"]}: {s["description"]}{extra}')

    first = skills[0]
    example_request = first["description"].rstrip(".")
    example_json = json.dumps(
        {"tool": "run_skill", "args": {"skill_name": first["name"], "parameters": {}}}
    )
    return (
        SYSTEM_PROMPT
        + "\nSAVED SKILLS (use these exact names with run_skill):\n"
        + "\n".join(lines)
        + f'\n\nUser: "{example_request}"\n{example_json}\n'
    )


TOOL_KEYWORDS = {
    "find_and_rename_file": {"rename", "name", "call"},
    "set_volume": {"volume", "sound", "audio", "louder", "quieter", "mute", "unmute"},
    "get_battery": {"battery", "charge", "charging", "power"},
    "get_running_processes": {"running", "process", "processes", "task", "tasks"},
    "open_app": {"open", "launch", "start", "run"},
}


def tool_fits_request(tool, user_input):
    wanted = TOOL_KEYWORDS.get(tool)
    if not wanted:
        return True
    return bool(set(re.findall(r"[a-z0-9]+", user_input.lower())) & wanted)


# ==========================================
# 4. ROUTER
# ==========================================

def extract_text_value(clause, example):
    """Ask the model ONLY for the text to type in a learned action."""
    schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}
    prompt = (
        f"In an earlier demonstration the user typed: {example!r}.\n"
        f"Now the user says: {clause}\n"
        "Reply with only the text that should be typed this time. "
        "If the request does not say what to type, use an empty string."
    )
    try:
        r = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}],
                        format=schema, options={"temperature": 0})
        value = json.loads(r["message"]["content"]).get("value", "").strip()
        return value or None
    except Exception:
        return None


def parse_router_output(raw_content):
    """Model text -> dict, tolerating a markdown fence in case the server ignored `format`."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_content.strip(), flags=re.S).strip()
    return json.loads(text)


def process_user_request(user_input: str):
    print(f'\nUser Input: "{user_input}"')

    # Step 1: does the request clearly match a saved skill?
    skill = match_skill(user_input)
    if skill:
        print(f"Matched saved skill '{skill['name']}'")
        params = extract_parameters(skill, user_input)
        if params:
            print(f"Parameters: {params}")
        print(f"Result: {run_skill(skill['name'], params)}")
        return

    # Step 1b: requests about apps learned with /learn
    try:
        import planner
        handled = planner.handle_request(user_input, extract_value=extract_text_value,
                                         builtin=(set(APP_ALLOWLIST), open_app))
    except Exception as e:
        handled = f"Error while planning: {e}"
    if handled is not None:
        print(f"Result: {handled}")
        return

    # Step 2: otherwise let the model pick a tool
    try:
        response = ollama.chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": build_system_prompt(user_input)},
                {"role": "user", "content": user_input},
            ],
            format=ROUTER_SCHEMA,
            options={"temperature": 0.0},
        )
    except Exception as e:
        print(f"Error talking to Ollama: {e}")
        return

    raw_content = response["message"]["content"]
    try:
        data = parse_router_output(raw_content)
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON returned from LLM:\n{raw_content}")
        return
    if not isinstance(data, dict):
        print(f"Error: Unexpected LLM output: {raw_content}")
        return

    tool_name = data.get("tool")
    args = data.get("args", {})
    if not isinstance(args, dict):
        args = {}
    print(f"LLM Decision: '{tool_name}' with args {args}")

    if tool_name not in TOOL_REGISTRY:
        print(f"Error: Tool '{tool_name}' not recognized.")
        return
    if not tool_fits_request(tool_name, user_input):
        print(f"(The model chose '{tool_name}', but your request does not mention anything it does. Not running it.)")
        print(f"Result: {unknown()}")
        return

    fn = TOOL_REGISTRY[tool_name]
    args = clean_args(fn, args)

    if tool_name in CONFIRM_TOOLS:
        if input(f"{CONFIRM_TOOLS[tool_name](args)} [y/N]: ").strip().lower() != "y":
            print("Result: Cancelled.")
            return

    try:
        result = fn(**args)
    except TypeError as e:
        print(f"Error: the model gave wrong arguments for '{tool_name}': {e}")
        return
    except Exception as e:
        print(f"Error while running '{tool_name}': {e}")
        return
    print(f"Result: {result}")


# ==========================================
# 5. ENTRY POINT
# ==========================================

def self_test():
    print("=== Testing real Windows tools ===")
    process_user_request("Set the system volume to 20%")
    process_user_request("What is my current battery level?")
    process_user_request("Open Notepad")
    process_user_request("Is chrome running right now?")


def _run_recorder():
    recorder = os.path.join(HERE, "recorder.py")
    subprocess.run([sys.executable, recorder])


def teach_single_task(goal=""):
    """Teach ONE task (old flow): record once, then build a single replayable skill."""
    if not os.path.exists(os.path.join(HERE, "recorder.py")):
        print("Error: recorder.py must be in the same folder as main.py.")
        return
    print("\n=== Learning mode ===")
    print("1. Switch to your app and do the task ONCE, exactly as you normally would.")
    print("2. Press F8 when you are done. (F7 pauses recording, e.g. for a password.)")
    input("Press Enter here to start recording... ")
    _run_recorder()
    if not os.path.exists("raw_recording.json"):
        print("No recording was saved. Nothing learned.")
        return
    import skill_builder
    skill_builder.main(goal or None)
    print("\nDone. You can now ask for it in plain language.")


def learn_from_session():
    """Free-form learning: record everything you do; the assistant separates it by app on its own."""
    if not all(os.path.exists(os.path.join(HERE, f)) for f in ("recorder.py", "learner.py")):
        print("Error: recorder.py and learner.py must be in the same folder as main.py.")
        return
    print("\n=== Learning mode ===")
    print("Just use your computer normally: open apps, close them, click around, in any order.")
    print("You do not need to name anything. Press F8 when you are done.")
    print("(F7 pauses recording, e.g. while typing a password.)")
    input("Press Enter here to start recording... ")
    _run_recorder()
    if not os.path.exists("raw_recording.json"):
        print("No recording was saved. Nothing learned.")
        return
    import learner
    learner.learn_from_recording()


def learned_apps():
    folder = os.path.join("skills", "apps")
    out = []
    if os.path.isdir(folder):
        for fn in sorted(os.listdir(folder)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(folder, fn), "r", encoding="utf-8") as f:
                        d = json.load(f)
                    out.append((d.get("app", fn[:-5]), len(d.get("actions", [])), d.get("sessions", 1)))
                except Exception:
                    continue
    return out


def show_skills():
    skills = list_skills()
    apps = learned_apps()
    if not skills and not apps:
        print("Nothing learned yet. Teach me with:  /learn  (free-form session)  or  /teach <task>")
        return
    for s in skills:
        print(f"  - task: {s['name']} - {s['description']}")
    for name, n_actions, n_sessions in apps:
        print(f"  - app: {name} ({n_actions} action(s), seen in {n_sessions} session(s))")


HELP_TEXT = (
    "Commands:\n"
    "  /learn          free-form session: just use your PC, I work out the apps and actions\n"
    "  /teach <task>   teach me ONE named task by showing me once\n"
    "  /skills         list what I have learned\n"
    "  exit            quit\n"
    "Anything else is treated as a request."
)

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        self_test()
    else:
        print(f"Local assistant ready (model: {MODEL}).")
        print("What I have learned so far:")
        show_skills()
        print("\n" + HELP_TEXT)
        while True:
            try:
                text = input("\nYou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text.lower() in {"exit", "quit"}:
                break
            if not text:
                continue
            if text.startswith("/learn"):
                learn_from_session()
            elif text.startswith("/teach"):
                teach_single_task(text[len("/teach"):].strip())
            elif text.startswith("/skills"):
                show_skills()
            elif text.startswith("/help"):
                print(HELP_TEXT)
            else:
                process_user_request(text)
