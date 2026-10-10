"""
executor.py (v1)

Runs a saved skill from ./skills/ using Windows UI Automation.

Usage (terminal):
    python executor.py --list
    python executor.py open_home_files
    python executor.py open_home_files --steps 1,4        (run only some steps: good for testing)
    python executor.py search_web --param query="flutter"  (fill in parameters)

Safety:
  * Shows the steps and asks for confirmation (unless --yes).
  * Waits up to 10 s for each target element. If it is not found, it STOPS.
  * Press F9 at any time to abort.
  * Passwords are never stored: 'enter_password' steps ask you at run time.

Can also be imported:  from executor import run_skill, list_skills
"""
import argparse
import ctypes
import getpass
import json
import os
import threading
import time

import psutil
import uiautomation as auto
from pynput import keyboard

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

SKILLS_DIR = "skills"
STEP_TIMEOUT = 10.0      # seconds to wait for each element to appear
SETTLE_SECONDS = 0.4     # short pause after each step
ABORT_KEY = keyboard.Key.f9

abort_event = threading.Event()
kb = keyboard.Controller()

MOD_KEYS = {
    "ctrl": keyboard.Key.ctrl,
    "alt": keyboard.Key.alt,
    "shift": keyboard.Key.shift,
    "win": keyboard.Key.cmd,
}


class StepError(Exception):
    pass


class Aborted(Exception):
    pass


# ---------------------------------------------------------------------------
# Skill files
# ---------------------------------------------------------------------------
def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_skills():
    """Return [{'name':..., 'description':...}, ...] for every saved skill."""
    out = []
    if not os.path.isdir(SKILLS_DIR):
        return out
    for fn in sorted(os.listdir(SKILLS_DIR)):
        if fn.endswith(".json"):
            try:
                data = _read_json(os.path.join(SKILLS_DIR, fn))
                out.append(
                    {
                        "name": data.get("name", fn[:-5]),
                        "description": data.get("description", ""),
                        "file": fn,
                    }
                )
            except Exception:
                continue
    return out


def load_skill(name):
    path = os.path.join(SKILLS_DIR, f"{name}.json")
    if os.path.exists(path):
        return _read_json(path)
    for s in list_skills():  # fall back to matching the "name" field
        if s["name"] == name:
            return _read_json(os.path.join(SKILLS_DIR, s["file"]))
    return None


# ---------------------------------------------------------------------------
# Finding elements
# ---------------------------------------------------------------------------
def candidate_windows(app, window_class):
    """Top-level windows that belong to the recorded app or have the recorded class."""
    wins = []
    for w in auto.GetRootControl().GetChildren():
        try:
            if window_class and w.ClassName == window_class:
                wins.append(w)
                continue
            if app and psutil.Process(w.ProcessId).name().lower() == app.lower():
                wins.append(w)
        except Exception:
            continue
    return wins


def build_queries(target):
    """Search recipes, most reliable first."""
    ctype = getattr(auto.ControlType, target.get("control_type", ""), None)
    queries = []
    if target.get("automation_id"):
        queries.append({"AutomationId": target["automation_id"]})
    if target.get("name"):
        queries.append({"SubName": target["name"], "ControlType": ctype})
    return queries


def find_element(step, timeout=STEP_TIMEOUT):
    queries = build_queries(step["target"])
    if not queries:
        return None  # nothing stable to search for
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if abort_event.is_set():
            raise Aborted()
        parent_name = step["target"].get("parent_name")
        for win in candidate_windows(step.get("app"), step.get("window_class")):
            root = win
            if parent_name:   # e.g. the 'Picture' inside the item called '20261009_121612.jpg'
                parent = auto.Control(searchFromControl=win, SubName=parent_name)
                if not parent.Exists(0, 0):
                    continue
                root = parent
            for q in queries:
                ctrl = auto.Control(searchFromControl=root, **q)
                if ctrl.Exists(0, 0):
                    return ctrl
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# Performing actions
# ---------------------------------------------------------------------------
def to_key(name):
    if name in MOD_KEYS:
        return MOD_KEYS[name]
    if name in keyboard.Key.__members__:
        return keyboard.Key[name]
    if len(name) == 1:
        return name
    raise StepError(f"unknown key '{name}'")


def press_combo(combo):
    parts = combo.split("+")
    mods = [MOD_KEYS[p] for p in parts[:-1] if p in MOD_KEYS]
    main_key = to_key(parts[-1])
    for m in mods:
        kb.press(m)
    kb.press(main_key)
    kb.release(main_key)
    for m in reversed(mods):
        kb.release(m)


def fill(text, params):
    for k, v in params.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def run_step(step, params):
    action = step["action"]

    if action.endswith("click_element"):
        ctrl = find_element(step)
        if ctrl is None:
            t = step["target"]
            label = t.get("name") or t.get("automation_id") or "(unnamed element)"
            raise StepError(f"could not find '{label}' in {step.get('app')} within {STEP_TIMEOUT:.0f}s")
        if action == "double_click_element":
            ctrl.DoubleClick()
        elif action == "right_click_element":
            ctrl.RightClick()
        elif action == "middle_click_element":
            ctrl.MiddleClick()
        else:
            ctrl.Click()

    elif action == "type_text":
        kb.type(fill(step["text"], params))

    elif action == "press_key":
        for _ in range(int(step.get("repeat", 1))):
            press_combo(step["key"])
            time.sleep(0.05)

    elif action == "enter_password":
        pw = getpass.getpass("Password for this step (not saved anywhere): ")
        kb.type(pw)

    else:
        raise StepError(f"unknown action '{action}'")

    time.sleep(SETTLE_SECONDS)


def describe(step):
    a = step["action"]
    if a.endswith("click_element"):
        t = step["target"]
        return f"{a} '{t.get('name') or t.get('automation_id')}' in {step.get('app')}"
    if a == "type_text":
        return f"type {step['text']!r}"
    if a == "press_key":
        times = f" x{step['repeat']}" if step.get("repeat") else ""
        return f"press {step['key']}{times}"
    return a


# ---------------------------------------------------------------------------
def collect_params(skill, given):
    params = dict(given)
    for pname, spec in skill.get("parameters", {}).items():
        if pname not in params:
            hint = spec.get("example", "")
            params[pname] = input(f"Value for '{pname}' (example: {hint}): ")
    return params


def run_skill(skill_name, parameters=None, only_steps=None, confirm=True):
    """Run a saved skill. Returns a result string (also usable as an LLM tool)."""
    skill = load_skill(skill_name)
    if not skill:
        return f"Error: skill '{skill_name}' not found. Saved skills: {[s['name'] for s in list_skills()]}"

    steps = skill.get("steps", [])
    if only_steps:
        steps = [s for i, s in enumerate(steps, 1) if i in only_steps]
    if not steps:
        return "Error: the skill has no steps to run."

    params = collect_params(skill, parameters or {})

    print(f"\nSkill: {skill.get('name')} - {skill.get('description', '')}")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {describe(s)}")
    if confirm and input("Run these steps? [y/N]: ").strip().lower() != "y":
        return "Cancelled by user."

    abort_event.clear()
    listener = keyboard.Listener(
        on_press=lambda k: abort_event.set() if k == ABORT_KEY else None
    )
    listener.start()
    print("Starting in 3 seconds. Do not touch the mouse. Press F9 to abort.")
    time.sleep(3)

    try:
        for n, step in enumerate(steps, 1):
            if abort_event.is_set():
                raise Aborted()
            print(f"  step {n}/{len(steps)}: {describe(step)}")
            run_step(step, params)
    except Aborted:
        return f"Aborted by user before step {n}."
    except StepError as exc:
        return f"FAILED at step {n} ({describe(step)}): {exc}. Stopped without running the remaining steps."
    except Exception as exc:
        return f"FAILED at step {n} ({describe(step)}): unexpected error: {exc}. Stopped."
    finally:
        listener.stop()

    return (
        f"Finished: all {len(steps)} steps ran and every target was found. "
        "(The final result is not checked automatically yet, so confirm it on screen.)"
    )


# ---------------------------------------------------------------------------
def parse_steps(text):
    chosen = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            chosen.update(range(int(a), int(b) + 1))
        else:
            chosen.add(int(part))
    return chosen


def main():
    ap = argparse.ArgumentParser(description="Run a saved skill.")
    ap.add_argument("skill", nargs="?", help="skill name (file name without .json)")
    ap.add_argument("--list", action="store_true", help="list saved skills")
    ap.add_argument("--param", action="append", default=[], help="name=value (repeatable)")
    ap.add_argument("--steps", help="run only these step numbers, e.g. 1,4 or 1-3")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation question")
    args = ap.parse_args()

    if args.list or not args.skill:
        for s in list_skills():
            print(f"- {s['name']}: {s['description']}")
        return

    params = {}
    for p in args.param:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip()] = v
    only = parse_steps(args.steps) if args.steps else None
    print(run_skill(args.skill, params, only_steps=only, confirm=not args.yes))


if __name__ == "__main__":
    main()