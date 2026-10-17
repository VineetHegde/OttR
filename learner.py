"""
learner.py - turns ONE free-form recording session into per-app knowledge.

You do not name or label anything. The recording is cut up by application:

  * which apps you used (the program hosting this assistant and the Windows shell are ignored)
  * how each app was started: a direct program path when that is possible, otherwise the
    clicks / typing you used to open it (taskbar click, Start-menu search, ...)
  * what happened to each app: opened, closed, minimized, restored
  * what you did INSIDE each app, split into small independent actions. A click that
    changes state (selects an item, flips a toggle) is its own action and remembers
    the state it produced, so it can be skipped later if that state already holds.

Result: skills/apps/<app>.json, one file per app. Sessions are merged, so the knowledge grows.
No raw keystroke log is kept: the recording is deleted after learning.
"""
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

from common import clean_label, mermaid_label, norm, save_json, slug, write_flowchart_html

RAW_FILE = "raw_recording.json"
PROFILES_DIR = os.path.join("skills", "apps")
CROP_DIR = os.path.join("skills", "crops")

SHELL_APPS = {
    "explorer.exe", "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "textinputhost.exe", "lockapp.exe", "applicationframehost.exe",
}
LIFECYCLE = {"window_opened", "window_closed", "window_minimized", "window_restored", "window_activated"}
CAPTION_BUTTONS = {"close", "minimize", "minimise", "maximize", "maximise", "restore", "restore down"}
SYSTEM_KEYS = {"alt+f4", "win+d", "win+m", "alt+tab", "win+tab", "win", "win+r",
               "win+up", "win+down", "win+left", "win+right"}
TRIGGER_KEYS = {"win", "win+r"}

ACTION_GAP = 4.0          # seconds of pause that start a new action
TRIGGER_WINDOW = 6.0      # shell clicks/typing this close before an app opens count as "how it was launched"
DOUBLE_CLICK_SECONDS = 0.6


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
NOISE_NAMES = {"non client input sink window"}


def is_caption_click(e):
    """Close/minimize/maximize buttons and the window frame itself are lifecycle, not tasks."""
    nm = clean_label(e.get("target", {}).get("name", "")).lower()
    if nm in CAPTION_BUTTONS or nm in NOISE_NAMES:
        return True
    title = (e.get("window_title") or "").lower()
    return bool(title) and nm in {f"{v} {title}" for v in ("close", "minimize", "maximize", "restore")}


def app_of(e):
    """App name for an event. File Explorer windows belong to explorer.exe, so tell them apart by class."""
    app = e.get("app") or "unknown"
    if app.lower() == "explorer.exe" and (e.get("window_class") or "").lower() == "cabinetwclass":
        return "File Explorer"
    return app


def is_shell(e):
    return app_of(e).lower() in SHELL_APPS


def trigger_label(e):
    """What a launch trigger 'says': a clicked item's name or the text typed into search."""
    if e["action"] == "click":
        return clean_label(e.get("target", {}).get("name", ""))
    if e["action"] == "type_text":
        return e.get("text") or ""
    return ""


def compatible(label, launch):
    """Does 'Google Chrome' / 'calculator' plausibly name the app that just opened?"""
    lab = norm(label)
    if len(lab) < 3:
        return False
    title = launch.get("title", "")
    ids = [norm(os.path.splitext(launch.get("app", ""))[0]), norm(title), norm(title.split(" - ")[-1])]
    return any(len(i) >= 3 and (lab in i or i in lab) for i in ids)


def has_state(e):
    st = e.get("state_after") or {}
    return "selected" in st or "toggle" in st


# ---------------------------------------------------------------------------
# turning events into replayable steps
# ---------------------------------------------------------------------------
def click_step(e):
    t = e.get("target", {})
    name = clean_label(t.get("name", ""))
    aid = (t.get("automation_id") or "").strip()
    verb = {"left": "click", "right": "right_click", "middle": "middle_click"}.get(e.get("button", "left"), "click")
    step = {"app": e.get("app", ""), "window_class": e.get("window_class", "")}
    if name or aid:
        step.update({
            "action": f"{verb}_element",
            "target": {"name": name, "control_type": t.get("control_type", ""),
                       "automation_id": aid, "class_name": t.get("class_name", ""),
                       "parent_name": t.get("parent_name", "")},
            "fallback_rel_pos": t.get("rel_pos"),
        })
    else:
        # no accessibility info (game, Java app, custom UI): position in window + image crop
        if not e.get("rel_in_window") and not e.get("crop"):
            return None
        step.update({
            "action": f"{verb}_position",
            "target": {"name": "", "control_type": t.get("control_type", ""),
                       "automation_id": "", "class_name": t.get("class_name", "")},
            "rel_in_window": e.get("rel_in_window"),
            "crop": e.get("crop"),
        })
    if e.get("state_after"):
        step["state_after"] = e["state_after"]
    return step


def step_key(s):
    t = s.get("target", {})
    rel = s.get("rel_in_window") or []
    return (t.get("name"), t.get("control_type"), t.get("automation_id"), t.get("parent_name"), s.get("app"),
            tuple(round(v, 2) for v in rel))


def build_steps(evts, parameterizable=True):
    steps = []
    last_t = 0.0
    for e in evts:
        a = e["action"]
        if a == "click":
            st = click_step(e)
            if st is None:
                continue
            key = step_key(st)
            prev = steps[-1] if steps else None
            if (prev and prev.get("_key") == key and e.get("button", "left") == "left"
                    and e["t"] - last_t <= DOUBLE_CLICK_SECONDS
                    and prev["action"] in ("click_element", "click_position")):
                prev["action"] = prev["action"].replace("click_", "double_click_")
                last_t = e["t"]
                continue
            st["_key"] = key
            steps.append(st)
            last_t = e["t"]
        elif a == "type_text":
            if e.get("sensitive"):
                steps.append({"action": "enter_password", "source": "user_input"})
            else:
                step = {"action": "type_text", "text": e.get("text", "")}
                if parameterizable:
                    step["parameterizable"] = True
                steps.append(step)
        elif a == "press_key":
            if steps and steps[-1]["action"] == "press_key" and steps[-1]["key"] == e["key"]:
                steps[-1]["repeat"] = steps[-1].get("repeat", 1) + 1
            else:
                steps.append({"action": "press_key", "key": e["key"]})
    for s in steps:
        s.pop("_key", None)
    return steps


def describe_steps(steps):
    parts = []
    for s in steps:
        a = s["action"]
        if a.endswith("_element"):
            nm = s["target"]["name"] or s["target"]["automation_id"] or "item"
            if not s["target"]["name"] and s["target"].get("parent_name"):
                nm = f"{nm} in {s['target']['parent_name']}"
            st = s.get("state_after") or {}
            verb = "Select" if st.get("selected") else "Toggle" if "toggle" in st else "Click"
            parts.append((verb, nm))
        elif a.endswith("_position"):
            parts.append(("Click", "(unlabeled spot)"))
        elif a == "type_text":
            parts.append(("Type", repr(s["text"])))
        elif a == "press_key":
            parts.append(("Press", s["key"] + (f" x{s['repeat']}" if s.get("repeat") else "")))
        elif a == "enter_password":
            parts.append(("Enter", "password"))
    if not parts:
        return "(empty)"
    if len(parts) == 1:
        return f"{parts[0][0]} {parts[0][1]}"
    shown = parts[:5]
    verbs = {v for v, _ in shown}
    if len(verbs) == 1:
        text = f"{shown[0][0]} " + ", ".join(n for _, n in shown)
    else:
        text = ", then ".join(f"{v} {n}" for v, n in shown)
    return text + (", ..." if len(parts) > 5 else "")


def postcondition_of(steps):
    for s in reversed(steps):
        st = s.get("state_after") or {}
        if ("selected" in st or "toggle" in st) and s["action"].endswith("_element"):
            t = s["target"]
            return {"target": {k: t.get(k, "") for k in ("name", "control_type", "automation_id")},
                    "app": s.get("app", ""), "window_class": s.get("window_class", ""),
                    "state": s["state_after"]}
    return None


def signature(steps):
    basis = [[s["action"], s.get("target", {}).get("name"), s.get("target", {}).get("automation_id"),
              s.get("target", {}).get("parent_name"), s.get("text"), s.get("key"), [round(v, 2) for v in (s.get("rel_in_window") or [])]]
             for s in steps]
    return hashlib.sha1(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:12]


def split_actions(app_events, life_times):
    """Cut one app's events into small independent actions."""
    groups, cur, last_t = [], [], None

    def flush():
        nonlocal cur
        if cur:
            groups.append(cur)
            cur = []

    for e in app_events:
        if last_t is not None:
            gap = e["t"] - last_t > ACTION_GAP
            lifecycle = any(last_t < lt <= e["t"] for lt in life_times)
            if gap or lifecycle:
                flush()
        stateful = e["action"] == "click" and has_state(e)
        if stateful:
            flush()           # a state-changing click is atomic
        cur.append(e)
        if stateful:
            flush()
        last_t = e["t"]
    flush()
    return groups


# ---------------------------------------------------------------------------
# the segmentation itself
# ---------------------------------------------------------------------------
def segment(events):
    events = sorted((e for e in events if isinstance(e, dict) and "action" in e), key=lambda e: e.get("t", 0))
    session = next((e for e in events if e["action"] == "session_start"), {})
    host = {h.lower() for h in session.get("host_apps", [])}
    events = [e for e in events if e["action"] != "session_start"]

    title_map = {}
    for e in events:
        if e["action"] == "click" and e.get("window_title") and (e.get("app") or "").lower() != "applicationframehost.exe":
            title_map[e["window_title"]] = e["app"]
    for e in events:
        if (e.get("app") or "").lower() == "applicationframehost.exe":
            t = e.get("title") or e.get("window_title") or ""
            if t in title_map:
                e["app"] = title_map[t]
    for w in session.get("windows", []):
        if (w.get("app") or "").lower() == "applicationframehost.exe" and w.get("title") in title_map:
            w["app"] = title_map[w["title"]]

    open_count = Counter(app_of(w).lower() for w in session.get("windows", []))
    life, titles, exe_paths, display = defaultdict(Counter), defaultdict(list), {}, {}
    life_times = defaultdict(list)
    launches = []

    for e in events:
        a = e["action"]
        if a not in LIFECYCLE:
            continue
        app = app_of(e)
        k = app.lower()
        if k in host or is_shell(e) or k == "unknown":
            continue
        display.setdefault(k, app)
        if a == "window_opened":
            if open_count[k] == 0:
                launches.append(e)
            open_count[k] += 1
            life[k]["opened"] += 1
            titles[k].append(e.get("title", ""))
            if e.get("exe_path") and k not in exe_paths:
                exe_paths[k] = e["exe_path"]
        elif a == "window_closed":
            open_count[k] = max(0, open_count[k] - 1)
            life[k]["closed"] += 1
        elif a == "window_minimized":
            life[k]["minimized"] += 1
        elif a == "window_restored":
            life[k]["restored"] += 1
        elif a == "window_activated":
            life[k]["activated"] += 1
        if a != "window_activated":
            life_times[k].append(e["t"])

    # which shell clicks / typing were "how the app was launched"
    def is_trigger(e):
        if e["action"] == "click":
            return is_shell(e)
        if e["action"] == "type_text":
            return is_shell(e) and not e.get("sensitive")
        if e["action"] == "press_key":
            return is_shell(e) or e.get("key") in TRIGGER_KEYS
        return False

    # A trigger only counts if it plausibly names the app it launched (so a stray taskbar click on
    # some other app is not blamed for a window that happens to open a moment later).
    launch_triggers = defaultdict(list)
    candidates = [e for e in events if e["action"] not in LIFECYCLE and is_trigger(e)]
    labeled = [e for e in candidates if trigger_label(e)]
    plain_keys = [e for e in candidates if not trigger_label(e)]
    for e in labeled:
        for i, L in enumerate(launches):
            if (L["t"] >= e["t"] - 0.2 and L["t"] - e["t"] <= TRIGGER_WINDOW
                    and compatible(trigger_label(e), L)):
                launch_triggers[i].append(e)
                break
    used = set()
    for i in list(launch_triggers):  # plain keys (Win, Enter) around a matching click / typed text
        evs = launch_triggers[i]
        lo, hi = min(x["t"] for x in evs) - 2.5, max(x["t"] for x in evs) + 2.5
        for e in plain_keys:
            if id(e) not in used and lo <= e["t"] <= min(hi, launches[i]["t"] + 0.2):
                evs.append(e)
                used.add(id(e))
        evs.sort(key=lambda x: x["t"])

    # events inside apps
    content = defaultdict(list)
    ignored = Counter()
    skipped_clicks = 0
    for e in events:
        a = e["action"]
        if a not in ("click", "type_text", "press_key"):
            continue
        app = app_of(e)
        k = app.lower()
        if k in host:
            ignored[f"{app} (hosts this assistant)"] += 1
            continue
        if k == "unknown":
            ignored["unidentified window"] += 1
            continue
        if is_shell(e):
            continue  # taskbar / Start menu: used as launch triggers or ignored
        if a == "click" and is_caption_click(e):
            continue
        if a == "press_key" and e.get("key") in SYSTEM_KEYS:
            continue
        if a == "click" and click_step(e) is None:
            skipped_clicks += 1
            continue
        display.setdefault(k, app)
        content[k].append(e)

    profiles, dropped = [], []
    for k in sorted(set(content) | {app_of(L).lower() for L in launches}):
        name = display.get(k, k)

        launch_steps, opened_by = [], None
        for i, L in enumerate(launches):
            if app_of(L).lower() == k:
                launch_steps = build_steps(launch_triggers.get(i, []), parameterizable=False)
                if launch_steps and launch_steps[-1]["action"] == "type_text":
                    launch_steps.append({"action": "press_key", "key": "enter"})
                if not launch_steps:   # maybe another app opened it (e.g. double-clicking a photo)
                    before = [e for e in events
                              if e["action"] in ("click", "type_text", "press_key")
                              and 0 <= L["t"] - e["t"] <= 3.0
                              and app_of(e).lower() != k and app_of(e).lower() not in host
                              and not is_shell(e)]
                    if before:
                        opened_by = {"app": app_of(before[-1]),
                                     "step": describe_steps(build_steps([before[-1]], parameterizable=False))}
                break
        exe = exe_paths.get(k, "")
        direct = bool(exe) and not any(b in exe.lower() for b in
                                       ("\\windowsapps\\", "\\immersivecontrolpanel\\", "\\systemapps\\"))

        if not content.get(k) and not launch_steps and not opened_by:
            dropped.append(name)   # a window appeared with no cause we can explain: probably noise
            continue

        actions, seen_names = [], Counter()
        chain, last_end = [], None
        resets = [e["t"] for e in events
                  if e["action"] in ("window_opened", "window_closed") and app_of(e).lower() == k]
        for group in split_actions(content.get(k, []), life_times.get(k, [])):
            steps = build_steps(group)
            if not steps:
                continue
            start, end = group[0]["t"], group[-1]["t"]
            if last_end is not None and any(last_end < r <= start for r in resets):
                chain = []       # the app was closed/opened in between: earlier steps no longer apply
            nm = describe_steps(steps)
            seen_names[nm] += 1
            if seen_names[nm] > 1:
                nm = f"{nm} ({seen_names[nm]})"
            sig = signature(steps)
            # "after" = what you had already done in this app when you did this action. Later, if the
            # action's target is not on screen, these earlier actions are what can get you there.
            actions.append({"name": nm, "steps": steps, "postcondition": postcondition_of(steps),
                            "signature": sig, "after": chain[-6:]})
            chain.append(sig)
            last_end = end

        stem = os.path.splitext(name)[0].lower()
        aliases = [stem]
        click_titles = Counter(e.get("window_title", "") for e in content.get(k, []) if e.get("window_title"))
        seen_titles = titles.get(k, [])[:3] + [t for t, _ in click_titles.most_common(2)]
        for title in seen_titles:
            tail = title.split(" - ")[-1].strip().lower()
            if tail and len(tail) <= 30:
                aliases.append(tail)
            if title and " - " not in title and len(title) <= 30:
                aliases.append(title.lower())
        aliases = list(dict.fromkeys(a for a in aliases if a))[:6]
        simple = [a for a in aliases if re.fullmatch(r"[a-z0-9 ]{3,24}", a)]
        search_name = min(simple, key=len) if simple else stem

        profiles.append({
            "app": name,
            "aliases": aliases,
            "search_name": search_name,
            "exe_path": exe,
            "direct_launch": direct,
            "launch_steps": launch_steps,
            "opened_by": opened_by,
            "lifecycle_seen": dict(life.get(k, {})),
            "actions": actions,
            "sessions": 1,
            "updated": datetime.now().isoformat(timespec="seconds"),
        })

    return {"profiles": profiles, "ignored": dict(ignored), "dropped": dropped,
            "unlabeled_clicks": skipped_clicks, "event_count": len(events)}


# ---------------------------------------------------------------------------
# saving (merging with what is already known)
# ---------------------------------------------------------------------------
def profile_path(app):
    return os.path.join(PROFILES_DIR, f"{slug(app) or 'app'}.json")


def merge_and_save(profile):
    os.makedirs(PROFILES_DIR, exist_ok=True)
    path = profile_path(profile["app"])
    new_actions = len(profile["actions"])
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            old = json.load(f)
        known = {a["signature"] for a in old.get("actions", [])}
        old_by_sig = {a["signature"]: a for a in old.get("actions", [])}
        for a in profile["actions"]:
            prev = old_by_sig.get(a["signature"])
            if prev is not None and not prev.get("after") and a.get("after"):
                prev["after"] = a["after"]
        added = [a for a in profile["actions"] if a["signature"] not in known]
        names = {a["name"] for a in old.get("actions", [])}
        for a in added:
            while a["name"] in names:
                a["name"] += " (new)"
            names.add(a["name"])
        old["actions"] = old.get("actions", []) + added
        old["aliases"] = list(dict.fromkeys(old.get("aliases", []) + profile["aliases"]))[:8]
        old["search_name"] = old.get("search_name") or profile.get("search_name", "")
        old["exe_path"] = old.get("exe_path") or profile["exe_path"]
        old["direct_launch"] = old.get("direct_launch") or profile["direct_launch"]
        if not old.get("launch_steps"):
            old["launch_steps"] = profile["launch_steps"]
        old["opened_by"] = old.get("opened_by") or profile.get("opened_by")
        merged_life = Counter(old.get("lifecycle_seen", {}))
        merged_life.update(profile["lifecycle_seen"])
        old["lifecycle_seen"] = dict(merged_life)
        old["sessions"] = old.get("sessions", 1) + 1
        old["updated"] = profile["updated"]
        profile, new_actions = old, len(added)
    save_json(path, profile)
    return path, new_actions


def collect_crops_in_use():
    used = set()
    if not os.path.isdir(PROFILES_DIR):
        return used
    for fn in os.listdir(PROFILES_DIR):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(PROFILES_DIR, fn), "r", encoding="utf-8") as f:
                    text = f.read()
                used.update(m.replace("\\", "/") for m in re.findall(r"skills[/\\\\]+crops[/\\\\]+[\w.\-]+\.png", text))
            except Exception:
                continue
    return used


def clean_unused_crops():
    if not os.path.isdir(CROP_DIR):
        return
    used = {os.path.basename(p) for p in collect_crops_in_use()}
    for fn in os.listdir(CROP_DIR):
        if fn.endswith(".png") and fn not in used:
            try:
                os.remove(os.path.join(CROP_DIR, fn))
            except OSError:
                pass


# ---------------------------------------------------------------------------
def print_report(result, saved):
    print("\n=== What I learned from this session ===")
    if not result["profiles"]:
        print("I could not find any app activity I can learn from.")
    for p in result["profiles"]:
        path, added = saved.get(p["app"], ("", 0))
        life = ", ".join(f"{k} x{v}" for k, v in p["lifecycle_seen"].items() if k != "activated") or "none seen"
        print(f"\n{p['app']}   (aliases: {', '.join(p['aliases'])})")
        if p["launch_steps"]:
            how = "; ".join(describe_steps([s]) for s in p["launch_steps"])
            print(f"  how it was opened : {how}")
        if p.get("opened_by"):
            ob = p["opened_by"]
            print(f"  opened from inside: {ob['app']} ({ob['step']})")
        if p["direct_launch"]:
            print(f"  direct launch     : {p['exe_path']}")
        elif p["exe_path"]:
            how2 = "the clicks/typing above" if p["launch_steps"] else "a Start-menu search"
            print(f"  direct launch     : not possible (Store app) - will use {how2}")
        print(f"  window events     : {life}")
        if p["actions"]:
            print("  actions learned   :")
            for i, a in enumerate(p["actions"], 1):
                pc = "  [skips itself if already done]" if a["postcondition"] else ""
                print(f"    {i}. {a['name']}{pc}")
        else:
            print("  actions learned   : none (only opening/closing was seen)")
        if path:
            print(f"  saved to {path} ({added} new action(s))")
    if result["ignored"]:
        print("\nIgnored: " + "; ".join(f"{k}: {v} event(s)" for k, v in result["ignored"].items()))
    if result["dropped"]:
        print("Ignored windows with no explained cause: " + ", ".join(result["dropped"]))
    if result["unlabeled_clicks"]:
        print(f"{result['unlabeled_clicks']} click(s) had no name and no usable position, so they were skipped.")


def generate_learn_flowchart(result):
    lines = ["graph TD", "    Start((Session Start))"]
    n, prev = 0, "Start"
    for p in result.get("profiles", []):
        n += 1
        node = f"App_{n}"
        lines.append(f'    {prev} -->|Opens| {node}["{mermaid_label(p.get("app", "Unknown App"))}"]')
        prev = node
        for action in p.get("actions", []):
            n += 1
            node = f"Act_{n}"
            lines.append(f'    {prev} --> {node}["{mermaid_label(action.get("name", "Unknown Action"))}"]')
            prev = node
    lines.append(f"    {prev} --> End((Session End))")
    path = write_flowchart_html("learn_Output", "learn", "Learned Session Flowchart", lines)
    if path:
        print(f"\n[+] Visual flowchart saved to: {path}")


def learn_from_recording(path=RAW_FILE, delete_raw=True):
    try:
        with open(path, "r", encoding="utf-8") as f:
            events = json.load(f)
    except FileNotFoundError:
        print(f"'{path}' not found. Run the recorder first.")
        return None
    except json.JSONDecodeError as exc:
        print(f"'{path}' is not valid JSON ({exc}). Record again.")
        return None

    try:
        result = segment(events)
        saved = {}
        for p in result["profiles"]:
            saved[p["app"]] = merge_and_save(p)
        clean_unused_crops()
        print_report(result, saved)
        generate_learn_flowchart(result)
    except Exception:
        # Do NOT delete the recording: it is the only copy of the demonstration.
        # It contains everything typed (except password fields), so delete it yourself once you are done.
        print(f"\nLearning failed. '{path}' was kept so you can retry after a fix; it contains the text you typed.")
        raise

    if delete_raw:
        try:
            os.remove(path)
            print("\n(The raw recording was deleted. Only the learned structure is kept.)")
        except OSError:
            pass
    return result


if __name__ == "__main__":
    learn_from_recording(sys.argv[1] if len(sys.argv) > 1 else RAW_FILE, delete_raw=False)