"""
executor.py (v2)

Runs saved skills (./skills/*.json) and single learned steps using Windows UI Automation.

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

What a step can be:
    click_element / double_click_element / right_click_element / middle_click_element
    click_position (and double/right/middle)   - for apps with no accessibility info
    type_text      - supports {parameter} placeholders
    press_key      - "enter", "ctrl+shift+s", "win", ... with an optional "repeat"
    enter_password - asked for in the terminal, never stored
"""
import argparse
import contextlib
import getpass
import json
import os
import re
import threading
import time

import winapi  # noqa: F401  (sets DPI awareness before UI Automation is used)
import psutil
import uiautomation as auto
from pynput import keyboard, mouse

from common import describe_step, parse_steps, read_state

SKILLS_DIR = "skills"
STEP_TIMEOUT = 10.0      # seconds to wait for each element to appear
SETTLE_SECONDS = 0.4     # short pause after each step
TYPE_DELAY = 0.02        # pause between typed characters (some apps drop characters otherwise)
EXPAND_AFTER = 1.5       # only open drop-downs looking for a target after this many seconds
MAX_COMBOS = 5           # how many drop-downs to try opening while looking for a target
ABORT_KEY = keyboard.Key.f9

abort_event = threading.Event()
kb = keyboard.Controller()

MOD_KEYS = {
    "ctrl": keyboard.Key.ctrl,
    "alt": keyboard.Key.alt,
    "shift": keyboard.Key.shift,
    "win": keyboard.Key.cmd,
}

CLICKERS = {
    "click_element": "Click",
    "double_click_element": "DoubleClick",
    "right_click_element": "RightClick",
    "middle_click_element": "MiddleClick",
}

# Items that only exist in the UI tree while their drop-down / menu is open.
EXPAND_TYPES = {"ListItemControl", "MenuItemControl", "TextControl", "DataItemControl", "CustomControl"}

# AutomationIds that frameworks reuse for many unrelated elements. They are useless on their own.
GENERIC_IDS = {
    "ContentPresenter", "ContentRoot", "ContentGrid", "RootGrid", "Root", "LayoutRoot", "Grid", "Border",
    "TextBlock", "ItemsPresenter", "ScrollViewer", "ItemsRepeater", "PART_ContentPresenter",
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
    if not name or os.path.basename(str(name)) != str(name):
        return None  # a skill name is never a path
    path = os.path.join(SKILLS_DIR, f"{name}.json")
    if os.path.exists(path):
        return _read_json(path)
    for s in list_skills():  # fall back to matching the "name" field
        if s["name"] == name:
            return _read_json(os.path.join(SKILLS_DIR, s["file"]))
    return None


# ---------------------------------------------------------------------------
# Abort key
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def abort_listener():
    """While active, pressing F9 sets abort_event."""
    abort_event.clear()
    listener = keyboard.Listener(on_press=lambda k: abort_event.set() if k == ABORT_KEY else None)
    listener.start()
    try:
        yield
    finally:
        listener.stop()


# ---------------------------------------------------------------------------
# Finding elements
# ---------------------------------------------------------------------------
def candidate_windows(app, window_class):
    """Top-level windows that belong to the recorded app or have the recorded class (best matches first)."""
    both, either = [], []
    for w in auto.GetRootControl().GetChildren():
        try:
            by_class = bool(window_class) and w.ClassName == window_class
            by_app = bool(app) and psutil.Process(w.ProcessId).name().lower() == app.lower()
        except Exception:
            continue
        if by_class and by_app:
            both.append(w)
        elif by_class or by_app:
            either.append(w)
    return both + either


def is_generic_id(aid):
    return aid in GENERIC_IDS or aid.isdigit() or bool(re.fullmatch(r"[0-9a-fA-F-]{32,36}", aid))


def build_queries(target):
    """Search recipes, most specific first.

    A framework-generic AutomationId (e.g. 'ContentPresenter') is never used when a name is
    available, because dozens of elements share it. An exact name beats a substring match.
    """
    type_name = target.get("control_type") or ""
    ctype = getattr(auto.ControlType, type_name, None) if type_name else None
    name = (target.get("name") or "").strip()
    aid = (target.get("automation_id") or "").strip()

    def q(**kw):
        if ctype is not None:      # an unknown type must be left out, not passed as None
            kw["ControlType"] = ctype
        return kw

    queries = []
    if name and aid and not is_generic_id(aid):
        queries.append(q(Name=name, AutomationId=aid))
    if name:
        queries.append(q(Name=name))
        queries.append(q(SubName=name))
    if aid and (not name or not is_generic_id(aid)):
        queries.append(q(AutomationId=aid))
    return queries


def _is_visible(ctrl):
    try:
        if ctrl.IsOffscreen:
            return False
        r = ctrl.BoundingRectangle
        return r.width() > 0 and r.height() > 0
    except Exception:
        return False


def _search_once(step, queries):
    """One pass over the candidate windows. Returns (visible_match, hidden_match)."""
    parent_name = step["target"].get("parent_name") or ""
    hidden = None
    for win in candidate_windows(step.get("app"), step.get("window_class")):
        try:
            root = win
            if parent_name:
                parent = auto.Control(searchFromControl=win, SubName=parent_name)
                if not parent.Exists(0, 0):
                    continue
                root = parent
            for q in queries:
                ctrl = auto.Control(searchFromControl=root, **q)
                if not ctrl.Exists(0, 0):
                    continue
                if _is_visible(ctrl):
                    return ctrl, hidden
                if hidden is None:
                    hidden = ctrl
        except Exception:
            continue
    return None, hidden


def _try_reveal(step, queries):
    """The target may live inside a closed drop-down. Open the closed ones one at a time until it
    shows up; close each one again if it did not help. Returns the control or None."""
    for win in candidate_windows(step.get("app"), step.get("window_class")):
        for i in range(1, MAX_COMBOS + 1):
            try:
                combo = auto.ComboBoxControl(searchFromControl=win, foundIndex=i)
                if not combo.Exists(0, 0):
                    break
                if not _is_visible(combo):
                    continue
                p = combo.GetExpandCollapsePattern()
                if not (p and p.ExpandCollapseState == auto.ExpandCollapseState.Collapsed):
                    continue
                p.Expand()
                time.sleep(0.5)
                ctrl, _ = _search_once(step, queries)
                if ctrl is not None:
                    return ctrl
                try:
                    p.Collapse()
                except Exception:
                    pass
            except Exception:
                continue
    return None


def find_element(step, timeout=STEP_TIMEOUT, allow_hidden=True, expand=True):
    """Find a step's target. Waits up to `timeout` seconds.

    allow_hidden: if the element exists but has no visible area (zero-size, off-screen), return it
                  at the end anyway so a pattern (Invoke/Select/Toggle) can still activate it.
    expand:       after EXPAND_AFTER seconds, try opening closed drop-downs to reveal list items.
    """
    target = step["target"]
    queries = build_queries(target)
    if not queries:
        return None
    start = time.monotonic()
    deadline = start + timeout
    hidden, tried_reveal = None, False
    while True:
        if abort_event.is_set():
            raise Aborted()
        ctrl, h = _search_once(step, queries)
        if ctrl is not None:
            return ctrl
        if hidden is None:
            hidden = h
        if (expand and not tried_reveal and target.get("control_type") in EXPAND_TYPES
                and time.monotonic() - start >= EXPAND_AFTER):
            tried_reveal = True
            ctrl = _try_reveal(step, queries)
            if ctrl is not None:
                return ctrl
        if time.monotonic() >= deadline:
            break
        time.sleep(0.3)
    return hidden if allow_hidden else None


def target_visible(step, timeout=1.5):
    """Is the step's element on screen right now? (used by the planner to decide if prerequisites are needed)"""
    return find_element(step, timeout=timeout, allow_hidden=False, expand=False) is not None


def state_satisfied(pc):
    """Postcondition check: is the element already selected / toggled the way the recording ended up?

    Only 'selected' and 'toggle' are compared. 'expanded' is transient (menus close again), so a
    postcondition that only has that is never treated as already satisfied.
    """
    want = {k: v for k, v in (pc.get("state") or {}).items() if k in ("selected", "toggle")}
    if not want:
        return False
    step = {"target": pc["target"], "app": pc.get("app"), "window_class": pc.get("window_class", "")}
    ctrl = find_element(step, timeout=2.0, allow_hidden=True, expand=False)
    if ctrl is None:
        return False
    now = read_state(ctrl)
    return all(now.get(k) == v for k, v in want.items())


# ---------------------------------------------------------------------------
# Performing actions
# ---------------------------------------------------------------------------
def _activate_by_pattern(ctrl):
    """Activate a control without the mouse. Returns the name of what worked, or None."""
    def invoke():
        ctrl.GetInvokePattern().Invoke()

    def select():
        ctrl.GetSelectionItemPattern().Select()

    def toggle():
        ctrl.GetTogglePattern().Toggle()

    def expand_collapse():
        p = ctrl.GetExpandCollapsePattern()
        if p.ExpandCollapseState == auto.ExpandCollapseState.Collapsed:
            p.Expand()
        else:
            p.Collapse()

    for label, fn in (("Invoke", invoke), ("Select", select), ("Toggle", toggle), ("Expand", expand_collapse)):
        try:
            fn()
            return label
        except Exception:
            continue
    return None


def _scroll_into_view(ctrl):
    try:
        if ctrl.IsOffscreen:
            ctrl.GetScrollItemPattern().ScrollIntoView()
            time.sleep(0.3)
    except Exception:
        pass


def click_control(ctrl, action):
    """Click a found element.

    uiautomation's Click() only LOGS "Can not move cursor ... BoundingRectangle is (0,0,0,0)" and
    returns when an element has no area - it never raises - so an empty rectangle has to be checked
    up front. Without that a click would be reported as done when nothing happened.
    """
    _scroll_into_view(ctrl)
    if not _is_visible(ctrl):
        if action == "click_element":
            how = _activate_by_pattern(ctrl)
            if how:
                return f"activated without the mouse ({how})"
            raise StepError("the element has no visible area and does not support Invoke, Select, Toggle or Expand")
        raise StepError(f"{action} needs the element to be visible, but it has no visible area")
    time.sleep(0.3)  # let Windows UI animations settle
    try:
        getattr(ctrl, CLICKERS[action])(simulateMove=True)  # real mouse movement: WinUI ignores some synthetic clicks
    except Exception as exc:
        raise StepError(str(exc)) from exc
    return "clicked"


def click_position(step):
    """For apps with no accessibility info: click at the recorded spot inside the app's window."""
    rel = step.get("rel_in_window")
    if not rel or len(rel) != 2:
        raise StepError("this step has no recorded position")
    win, rect = None, None
    for w in candidate_windows(step.get("app"), step.get("window_class")):
        try:
            r = w.BoundingRectangle
            if r.width() > 0 and r.height() > 0:
                win, rect = w, r
                break
        except Exception:
            continue
    if win is None:
        raise StepError("could not find the window to click in")
    try:
        win.SetActive()
    except Exception:
        pass
    time.sleep(0.3)
    x = int(rect.left + rel[0] * rect.width())
    y = int(rect.top + rel[1] * rect.height())
    m = mouse.Controller()
    m.position = (x, y)
    time.sleep(0.15)
    verb = step["action"].split("_")[0]
    button = {"right": mouse.Button.right, "middle": mouse.Button.middle}.get(verb, mouse.Button.left)
    m.click(button, 2 if verb == "double" else 1)


def split_combo(combo):
    """'ctrl+shift+s' -> (['ctrl', 'shift'], 's').  'ctrl++' -> (['ctrl'], '+').  '+' -> ([], '+')."""
    combo = (combo or "").strip()
    if not combo:
        raise StepError("empty key")
    if combo == "+":
        return [], "+"
    if combo.endswith("++"):
        return [p.strip().lower() for p in combo[:-2].split("+") if p.strip()], "+"
    parts = [p.strip() for p in combo.split("+")]
    if "" in parts:
        raise StepError(f"bad key combination '{combo}'")
    return [p.lower() for p in parts[:-1]], parts[-1]


def to_key(name):
    low = name.lower()
    if low in MOD_KEYS:
        return MOD_KEYS[low]
    if low in keyboard.Key.__members__:
        return keyboard.Key[low]
    if re.fullmatch(r"vk\d+", low):          # the recorder writes keys it cannot name as vk<code>
        return keyboard.KeyCode.from_vk(int(low[2:]))
    if len(name) == 1:
        return name
    raise StepError(f"unknown key '{name}'")


def press_combo(combo):
    mod_names, main = split_combo(combo)
    mods = []
    for m in mod_names:
        if m not in MOD_KEYS:
            raise StepError(f"'{m}' is not a modifier key (in '{combo}')")
        mods.append(MOD_KEYS[m])
    main_key = to_key(main)
    held = []
    try:
        for m in mods:
            kb.press(m)
            held.append(m)
        kb.press(main_key)
        kb.release(main_key)
    finally:
        for m in reversed(held):      # never leave Ctrl/Alt/Win stuck down
            kb.release(m)


def type_text(text):
    for ch in text:
        if abort_event.is_set():
            raise Aborted()
        kb.type(ch)
        time.sleep(TYPE_DELAY)


def fill(text, params):
    for k, v in params.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def type_password():
    """Ask for the password in the terminal (hidden), then type it into the window that was active.

    Typing the password in the terminal steals keyboard focus, so the window that was in front
    before the prompt is brought back first - otherwise the password would be typed into the terminal.
    """
    fg = winapi.foreground_window()
    pw = getpass.getpass("Password needed for this step (typing is hidden): ")
    if fg:
        winapi.bring_to_front(fg)
        time.sleep(0.6)
    try:
        type_text(pw)
    finally:
        pw = None


def run_step(step, params):
    action = step["action"]

    if action in CLICKERS:
        ctrl = find_element(step)
        if ctrl is None:
            t = step["target"]
            label = t.get("name") or t.get("automation_id") or "(unnamed element)"
            raise StepError(f"could not find '{label}' in {step.get('app')} within {STEP_TIMEOUT:.0f}s")
        click_control(ctrl, action)

    elif action.endswith("_position"):
        click_position(step)

    elif action == "type_text":
        type_text(fill(step.get("text") or "", params))

    elif action == "press_key":
        for _ in range(int(step.get("repeat") or 1)):
            press_combo(step["key"])
            time.sleep(0.05)

    elif action == "enter_password":
        type_password()

    else:
        raise StepError(f"unknown step type '{action}'")

    time.sleep(SETTLE_SECONDS)


describe = describe_step


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

    n, step = 0, None
    with abort_listener():
        print("Starting in 3 seconds. Do not touch the mouse. Press F9 to abort.")
        time.sleep(3)
        try:
            for n, step in enumerate(steps, 1):
                if abort_event.is_set():
                    raise Aborted()
                print(f"  step {n}/{len(steps)}: {describe(step)}")
                run_step(step, params)
        except Aborted:
            return f"Aborted by user at step {n}."
        except StepError as exc:
            return f"FAILED at step {n} ({describe(step)}): {exc}. Stopped without running the remaining steps."
        except Exception as exc:
            return f"FAILED at step {n} ({describe(step)}): unexpected error: {exc}. Stopped."

    return (
        f"Finished: all {len(steps)} steps ran. "
        "(The final result is not checked automatically yet, so confirm it on screen.)"
    )


# ---------------------------------------------------------------------------
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
    try:
        only = parse_steps(args.steps) if args.steps else None
    except ValueError as exc:
        print(f"--steps: {exc}")
        return
    print(run_skill(args.skill, params, only_steps=only, confirm=not args.yes))


if __name__ == "__main__":
    main()
