"""
planner.py - plain-language requests -> actions, using what /learn recorded (skills/apps/*.json).

How a request is handled:
  1. split it into clauses ("open settings, then dark mode")
  2. find the learned app each clause is about, and the learned actions that match its words
  3. for every action: skip it if its end state already holds (selected / toggled), and run
     the actions that came before it in the demo only if its target is not on screen
"""
import json
import os
import re
import time

from common import mermaid_label, norm, stem, tokenize, write_flowchart_html

PROFILES_DIR = os.path.join("skills", "apps")

VERBS = {
    "open": tokenize("open launch start run show go switch bring"),
    "close": tokenize("close quit exit kill shut"),
    "minimize": tokenize("minimize minimise hide"),
    "restore": tokenize("restore maximize maximise unminimize"),
}
ALL_VERBS = set().union(*VERBS.values())
FILLER = tokenize(
    "please can could you would will i want need to the a an my me in on of for under inside also now it "
    "this that up with at into app application window folder page tab screen menu section option button "
    "do is are be just from some any something change set make go use click type press mode and turn off select toggle choose"
)


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


def action_tokens(a):
    return tokenize(a["name"]) - FILLER


def action_key(a):
    return a.get("signature") or a["name"]


def has_skippable_state(a):
    """True if running this action can be skipped when its end state already holds."""
    pc = a.get("postcondition") or {}
    return any(k in (pc.get("state") or {}) for k in ("selected", "toggle"))


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
def find_app(clause, tokens, profiles):
    best, best_score = None, 0
    for p in profiles:
        for alias in p["aliases"] + [os.path.splitext(p["app"])[0]]:
            at = tokenize(alias)
            if at and at <= tokens and len(at) * 10 > best_score:
                best, best_score = p, len(at) * 10

    if not best:
        nc = norm(clause)
        for p in profiles:
            for alias in p["aliases"] + [os.path.splitext(p["app"])[0]]:
                na = norm(alias)
                if len(na) >= 3 and na in nc and len(na) > best_score:
                    best, best_score = p, len(na)
    return best


def ordered_words(clause, tokens):
    out = []
    for w in re.findall(r"[a-z0-9]+", clause.lower()):
        if stem(w) in tokens and w not in out:
            out.append(w)
    return " ".join(out)


def best_actions(profile, leftover, clause_order=None):
    """Pick the learned actions that explain the words left in a clause.

    Returns ([(action, tie, matched_words), ...], words_nobody_explained).
    The actions come back in the order the user mentioned them (ties: the order they were learned).
    """
    found, curr, used = [], set(leftover), set()
    while True:
        best, best_score, tie = None, 0, False
        for a in profile["actions"]:
            if action_key(a) in used:
                continue
            score = len(curr & action_tokens(a))
            if score > best_score:
                best, best_score, tie = a, score, False
            elif score and score == best_score:
                tie = True
        if best is None:
            break
        matched = curr & action_tokens(best)
        used.add(action_key(best))
        found.append((best, tie, matched))
        curr -= matched

    first_pos = {}
    for i, w in enumerate(clause_order or []):
        first_pos.setdefault(w, i)
    learned_at = {id(a): i for i, a in enumerate(profile["actions"])}
    big = 10 ** 6
    found.sort(key=lambda f: (min((first_pos.get(t, big) for t in f[2]), default=big), learned_at[id(f[0])]))
    return found, curr


def build_plan(text, extract_value=None, profiles=None, builtin_names=None):
    profiles = load_profiles() if profiles is None else profiles
    if not profiles:
        return None
    builtin_map = {stem(n.lower()): n for n in (builtin_names or [])}
    clauses = [c.strip() for c in re.split(r"\s*(?:,|;|\band then\b|\bthen\b)\s*",
                                           text.strip().rstrip("?.!")) if c.strip()]
    plan, unmatched, current, recognized = [], [], None, False
    planned = set()    # actions already in the plan (so a prerequisite is not added twice)

    for clause in clauses:
        toks = tokenize(clause)
        clause_order = [stem(w) for w in re.findall(r"[a-z0-9]+", clause.lower())]
        verb = next((v for v, ws in VERBS.items() if toks & ws), None)
        profile = find_app(clause, toks, profiles)
        if profile:
            recognized, current = True, profile
        else:
            hit = (toks - FILLER - ALL_VERBS) & set(builtin_map)
            if hit and verb in (None, "open"):
                for h in sorted(hit):
                    plan.append({"kind": "builtin_open", "name": builtin_map[h]})
                continue
            profile = current
        if not profile:
            rest = toks - FILLER - ALL_VERBS
            if rest:
                unmatched.append(ordered_words(clause, rest))
            continue

        alias_tokens = set().union(*[tokenize(a) for a in profile["aliases"]], tokenize(profile["app"]))
        leftover = toks - FILLER - ALL_VERBS - alias_tokens

        if verb in ("close", "minimize", "restore"):
            plan.append({"kind": verb, "profile": profile})
            continue

        # "open settings and notepad": a built-in app named in the same clause as a learned app
        builtin_hits = sorted(leftover & set(builtin_map)) if verb in (None, "open") else []
        leftover = leftover - set(builtin_hits)

        actions_found, remaining = best_actions(profile, leftover, clause_order)

        last_profile = next((item["profile"] for item in reversed(plan) if "profile" in item), None)
        if (verb == "open" or actions_found or not leftover) and last_profile is not profile:
            plan.append({"kind": "open", "profile": profile})

        by_sig = {a.get("signature"): a for a in profile["actions"] if a.get("signature")}
        for action, tie, _ in actions_found:
            # one value per typed step that can change (a request can supply several)
            values = []
            if extract_value:
                for st in action["steps"]:
                    if st["action"] == "type_text" and st.get("parameterizable"):
                        values.append(extract_value(clause, st["text"]))

            # what the user did before this action in the demo; only run if the target is not on screen
            prereqs = []
            for sig in action.get("after") or []:
                pre = by_sig.get(sig)
                if pre is not None and pre is not action and action_key(pre) not in planned:
                    prereqs.append(pre)

            plan.append({"kind": "action", "profile": profile, "action": action,
                         "values": values, "tie": tie, "prereqs": prereqs})
            planned.add(action_key(action))

        for h in builtin_hits:
            plan.append({"kind": "builtin_open", "name": builtin_map[h]})

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
    vals = [v for v in item.get("values") or [] if v]
    val = f"  [text: {', '.join(repr(v) for v in vals)}]" if vals else ""
    skip = "  [skipped if already selected/on]" if has_skippable_state(a) else ""
    pre = ""
    if item.get("prereqs"):
        pre = "  [if its target is not on screen, first: " + "; ".join(x["name"] for x in item["prereqs"]) + "]"
    amb = "  [more than one action fit; using the first]" if item.get("tie") else ""
    return f"In {p}: {a['name']}{val}{skip}{pre}{amb}"


# ---------------------------------------------------------------------------
# running (Windows only; these import their Windows modules lazily)
# ---------------------------------------------------------------------------
def _executor():
    import executor
    return executor


def _winapi():
    import winapi
    return winapi


def find_windows(profile):
    w = _winapi()
    alias_norms = {norm(a) for a in profile["aliases"]} - {""}
    app = profile["app"].lower()
    found = []
    for hwnd, info in w.list_windows().items():
        name, _, _ = w.resolve_app(hwnd, info["pid"])
        low = name.lower()
        title_norms = {norm(info["title"]), norm(info["title"].split(" - ")[-1])}
        if low == app or (app == "file explorer" and low == "explorer.exe" and info["class"] == "CabinetWClass"):
            found.append((hwnd, info))
        elif low == w.FRAME_HOST and title_norms & alias_norms:
            found.append((hwnd, info))
    return found


def bring_to_front(hwnd):
    _winapi().bring_to_front(hwnd)


DIRECT_BLOCKLIST = ("\\windowsapps\\", "\\immersivecontrolpanel\\", "\\systemapps\\")


def direct_ok(profile):
    exe = profile.get("exe_path") or ""
    low = exe.lower().replace("/", "\\")
    return bool(exe) and os.path.exists(exe) and not any(b in low for b in DIRECT_BLOCKLIST)


def start_menu_search(profile):
    from pynput import keyboard
    kb = keyboard.Controller()
    kb.press(keyboard.Key.cmd)
    kb.release(keyboard.Key.cmd)
    time.sleep(1.0)  # wait for the Start menu to animate

    search_text = profile.get("search_name") or os.path.splitext(profile["app"])[0]

    # type character by character: Windows Search drops letters when they arrive too fast
    for char in search_text:
        kb.type(char)
        time.sleep(0.05)

    time.sleep(2.0)  # let the search results render
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
        except ex.Aborted:
            raise
        except Exception as exc:
            tried[-1] = f"{label} (failed: {exc})"
            continue
        wins = wait_for_window(profile, 12)
        if wins:
            time.sleep(0.8)
            bring_to_front(wins[0][0])
            return f"opened ({label})"
    raise RuntimeError(f"App '{display(profile)}' failed to open. Fallbacks tried: {'; '.join(tried)}. "
                       "Please open it manually and try again.")


def close_app(profile):
    w = _winapi()
    wins = find_windows(profile)
    if not wins:
        return "not open"
    for hwnd, _ in wins:
        w.close_window(hwnd)
    for _ in range(10):
        time.sleep(0.5)
        if not find_windows(profile):
            return "closed"
    return "asked it to close, but it is still open (it may be waiting for you, e.g. unsaved work)"


def minimize_app(profile):
    w = _winapi()
    wins = find_windows(profile)
    for hwnd, _ in wins:
        w.minimize_window(hwnd)
    return "minimized" if wins else "not open"


def restore_app(profile):
    wins = find_windows(profile)
    if not wins:
        return "not open"
    bring_to_front(wins[0][0])
    return "restored"


def click_position(step, profile):
    """Click a recorded spot (apps with no accessibility info) inside the app's window."""
    from pynput import mouse
    w = _winapi()
    wins = find_windows(profile)
    if not wins or not step.get("rel_in_window"):
        raise RuntimeError("could not find the window to click in")
    hwnd = wins[0][0]
    bring_to_front(hwnd)
    time.sleep(0.3)
    left, top, right, bottom = w.window_rect(hwnd)
    rx, ry = step["rel_in_window"]
    x, y = int(left + rx * (right - left)), int(top + ry * (bottom - top))
    m = mouse.Controller()
    m.position = (x, y)
    time.sleep(0.15)
    verb = step["action"].split("_")[0]
    btn = {"right": mouse.Button.right, "middle": mouse.Button.middle}.get(verb, mouse.Button.left)
    m.click(btn, 2 if verb == "double" else 1)


def _perform(action, profile, ex, values=None, check_state=True):
    """Run one learned action. Returns False if it was skipped because its end state already holds."""
    pc = action.get("postcondition")
    if check_state and pc and has_skippable_state(action) and ex.state_satisfied(pc):
        return False
    vals = list(values or [])
    for step in action["steps"]:
        if ex.abort_event.is_set():
            raise ex.Aborted()
        a = step["action"]
        if a.endswith("_position"):
            click_position(step, profile)
            time.sleep(ex.SETTLE_SECONDS)
            continue
        if a == "type_text" and step.get("parameterizable"):
            v = vals.pop(0) if vals else None
            if v:
                step = dict(step, text=v)
        ex.run_step(step, {})
    return True


def run_action(item, ex):
    profile, action = item["profile"], item["action"]
    wins = find_windows(profile)
    if wins:
        bring_to_front(wins[0][0])
        time.sleep(0.4)

    notes = []
    pc = action.get("postcondition")
    if pc and has_skippable_state(action) and ex.state_satisfied(pc):
        return "already in that state - skipped"

    # Prerequisites: only when the action starts with an element that is not on screen.
    first = action["steps"][0] if action.get("steps") else None
    if item.get("prereqs") and first and first["action"].endswith("_element") and not ex.target_visible(first):
        for pre in item["prereqs"]:
            ran = _perform(pre, profile, ex)
            notes.append(f"{'ran' if ran else 'skipped'} prerequisite '{pre['name']}'")

    _perform(action, profile, ex, item.get("values"), check_state=False)  # state was checked above
    return "done" + (f" ({'; '.join(notes)})" if notes else "")


_BUILTIN_OPEN = None


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
    ex = _executor()
    n, item = 0, None
    with ex.abort_listener():
        print("Starting in 3 seconds. Do not touch the mouse. Press F9 to abort.")
        time.sleep(3)
        try:
            for n, item in enumerate(plan, 1):
                print(f"  step {n}/{len(plan)}: {describe_item(item)}")
                print(f"      -> {run_item(item, ex)}")
        except ex.Aborted:
            return "Aborted."
        except Exception as exc:
            return f"STOPPED at step {n} ({describe_item(item)}): {exc}"
    return "Finished. (I do not check the result on screen yet, so glance at it.)"


def generate_execute_flowchart(plan, request_text):
    lines = ["graph TD", f'    Start(("User request: {mermaid_label(request_text)}"))']
    prev = "Start"
    for n, item in enumerate(plan, 1):
        node = f"Step_{n}"
        lines.append(f'    {prev} -->|Step {n}| {node}["{mermaid_label(describe_item(item))}"]')
        prev = node
    lines.append(f"    {prev} --> End((Execute))")
    path = write_flowchart_html("execute_Output", "execute", "Execution Plan Flowchart", lines)
    if path:
        print(f"\n[+] Visual execution plan saved to: {path}")


def handle_request(text, extract_value=None, builtin=None):
    global _BUILTIN_OPEN
    names = None
    if builtin:
        names, _BUILTIN_OPEN = builtin
    built = build_plan(text, extract_value, profiles=load_profiles(), builtin_names=names)
    if not built or not built["plan"]:
        return None
    print("\nHere is what I understood:")
    for i, item in enumerate(built["plan"], 1):
        print(f"  {i}. {describe_item(item)}")
    if built["unmatched"]:
        print("  I do NOT know how to do: " + "; ".join(built["unmatched"])
              + "   (not learned yet - show me with /learn)")

    generate_execute_flowchart(built["plan"], text)

    if input("Run this? [y/N]: ").strip().lower() != "y":
        return "Cancelled."
    return run_plan(built["plan"])
