"""
planner.py - plain-language requests -> actions, using what /learn recorded (skills/apps/*.json).

It can:
  * open / close / minimize / restore any app it has learned (works for any program with a window)
  * run an action learned inside an app, skipping it when its end state already holds
    (e.g. "Select Home" is skipped if Home is already selected)
  * handle several tasks in one request ("open chrome and settings")
  * open an app it has never seen opened: direct program path, or the clicks you used,
    or the universal fallback of searching the Start menu

Anything it does not understand is SAID, not guessed.
The planning part is plain Python (testable anywhere); the Windows parts load lazily.
"""
import copy
import json
import os
import re
import time

PROFILES_DIR = os.path.join("skills", "apps")


def stem(w):
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


PUNCT = r"(?<=[A-Za-z])[.'\u2019](?=[A-Za-z])"    # "f.lux" -> "flux"


def tokenize(text):
    text = re.sub(PUNCT, "", text or "")
    return {stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())}


def norm(text):
    return "".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


VERBS = {
    "open": tokenize("open launch start run show go switch bring"),
    "close": tokenize("close quit exit kill shut"),
    "minimize": tokenize("minimize minimise hide"),
    "restore": tokenize("restore maximize maximise unminimize"),
}
ALL_VERBS = set().union(*VERBS.values())
FUNCTION_WORDS = tokenize(
    "please can could you would will i want need to the a an my me in on of for under inside also now it "
    "this that up with at into do is are be just from some any something"
)
UI_WORDS = tokenize("app application window folder page tab screen menu section option button")
FILLER = FUNCTION_WORDS | UI_WORDS
ON_WORDS = tokenize("on enable activate")
OFF_WORDS = tokenize("off disable deactivate")
SOFT_VERBS = tokenize("change set choose select click turn make use pick apply")
OPTION_TYPES = {"ListItemControl", "MenuItemControl", "RadioButtonControl"}


def display(profile):
    return (profile.get("search_name") or os.path.splitext(profile["app"])[0]).title()


def load_profiles(folder=PROFILES_DIR):
    out = []
    if os.path.isdir(folder):
        for fn in sorted(os.listdir(folder)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(folder, fn), "r", encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("app"):
                        d.setdefault("aliases", [])
                        d.setdefault("actions", [])
                        out.append(d)
                except Exception:
                    continue
    return out


def action_text(a):
    parts = [a["name"]]
    for s in a.get("steps", []):
        t = s.get("target") or {}
        parts += [t.get("name", ""), t.get("parent_name", ""),
                  re.sub(r"([a-z])([A-Z])", r"\1 \2", t.get("automation_id", ""))]
    return " ".join(parts)


def action_tokens(a):
    return tokenize(action_text(a)) - FUNCTION_WORDS


def find_app(tokens, profiles):
    best, best_score = None, 0
    for p in profiles:
        for alias in p["aliases"] + [os.path.splitext(p["app"])[0]]:
            at = tokenize(alias)
            if at and at <= tokens and len(at) > best_score:
                best, best_score = p, len(at)
    return best


def ordered_words(clause, tokens):
    """The words of the clause that are in `tokens`, in the order the user said them."""
    out = []
    clause = re.sub(r"(?<=[A-Za-z])[.'\u2019](?=[A-Za-z])", "", clause)
    for w in re.findall(r"[a-z0-9]+", clause.lower()):
        if stem(w) in tokens and w not in out:
            out.append(w)
    return " ".join(out)


def option_groups(profile):
    """Items picked from the same kind of list (e.g. 'Dark' and 'Light'): {(type, class): {names}}."""
    groups = {}
    for a in profile["actions"]:
        for s in a.get("steps", []):
            t = s.get("target") or {}
            if s["action"].endswith("_element") and t.get("control_type") in OPTION_TYPES and t.get("name"):
                groups.setdefault((t["control_type"], t.get("class_name", "")), set()).add(t["name"])
    return {k: v for k, v in groups.items() if len(v) >= 2}


def final_target(action):
    steps = action.get("steps", [])
    if steps and steps[-1]["action"].endswith("_element"):
        return steps[-1].get("target") or {}
    return {}


def rank_actions(profile, leftover):
    """Score every learned action against the request. Returns [(score, action, choice)], best first.
    An action that ends by picking one item from a list can be re-aimed at a sibling item
    (you showed 'Dark'; the request says 'Light') as long as that sibling was seen somewhere."""
    groups = option_groups(profile)
    ranked = []
    for a in profile["actions"]:
        own = action_tokens(a)
        score = 2 * len(leftover & own)
        choice = None
        t = final_target(a)
        opts = groups.get((t.get("control_type"), t.get("class_name", ""))) if t else None
        if opts:
            rest = leftover - own
            best_o, best_n = None, 0
            for o in opts:
                ot = tokenize(o) - FILLER
                if o != t.get("name") and ot and ot <= rest and len(ot) > best_n:
                    best_o, best_n = o, len(ot)
            if best_o:
                score += best_n
                choice = best_o
        if score > 0:
            ranked.append((score, a, choice))
    ranked.sort(key=lambda x: -x[0])
    return ranked


def apply_choice(action, choice):
    """A copy of the action whose last click targets the chosen sibling item."""
    a = copy.deepcopy(action)
    if not choice or not a.get("steps"):
        return a
    last = a["steps"][-1]
    orig = (last.get("target") or {}).get("name")
    last["target"]["name"] = choice
    last["target"]["automation_id"] = ""
    last.pop("fallback_rel_pos", None)
    last.pop("state_after", None)
    pc = a.get("postcondition")
    if pc and (pc.get("target") or {}).get("name") == orig:
        pc["target"]["name"] = choice
        pc["target"]["automation_id"] = ""
    return a


def build_plan(text, extract_value=None, profiles=None, builtin_names=None):
    """Returns {'plan': [...], 'unmatched': [...]} or None if no learned app is mentioned."""
    profiles = load_profiles() if profiles is None else profiles
    if not profiles:
        return None
    builtin_map = {stem(n.lower()): n for n in (builtin_names or [])}
    clauses = [c.strip() for c in re.split(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b)\s*",
                                           text.strip().rstrip("?.!")) if c.strip()]
    plan, unmatched, current, recognized = [], [], None, False

    for clause in clauses:
        toks = tokenize(clause)
        verb = next((v for v, ws in VERBS.items() if toks & ws), None)
        profile = find_app(toks, profiles)
        direct = bool(profile)
        if profile:
            recognized, current = True, profile
        else:
            hit = (toks - FILLER - ALL_VERBS) & set(builtin_map)
            if hit and verb in (None, "open"):          # a built-in app such as chrome or notepad
                for h in sorted(hit):
                    plan.append({"kind": "builtin_open", "name": builtin_map[h]})
                continue
            profile = current   # "...and change the wallpaper" continues with the previous app
        if not profile:
            rest = toks - FILLER - ALL_VERBS
            if rest:
                unmatched.append(ordered_words(clause, rest))   # never drop a part silently
            continue

        alias_tokens = set().union(*[tokenize(a) for a in profile["aliases"]], tokenize(profile["app"]))
        # "the settings app": drop 'app' right after the app's own name, but keep 'apps' as a real item name
        alias_words = {w for al in profile["aliases"] + [os.path.splitext(profile["app"])[0]]
                       for w in re.findall(r"[a-z0-9]{2,}", re.sub(PUNCT, "", al.lower()))}
        clause_n = re.sub(PUNCT, "", clause)
        if alias_words:
            pat = "|".join(sorted(map(re.escape, alias_words), key=len, reverse=True))
            clause_n = re.sub(r"\b(" + pat + r")\s+(?:app|application)\b", r"\1", clause_n, flags=re.I)
        toks2 = tokenize(clause_n)
        vocab = set().union(*[tokenize(action_text(x)) for x in profile["actions"]]) if profile["actions"] else set()
        leftover = toks2 - FUNCTION_WORDS - (UI_WORDS - vocab) - SOFT_VERBS - ALL_VERBS - alias_tokens

        if verb in ("close", "minimize", "restore"):
            plan.append({"kind": verb, "profile": profile})
            continue

        ranked = rank_actions(profile, leftover)
        action = choice = None
        candidates = []
        if ranked:
            top = ranked[0][0]
            candidates = [(a, ch) for sc, a, ch in ranked if sc == top]
            action, choice = candidates[0]
        used = (action_tokens(action) | (tokenize(choice) if choice else set())) if action else set()
        remaining = leftover - used
        want = None
        if action:
            st = (action.get("postcondition") or {}).get("state") or {}
            if "toggle" in st:                       # "turn on" / "turn off" decides the direction
                want = 1 if toks2 & ON_WORDS else 0 if toks2 & OFF_WORDS else None
                remaining = remaining - ON_WORDS - OFF_WORDS
        value = None
        if action and remaining and extract_value:
            step = next((s for s in action["steps"] if s["action"] == "type_text" and s.get("parameterizable")), None)
            if step:
                value = extract_value(clause, step["text"])
                if value:
                    remaining = set()

        if (direct and (verb == "open" or not leftover)) or action:
            if not (plan and plan[-1]["kind"] == "open" and plan[-1]["profile"] is profile):
                plan.append({"kind": "open", "profile": profile})
        if action:
            plan.append({"kind": "action", "profile": profile, "action": action, "choice": choice,
                         "value": value, "candidates": candidates, "want_toggle": want})
        if remaining:
            unmatched.append(ordered_words(clause, remaining))

    if not recognized:
        return None
    return {"plan": plan, "unmatched": unmatched}


def describe_item(item):
    k = item["kind"]
    if k == "builtin_open":
        return f"Open {item['name'].title()} (built-in app launcher)"
    p = display(item["profile"])
    if k == "open":
        return f"Open {p} (bring it to the front if it is already open)"
    if k == "close":
        return f"Close {p}"
    if k == "minimize":
        return f"Minimize {p}"
    if k == "restore":
        return f"Restore {p}"
    a = item["action"]
    extra = "  (skipped if already done)" if a.get("postcondition") else ""
    ch = f"  [pick: {item['choice']}]" if item.get("choice") else ""
    val = f"  [text: {item['value']!r}]" if item.get("value") else ""
    tg = {1: "  [turn on]", 0: "  [turn off]"}.get(item.get("want_toggle"), "")
    return f"In {p}: {a['name']}{ch}{tg}{extra}{val}"


# ===========================================================================
# Windows side (loaded lazily so the planning code above runs anywhere)
# ===========================================================================
def _executor():
    import executor
    return executor


def _rec():
    import ctypes
    from ctypes import wintypes
    import recorder   # reuse its Win32 helpers; importing it starts no listeners
    if not getattr(recorder, "_planner_ready", False):
        u = recorder.user32
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        recorder._planner_ready = True
    return recorder


def find_windows(profile):
    rec = _rec()
    alias_norms = {norm(a) for a in profile["aliases"]}
    app = profile["app"].lower()
    found = []
    for hwnd, info in rec.list_windows().items():
        name, _, _ = rec.resolve_app(hwnd, info["pid"])
        low = name.lower()
        title_norms = {norm(info["title"]), norm(info["title"].split(" - ")[-1])}
        if low == app or (app == "file explorer" and low == "explorer.exe" and info["class"] == "CabinetWClass"):
            found.append((hwnd, info))
        elif low == rec.FRAME_HOST and title_norms & alias_norms:
            found.append((hwnd, info))
    return found


def bring_to_front(hwnd):
    u = _rec().user32
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)        # SW_RESTORE
    u.keybd_event(0x12, 0, 0, 0)     # tap Alt so Windows allows the focus change
    u.SetForegroundWindow(hwnd)
    u.keybd_event(0x12, 0, 2, 0)


DIRECT_BLOCKLIST = ("\\windowsapps\\", "\\immersivecontrolpanel\\", "\\systemapps\\")


def direct_ok(profile):
    """Running the program file directly only works for ordinary programs, not Store / system apps."""
    exe = profile.get("exe_path") or ""
    low = exe.lower().replace("/", "\\")
    return bool(exe) and os.path.exists(exe) and not any(b in low for b in DIRECT_BLOCKLIST)


def start_menu_search(profile):
    from pynput import keyboard
    kb = keyboard.Controller()
    kb.press(keyboard.Key.cmd)
    kb.release(keyboard.Key.cmd)
    time.sleep(0.7)
    kb.type(profile.get("search_name") or os.path.splitext(profile["app"])[0])
    time.sleep(1.0)
    kb.press(keyboard.Key.enter)
    kb.release(keyboard.Key.enter)


def launch_methods(profile, ex):
    methods = []
    if direct_ok(profile):
        methods.append(("started its program", lambda: os.startfile(profile["exe_path"])))
    if profile.get("launch_steps"):
        def replay():
            for st in profile["launch_steps"]:
                ex.run_step(st, {})
        methods.append(("repeated how you opened it", replay))
    methods.append(("searched for it in the Start menu", lambda: start_menu_search(profile)))
    return methods


def wait_for_window(profile, seconds):
    for _ in range(int(seconds / 0.5)):
        time.sleep(0.5)
        wins = find_windows(profile)
        if wins:
            return wins
    return []


def ensure_open(profile, ex):
    wins = find_windows(profile)
    if wins:
        bring_to_front(wins[0][0])
        return "already open - brought to the front"
    tried = []
    for label, run in launch_methods(profile, ex):
        tried.append(label)
        try:
            run()
        except Exception as exc:
            tried[-1] = f"{label} (failed: {exc})"
            continue
        wins = wait_for_window(profile, 7)      # did a window really appear? if not, try the next method
        if wins:
            time.sleep(0.8)
            bring_to_front(wins[0][0])
            return f"opened ({label})"
    raise RuntimeError(f"{display(profile)} did not appear (tried: {'; '.join(tried)})")


def close_app(profile):
    rec = _rec()
    wins = find_windows(profile)
    if not wins:
        return "not open"
    for hwnd, _ in wins:
        rec.user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE: a polite close, like clicking X
    for _ in range(10):
        time.sleep(0.5)
        if not find_windows(profile):
            return "closed"
    return "asked it to close, but it is still open (it may be waiting for you, e.g. unsaved work)"


def minimize_app(profile):
    rec = _rec()
    wins = find_windows(profile)
    for hwnd, _ in wins:
        rec.user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE
    return "minimized" if wins else "not open"


def restore_app(profile):
    wins = find_windows(profile)
    if not wins:
        return "not open"
    bring_to_front(wins[0][0])
    return "restored"


def state_satisfied(pc, ex):
    """Is the end state of an action already true? (e.g. 'Home' already selected)"""
    rec = _rec()
    step = {"target": pc["target"], "app": pc.get("app"), "window_class": pc.get("window_class", "")}
    ctrl = ex.find_element(step, timeout=2.0)
    if ctrl is None:
        return False
    now = rec.read_state(ctrl)
    return bool(pc["state"]) and all(now.get(k) == v for k, v in pc["state"].items())


def click_position(step, profile):
    """For elements with no name: click at the same relative spot inside the app's window."""
    import ctypes
    from ctypes import wintypes
    from pynput import mouse
    rec = _rec()
    wins = find_windows(profile)
    if not wins or not step.get("rel_in_window"):
        raise RuntimeError("could not find the window to click in")
    hwnd = wins[0][0]
    bring_to_front(hwnd)
    time.sleep(0.3)
    r = wintypes.RECT()
    rec.user32.GetWindowRect(hwnd, ctypes.byref(r))
    rx, ry = step["rel_in_window"]
    x, y = int(r.left + rx * (r.right - r.left)), int(r.top + ry * (r.bottom - r.top))
    m = mouse.Controller()
    m.position = (x, y)
    time.sleep(0.15)
    btn = {"right": mouse.Button.right, "middle": mouse.Button.middle}.get(step["action"].split("_")[0], mouse.Button.left)
    m.click(btn, 2 if step["action"].startswith("double") else 1)


def target_visible(step, ex, timeout=2.5):
    return ex.find_element(step, timeout=timeout) is not None


def first_target_step(steps):
    return next((s for s in steps if s["action"].endswith("_element")), None)


def is_navigation(a):
    """A single click that moves you around (select a page, open a group/menu): safe to run as a
    stepping stone. Toggles, multi-step actions, typing and key presses are never used that way."""
    steps = a.get("steps", [])
    if len(steps) != 1 or not steps[0]["action"].endswith("_element"):
        return False
    st = (a.get("postcondition") or {}).get("state") or {}
    return "toggle" not in st


def prerequisites(profile, action):
    by_sig = {x["signature"]: x for x in profile["actions"] if x.get("signature")}
    return [by_sig[sg] for sg in action.get("after", []) if sg in by_sig and is_navigation(by_sig[sg])]


def execute_steps(steps, profile, value, ex):
    for step in steps:
        if ex.abort_event.is_set():
            raise ex.Aborted()
        a = step["action"]
        if a.endswith("_position"):
            click_position(step, profile)
            time.sleep(ex.SETTLE_SECONDS)
            continue
        if a == "type_text" and step.get("parameterizable") and value:
            step = dict(step, text=value)
        ex.run_step(step, {})


def run_action(item, ex):
    profile = item["profile"]
    action = apply_choice(item["action"], item.get("choice"))
    wins = find_windows(profile)
    if wins:
        bring_to_front(wins[0][0])
        time.sleep(0.4)
    pc = action.get("postcondition")
    if item.get("want_toggle") is not None and pc and "toggle" in (pc.get("state") or {}):
        pc = copy.deepcopy(pc)
        pc["state"]["toggle"] = item["want_toggle"]      # aim for what was asked, not what was demonstrated
    if pc and state_satisfied(pc, ex):
        return "skipped - it is already in that state"

    note = ""
    first = first_target_step(action["steps"])
    if first and not target_visible(first, ex, 2.5):
        # The thing to click is not on screen: the app is on a different page than when you taught it.
        # Run the earlier steps you took back then, only as many as needed, skipping any already satisfied.
        ran = []
        for pre in prerequisites(profile, item["action"]):
            if target_visible(first, ex, 0.8):
                break
            ppc = pre.get("postcondition")
            if ppc and state_satisfied(ppc, ex):
                continue
            execute_steps(pre["steps"], profile, None, ex)
            ran.append(pre["name"])
            time.sleep(0.6)
        if not target_visible(first, ex, 4.0):
            t = first["target"]
            label = t.get("name") or t.get("automation_id") or "its first target"
            tail = (f" even after running: {', '.join(ran)}" if ran else
                    " (no earlier steps were recorded for it, so I could not get there)")
            raise RuntimeError(f"could not find '{label}' in {display(profile)}{tail}")
        note = f" (first ran: {', '.join(ran)})" if ran else ""
    execute_steps(action["steps"], profile, item.get("value"), ex)
    return "done" + note


_BUILTIN_OPEN = None   # set by handle_request: function that opens a built-in app by name


def run_item(item, ex):
    k = item["kind"]
    if k == "builtin_open":
        return _BUILTIN_OPEN(item["name"]) if _BUILTIN_OPEN else "no built-in launcher available"
    p = item["profile"]
    if k == "open":
        return ensure_open(p, ex)
    if k == "close":
        return close_app(p)
    if k == "minimize":
        return minimize_app(p)
    if k == "restore":
        return restore_app(p)
    return run_action(item, ex)


def run_plan(plan):
    from pynput import keyboard
    ex = _executor()
    ex.abort_event.clear()
    listener = keyboard.Listener(on_press=lambda k: ex.abort_event.set() if k == ex.ABORT_KEY else None)
    listener.start()
    print("Starting in 3 seconds. Do not touch the mouse. Press F9 to abort.")
    time.sleep(3)
    n, item = 0, None
    try:
        for n, item in enumerate(plan, 1):
            print(f"  step {n}/{len(plan)}: {describe_item(item)}")
            print(f"      -> {run_item(item, ex)}")
    except ex.Aborted:
        return "Aborted."
    except Exception as exc:
        return f"STOPPED at step {n} ({describe_item(item)}): {exc}"
    finally:
        listener.stop()
    return "Finished. (I do not check the result on screen yet, so glance at it.)"


def choose_between_candidates(plan):
    """When several learned actions fit equally well, ask instead of silently using the first."""
    for item in plan:
        cands = item.get("candidates") or []
        if item["kind"] == "action" and len(cands) > 1:
            print("\nMore than one thing I learned fits. Which do you mean?")
            for i, (a, ch) in enumerate(cands, 1):
                print(f"  {i}. {a['name']}" + (f"  [pick: {ch}]" if ch else ""))
            raw = input("Type a number, or just press Enter for 1: ").strip()
            idx = int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(cands) else 0
            item["action"], item["choice"] = cands[idx]


def handle_request(text, extract_value=None, builtin=None):
    """Returns a result string if the request was about learned apps, else None (let other handlers try).
    builtin = (set of built-in app names, function that opens one by name)."""
    global _BUILTIN_OPEN
    names = None
    if builtin:
        names, _BUILTIN_OPEN = builtin
    built = build_plan(text, extract_value, builtin_names=names)
    if not built or not built["plan"]:
        return None
    choose_between_candidates(built["plan"])
    print("\nHere is what I understood:")
    for i, item in enumerate(built["plan"], 1):
        print(f"  {i}. {describe_item(item)}")
    if built["unmatched"]:
        print("  I do NOT know how to do: " + "; ".join(built["unmatched"])
              + "   (not learned yet - show me with /learn)")
    if input("Run this? [y/N]: ").strip().lower() != "y":
        return "Cancelled."
    return run_plan(built["plan"])