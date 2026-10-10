import difflib
import glob
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

# Saved-skill execution lives in executor.py
from executor import list_skills, load_skill
from executor import run_skill as execute_skill

MODEL = "llama3.2:1b"  # change here if you switch models

# ==========================================
# 1. REAL WINDOWS TOOLS
# ==========================================

def set_volume(level: int) -> str:
    """Sets master Windows system volume to a percentage (0-100)."""
    try:
        level = max(0, min(100, int(level)))
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


def find_and_rename_file(filename: str, new_name: str) -> str:
    """Searches Desktop, Documents, Downloads and renames the file (then verifies)."""
    user_profile = os.environ.get("USERPROFILE", "C:\\Users\\Default")
    search_dirs = [
        os.path.join(user_profile, "Desktop"),
        os.path.join(user_profile, "Documents"),
        os.path.join(user_profile, "Downloads"),
    ]

    target_path = None
    for folder in search_dirs:
        matches = glob.glob(os.path.join(folder, "**", filename), recursive=True)
        if matches:
            target_path = matches[0]
            break

    if not target_path:
        return f"Error: File '{filename}' not found in Desktop, Documents, or Downloads."

    new_path = os.path.join(os.path.dirname(target_path), new_name)
    if os.path.exists(new_path):
        return f"Error: '{new_path}' already exists. Nothing was renamed."

    try:
        os.rename(target_path, new_path)
    except Exception as e:
        return f"Failed to rename file: {str(e)}"

    # Verification: did it really happen?
    if os.path.exists(new_path) and not os.path.exists(target_path):
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
    matching = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = proc.info["name"]
            if search_name.lower() in name.lower():
                matching.append(f"{name} (PID: {proc.info['pid']})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not matching:
        return f"No active processes found matching '{search_name}'."
    return f"Found {len(matching)} processes: " + ", ".join(matching[:5])


# Only these apps can be launched by the AI (no free-form commands, no shell)
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
        os.startfile(exe)  # no shell command line is built, so nothing can be injected
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

# ==========================================
# 2. SKILL SHORTLIST (keeps the 1B model's job small)
# ==========================================

STOPWORDS = {"the", "a", "an", "to", "in", "on", "of", "for", "my", "me", "please", "and", "is", "it"}


def words(text):
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOPWORDS:
            continue
        out.add(stem(w))
    return out


def stem(w):
    """Very small plural-stripper: searches->search, files->file, tutorials->tutorial."""
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def shortlist_skills(user_input, limit=3):
    """Pick the saved skills most related to the request."""
    skills = [s for s in list_skills() if s.get("description")]
    if len(skills) <= limit:
        return skills
    req = words(user_input)

    def score(s):
        return len(req & words(s["name"].replace("_", " ") + " " + s["description"]))

    return sorted(skills, key=score, reverse=True)[:limit]


MATCH_MIN_WORDS = 2        # at least this many shared words
MATCH_MIN_COVERAGE = 0.6   # and this share of the request's OR the skill's words


def match_skill(user_input):
    """Return the saved skill that clearly matches the request, or None.
    Plain word matching: the 1B model is unreliable at choosing between a
    generic tool and a saved skill, so code decides when the match is clear."""
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
        return None  # ambiguous: let the LLM choose from the shortlist
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


# A tool may only run if the request actually mentions what it does. This stops the small
# model from sending e.g. "open photos" to the file-rename tool or "change wallpaper" to volume.
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
    """Ask the model ONLY for the text to type in a learned action (e.g. what to calculate or search)."""
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


def process_user_request(user_input: str):
    print(f'\nUser Input: "{user_input}"')

    # Step 1: does the request clearly match a saved skill? (decided by code, not the model)
    skill = match_skill(user_input)
    if skill:
        print(f"Matched saved skill '{skill['name']}'")
        params = extract_parameters(skill, user_input)
        if params:
            print(f"Parameters: {params}")
        print(f"Result: {run_skill(skill['name'], params)}")
        return

    # Step 1b: requests about apps learned with /learn (open/close/minimize them, run learned actions)
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
            format="json",
            options={"temperature": 0.0},
        )
    except Exception as e:
        print(f"Error talking to Ollama: {e}")
        return

    raw_content = response["message"]["content"]

    try:
        data = json.loads(raw_content)
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

    try:
        result = TOOL_REGISTRY[tool_name](**args)
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


def teach_single_task(goal=""):
    """Teach ONE task (old flow): record once, then build a single replayable skill."""
    if not os.path.exists("recorder.py"):
        print("Error: recorder.py must be in the same folder as main.py.")
        return
    print("\n=== Learning mode ===")
    print("1. Switch to your app and do the task ONCE, exactly as you normally would.")
    print("2. Press F8 when you are done. (F7 pauses recording, e.g. for a password.)")
    input("Press Enter here to start recording... ")
    subprocess.run([sys.executable, "recorder.py"])
    if not os.path.exists("raw_recording.json"):
        print("No recording was saved. Nothing learned.")
        return
    import skill_builder
    skill_builder.main(goal or None)
    print("\nDone. You can now ask for it in plain language.")


def learn_from_session():
    """Free-form learning: record everything you do; the assistant separates it by app on its own."""
    if not os.path.exists("recorder.py") or not os.path.exists("learner.py"):
        print("Error: recorder.py and learner.py must be in the same folder as main.py.")
        return
    print("\n=== Learning mode ===")
    print("Just use your computer normally: open apps, close them, click around, in any order.")
    print("You do not need to name anything. Press F8 when you are done.")
    print("(F7 pauses recording, e.g. while typing a password.)")
    input("Press Enter here to start recording... ")
    subprocess.run([sys.executable, "recorder.py"])
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