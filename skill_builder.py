"""
skill_builder.py (v2)

Turns raw_recording.json into a reusable skill file in ./skills/.

The steps are built by plain code (reliable). The local LLM is only asked for a
short name and description, using a JSON schema (no template to copy).
"""
import json
import os
import re
from collections import Counter
from datetime import datetime

import ollama

MODEL = "llama3.2:1b"  # change here if you switch models
RAW_FILE = "raw_recording.json"
SKILLS_DIR = "skills"
DOUBLE_CLICK_SECONDS = 0.6

NAME_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["name", "description"],
}


# ---------------------------------------------------------------------------
def load_raw():
    try:
        with open(RAW_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [e for e in data if e.get("action") in ("click", "type_text", "press_key")]
    except FileNotFoundError:
        print(f"'{RAW_FILE}' not found. Run recorder.py first.")
        return None


def clean_label(name):
    """Remove changing suffixes such as ' - 1 running window pinned'."""
    return re.sub(r"\s*-\s*\d+\s+running\s+windows?.*$", "", name or "").strip()


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40]


# ---------------------------------------------------------------------------
def choose_apps(events):
    """Let the user keep only the app(s) that belong to the task."""
    counts = Counter(e.get("app", "unknown") for e in events)
    apps = list(counts)
    print("\nApps seen in the recording:")
    for i, app in enumerate(apps, 1):
        print(f"  {i}. {app}  ({counts[app]} events)")
    while True:
        raw = input("Keep which? Type numbers like 2,3 - or just press Enter to keep all: ").strip()
        if not raw:
            return events
        keep = set()
        valid = True
        for part in raw.split(","):
            try:
                n = int(part.strip())
            except ValueError:
                valid = False
                break
            if not 1 <= n <= len(apps):
                valid = False
                break
            keep.add(apps[n - 1])
        if not valid:
            print("  Please type numbers from the list above, like 2,3.")
            continue
        return [e for e in events if e.get("app", "unknown") in keep]


def build_steps(events):
    steps = []
    last_click_t = 0.0
    for e in events:
        action = e["action"]

        if action == "click":
            t = e["target"]
            if not t.get("name", "").strip() and not t.get("automation_id", "").strip():
                continue  # unnamed element: cannot be found again reliably
            key = (t["name"], t["control_type"], t["automation_id"], e["app"])
            prev = steps[-1] if steps else None
            if (
                prev
                and prev.get("_key") == key
                and e["button"] == "left"
                and e["t"] - last_click_t <= DOUBLE_CLICK_SECONDS
            ):
                prev["action"] = "double_click_element"
                last_click_t = e["t"]
                continue
            step_action = {
                "left": "click_element",
                "right": "right_click_element",
                "middle": "middle_click_element",
            }.get(e["button"], "click_element")
            steps.append(
                {
                    "action": step_action,
                    "target": {
                        "name": clean_label(t["name"]),
                        "control_type": t["control_type"],
                        "automation_id": t["automation_id"],
                        "class_name": t.get("class_name", ""),
                    },
                    "app": e["app"],
                    "window_class": e.get("window_class", ""),
                    "fallback_rel_pos": t.get("rel_pos"),
                    "_key": key,
                }
            )
            last_click_t = e["t"]

        elif action == "type_text":
            if e.get("sensitive"):
                steps.append({"action": "enter_password", "source": "user_input"})
            else:
                steps.append({"action": "type_text", "text": e["text"]})

        elif action == "press_key":
            steps.append({"action": "press_key", "key": e["key"]})

    for s in steps:
        s.pop("_key", None)
    return collapse_repeats(steps)


def collapse_repeats(steps):
    """Merge consecutive identical key presses into one step with a repeat count."""
    out = []
    for s in steps:
        if (
            out
            and s["action"] == "press_key"
            and out[-1]["action"] == "press_key"
            and out[-1]["key"] == s["key"]
        ):
            out[-1]["repeat"] = out[-1].get("repeat", 1) + 1
        else:
            out.append(s)
    return out


def remove_steps(steps):
    """Let the user delete steps that were not part of the task."""
    raw = input("\nRemove any steps? Type numbers like 5-8,12 - or just press Enter to keep all: ").strip()
    if not raw:
        return steps
    drop = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                drop.update(range(int(a), int(b) + 1))
            else:
                drop.add(int(part))
        except ValueError:
            print(f"  Ignored '{part}'.")
    return [s for i, s in enumerate(steps, 1) if i not in drop]


def ask_parameters(steps):
    """The user decides which typed values change each time."""
    typed = [s for s in steps if s["action"] == "type_text"]
    params = {}
    if not typed:
        return params
    print("\nText typed during the demo:")
    for n, s in enumerate(typed, 1):
        print(f"  {n}. {s['text']!r}")
    raw = input("Which typed values change each time? Type like 1=filename,2=query - or just press Enter for none: ").strip()
    for part in raw.split(","):
        if "=" not in part:
            continue
        num, pname = part.split("=", 1)
        try:
            idx = int(num.strip()) - 1
        except ValueError:
            continue
        pname = slug(pname)
        if 0 <= idx < len(typed) and pname:
            step = typed[idx]
            params[pname] = {"type": "string", "required": True, "example": step["text"]}
            step["text"] = "{" + pname + "}"
    return params


# ---------------------------------------------------------------------------
def summarize_step(s):
    a = s["action"]
    if a.endswith("click_element"):
        t = s["target"]
        return f"{a} '{t['name']}' ({t['control_type']}) in {s['app']}"
    if a == "type_text":
        return f"type text {s['text']!r}"
    if a == "press_key":
        times = f" x{s['repeat']}" if s.get("repeat") else ""
        return f"press key {s['key']}{times}"
    return a


def suggest_name(intent, steps):
    summary = "\n".join(f"- {summarize_step(s)}" for s in steps)
    prompt = (
        "Give this recorded automation a short snake_case name (2 to 4 words) "
        "and a one-sentence description.\n"
        f"The user's goal: {intent}\n"
        f"Steps:\n{summary}"
    )
    name, desc = "", ""
    try:
        r = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            format=NAME_SCHEMA,
            options={"temperature": 0},
        )
        data = json.loads(r["message"]["content"])
        name = slug(data.get("name", ""))
        desc = str(data.get("description", "")).strip()
    except Exception as exc:
        print(f"(LLM naming failed: {exc}. Using your goal text instead.)")
    return name or slug(intent) or "new_skill", desc or intent


# ---------------------------------------------------------------------------
def main(intent=None):
    print("=== Skill Builder v2 ===")
    raw = load_raw()
    if not raw:
        return

    events = choose_apps(raw)
    steps = build_steps(events)
    if not steps:
        print("No usable steps found. Try a cleaner recording.")
        return

    print(f"\nBuilt {len(steps)} steps:")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {summarize_step(s)}")

    trimmed = remove_steps(steps)
    if len(trimmed) != len(steps):
        steps = trimmed
        if not steps:
            print("No steps left. Cancelled.")
            return
        print(f"\nNow {len(steps)} steps:")
        for i, s in enumerate(steps, 1):
            print(f"  {i}. {summarize_step(s)}")

    intent = (intent or input("\nWhat is the goal of this demonstration? ")).strip()
    params = ask_parameters(steps)

    print(f"\nAsking {MODEL} for a name and description...")
    name, description = suggest_name(intent, steps)
    chosen = input(f"Skill name [{name}] (Enter to accept, or type another): ").strip()
    if chosen:
        name = slug(chosen) or name

    skill = {
        "name": name,
        "description": description,
        "parameters": params,
        "steps": steps,
        "created": datetime.now().isoformat(timespec="seconds"),
    }

    os.makedirs(SKILLS_DIR, exist_ok=True)
    path = os.path.join(SKILLS_DIR, f"{name}.json")
    if os.path.exists(path):
        if input(f"'{path}' exists. Overwrite? [y/N]: ").strip().lower() != "y":
            new_name = slug(input("Save under a different name (Enter = cancel): ").strip())
            if not new_name:
                print("Cancelled. The raw recording was kept.")
                return
            skill["name"] = new_name
            path = os.path.join(SKILLS_DIR, f"{new_name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(skill, f, indent=2, ensure_ascii=False)
    print(f"\nSaved skill to {path}")

    if input("Delete the raw recording now? [Y/n]: ").strip().lower() != "n":
        os.remove(RAW_FILE)
        print("Raw recording deleted.")


if __name__ == "__main__":
    main()