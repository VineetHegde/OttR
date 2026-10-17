"""
skill_builder.py (v3)

Turns raw_recording.json into a reusable skill file in ./skills/  (the /teach flow).

The steps are built by plain code (learner.build_steps, the same builder /learn uses, so unnamed
elements, position clicks and passwords are handled the same way). The local LLM is only asked for
a short name and description, using a JSON schema (no template to copy).
"""
import json
import os
from collections import Counter
from datetime import datetime

import ollama

import learner
from common import MODEL, describe_step, parse_steps, save_json, slug

RAW_FILE = "raw_recording.json"
SKILLS_DIR = "skills"

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
        try:
            picked = parse_steps(raw)
        except ValueError as exc:
            print(f"  {exc}")
            continue
        if not picked or not all(1 <= n <= len(apps) for n in picked):
            print("  Please type numbers from the list above, like 2,3.")
            continue
        keep = {apps[n - 1] for n in picked}
        return [e for e in events if e.get("app", "unknown") in keep]


def build_steps(events):
    steps = learner.build_steps(events)
    for s in steps:
        s.pop("parameterizable", None)   # only meaningful for /learn profiles
    return steps


def remove_steps(steps):
    """Let the user delete steps that were not part of the task."""
    while True:
        raw = input("\nRemove any steps? Type numbers like 5-8,12 - or just press Enter to keep all: ").strip()
        if not raw:
            return steps
        try:
            drop = parse_steps(raw)
        except ValueError as exc:
            print(f"  {exc}")
            continue
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
def suggest_name(intent, steps):
    summary = "\n".join(f"- {describe_step(s, detailed=True)}" for s in steps)
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
    print("=== Skill Builder v3 ===")
    raw = load_raw()
    if not raw:
        print("Nothing usable in the recording.")
        return

    events = choose_apps(raw)
    steps = build_steps(events)
    if not steps:
        print("No usable steps found. Try a cleaner recording.")
        return

    print(f"\nBuilt {len(steps)} steps:")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {describe_step(s, detailed=True)}")

    trimmed = remove_steps(steps)
    if len(trimmed) != len(steps):
        steps = trimmed
        if not steps:
            print("No steps left. Cancelled.")
            return
        print(f"\nNow {len(steps)} steps:")
        for i, s in enumerate(steps, 1):
            print(f"  {i}. {describe_step(s, detailed=True)}")

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

    path = os.path.join(SKILLS_DIR, f"{name}.json")
    if os.path.exists(path):
        if input(f"'{path}' exists. Overwrite? [y/N]: ").strip().lower() != "y":
            new_name = slug(input("Save under a different name (Enter = cancel): ").strip())
            if not new_name:
                print("Cancelled. The raw recording was kept.")
                return
            skill["name"] = new_name
            path = os.path.join(SKILLS_DIR, f"{new_name}.json")
    save_json(path, skill)
    print(f"\nSaved skill to {path}")

    if input("Delete the raw recording now? [Y/n]: ").strip().lower() != "n":
        try:
            os.remove(RAW_FILE)
            print("Raw recording deleted.")
        except OSError:
            pass


if __name__ == "__main__":
    main()
