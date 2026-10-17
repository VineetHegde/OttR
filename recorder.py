"""
recorder.py (v3) - free-form session recorder
"""
import os
import queue
import threading
import time

import uiautomation as auto
from pynput import keyboard, mouse

from common import read_state, save_json
# importing winapi also sets DPI awareness (pynput, UI Automation and screenshots must share pixel coordinates)
from winapi import (FRAME_HOST, app_of_hwnd, find_host_apps, list_windows, proc_name,  # noqa: F401
                    resolve_app, user32, window_class, window_text)

try:
    from PIL import ImageGrab
except ImportError:  # crops are optional
    ImageGrab = None

OUTPUT_FILE = "raw_recording.json"
CROP_DIR = os.path.join("skills", "crops")
SESSION = time.strftime("%Y%m%d_%H%M%S")
POLL_SECONDS = 0.4      # how often window changes are checked
STATE_DELAY = 0.5       # wait this long after a click before reading element state
CROP_SIZE = 48          # pixels, only used for unnamed elements
STOP_KEY = keyboard.Key.f8
PAUSE_KEY = keyboard.Key.f7

HOST_APPS = find_host_apps()


# ---------------------------------------------------------------------------
# Input callbacks (must stay fast: they only queue work)
# ---------------------------------------------------------------------------
events_q = queue.Queue()
TICK = object()
buffer_lock = threading.Lock()
type_buffer = []
held_mods = set()
state = {"paused": False, "mod_used": False, "shift": False}
down_keys = set()
SHIFT_KEYS = {keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r}
MODIFIER_NAMES = {
    keyboard.Key.ctrl: "ctrl", keyboard.Key.ctrl_l: "ctrl", keyboard.Key.ctrl_r: "ctrl",
    keyboard.Key.alt: "alt", keyboard.Key.alt_l: "alt", keyboard.Key.alt_r: "alt",
    keyboard.Key.cmd: "win", keyboard.Key.cmd_l: "win", keyboard.Key.cmd_r: "win",
}


def flush_typing():
    with buffer_lock:
        if not type_buffer:
            return
        text = "".join(type_buffer)
        type_buffer.clear()
    events_q.put({"kind": "type", "text": text, "t": time.time()})


def add_char(ch):
    with buffer_lock:
        first = not type_buffer
        type_buffer.append(ch)
    if first:
        events_q.put({"kind": "probe"})  


def key_label(key):
    if isinstance(key, keyboard.KeyCode):
        if key.char and key.char.isprintable():
            return key.char.lower()
        vk = getattr(key, "vk", None)
        if vk and 48 <= vk <= 90:
            return chr(vk).lower()
        return f"vk{vk}"
    return key.name


def on_click(x, y, button, pressed):
    if not pressed or state["paused"]:
        return
    flush_typing()
    events_q.put({"kind": "click", "x": int(x), "y": int(y), "button": button.name, "t": time.time()})


def on_press(key):
    if key == STOP_KEY:
        flush_typing()
        return False
    if key == PAUSE_KEY:
        flush_typing()
        state["paused"] = not state["paused"]
        print("-- PAUSED --" if state["paused"] else "-- RESUMED --")
        return
    if state["paused"]:
        return
    if key in SHIFT_KEYS:
        state["shift"] = True
        return
    if key in MODIFIER_NAMES:
        held_mods.add(MODIFIER_NAMES[key])
        return
    if not isinstance(key, keyboard.KeyCode) and key not in (keyboard.Key.space, keyboard.Key.backspace):
        label = key_label(key)
        if label in down_keys:  
            return
        down_keys.add(label)
    if held_mods:
        state["mod_used"] = True
        flush_typing()
        parts = sorted(held_mods) + (["shift"] if state["shift"] else [])
        events_q.put({"kind": "key", "key": "+".join(parts + [key_label(key)]), "t": time.time()})
        return
    if isinstance(key, keyboard.KeyCode) and key.char and key.char.isprintable():
        add_char(key.char)
        return
    if key == keyboard.Key.space:
        add_char(" ")
        return
    if key == keyboard.Key.backspace:
        with buffer_lock:
            if type_buffer:
                type_buffer.pop()
                return
    flush_typing()
    events_q.put({"kind": "key", "key": key_label(key), "t": time.time()})


def on_release(key):
    if key in SHIFT_KEYS:
        state["shift"] = False
        return
    if not isinstance(key, keyboard.KeyCode):
        down_keys.discard(key_label(key))
    name = MODIFIER_NAMES.get(key)
    if not name:
        return
    held_mods.discard(name)
    if name == "win" and not state["mod_used"] and not state["paused"]:
        flush_typing()
        events_q.put({"kind": "key", "key": "win", "t": time.time()})
    if not held_mods:
        state["mod_used"] = False


# ---------------------------------------------------------------------------
# Worker: does all UI Automation + window polling in ONE thread
# ---------------------------------------------------------------------------
def nearest_named_parent(ctrl, levels=4):
    try:
        p = ctrl
        for _ in range(levels):
            p = p.GetParentControl()
            if p is None:
                return ""
            nm = (p.Name or "").strip()
            if nm:
                return nm
    except Exception:
        pass
    return ""


class Worker:
    def __init__(self):
        self.records = []
        self.pending = []          # (due_time, record, x, y) -> read element state later
        self.password_field = False
        self.prev = None           # last window snapshot {hwnd: {...}}
        self.unresolved = {}
        self.last_fg = None
        self.last_poll = 0.0
        self.crop_n = 0
        self.title_app = {}        # window title -> real app (to fix Store-app frame windows)

    # -- helpers ---------------------------------------------------------
    def emit(self, rec, show=None):
        self.records.append(rec)
        if show:
            print(show)

    def fix_app(self, app, title):
        """Store apps hide behind ApplicationFrameHost; use what we learned from clicks to name them."""
        if (app or "").lower() == FRAME_HOST:
            return self.title_app.get(title, app)
        return app

    def fg_context(self):
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                raise ValueError("no foreground window")
            return {
                "app": app_of_hwnd(int(hwnd))[0],
                "window_title": window_text(hwnd),
                "window_class": window_class(hwnd),
            }
        except Exception:
            return {"app": "unknown", "window_title": "", "window_class": ""}

    def save_crop(self, x, y):
        if ImageGrab is None:
            return None
        try:
            os.makedirs(CROP_DIR, exist_ok=True)
            self.crop_n += 1
            half = CROP_SIZE // 2
            img = ImageGrab.grab(bbox=(x - half, y - half, x + half, y + half), all_screens=True)
            path = os.path.join(CROP_DIR, f"{SESSION}_{self.crop_n}.png")
            img.save(path)
            return path.replace("\\", "/")
        except Exception:
            return None

    # -- input events ----------------------------------------------------
    def handle(self, ev):
        kind = ev["kind"]
        if kind == "probe":
            try:
                focused = auto.GetFocusedControl()
                is_pw = bool(focused and getattr(focused, "IsPassword", False))
                if is_pw and not self.password_field:
                    for r in reversed(self.records[-3:]):
                        if r.get("action") == "type_text" and not r.get("sensitive"):
                            r["text"], r["sensitive"] = None, True
                self.password_field = is_pw
            except Exception:
                self.password_field = True  

        elif kind == "click":
            self.handle_click(ev)

        elif kind == "type":
            rec = {"t": ev["t"], "action": "type_text"}
            if self.password_field:
                rec["text"], rec["sensitive"] = None, True
                shown = "type   (hidden: password field)"
            else:
                rec["text"] = ev["text"]
                shown = f"type   {ev['text']!r}"
            rec.update(self.fg_context())
            self.emit(rec, shown)

        elif kind == "key":
            rec = {"t": ev["t"], "action": "press_key", "key": ev["key"]}
            rec.update(self.fg_context())
            self.emit(rec, f"key    {ev['key']}")

    def handle_click(self, ev):
        x, y = ev["x"], ev["y"]
        ctrl = auto.ControlFromPoint(x, y)
        if ctrl is None:
            print(f"Warning: nothing found at ({x}, {y})")
            return
        try:
            top = ctrl.GetTopLevelControl()
        except Exception:
            top = None

        app = None
        try:
            hwnd = top.NativeWindowHandle if top else 0
            if hwnd:
                app = app_of_hwnd(int(hwnd))[0]
        except Exception:
            app = None
        if not app:
            app = proc_name(ctrl.ProcessId)
        if top is not None and top.Name and app.lower() != FRAME_HOST:
            self.title_app[top.Name] = app

        def rel_in(control):
            try:
                r = control.BoundingRectangle
                w, h = r.width(), r.height()
                if w > 0 and h > 0:
                    return [round((x - r.left) / w, 4), round((y - r.top) / h, 4)]
            except Exception:
                pass
            return None

        name = ctrl.Name or ""
        aid = ctrl.AutomationId or ""
        rec = {
            "t": ev["t"],
            "action": "click",
            "button": ev["button"],
            "x": x,
            "y": y,
            "app": app,
            "window_title": top.Name if top else "",
            "window_class": top.ClassName if top else "",
            "rel_in_window": rel_in(top) if top else None,
            "target": {
                "name": name,
                "control_type": ctrl.ControlTypeName,
                "automation_id": aid,
                "class_name": ctrl.ClassName,
                "rel_pos": rel_in(ctrl),
                "parent_name": nearest_named_parent(ctrl) if not name.strip() else "",
            },
        }
        if not name.strip() and not aid.strip():
            crop = self.save_crop(x, y)  
            if crop:
                rec["crop"] = crop
        self.emit(rec, f"click  [{ctrl.ControlTypeName}] '{name}' in {app}")
        self.pending.append((ev["t"] + STATE_DELAY, rec, x, y))

    def process_pending(self, now):
        still = []
        for item in self.pending:
            due, rec, x, y = item
            if now < due:
                still.append(item)
                continue
            try:
                ctrl = auto.ControlFromPoint(x, y)
                t = rec["target"]
                if (
                    ctrl
                    and (ctrl.Name or "") == t["name"]
                    and (ctrl.AutomationId or "") == t["automation_id"]
                    and ctrl.ControlTypeName == t["control_type"]
                ):
                    st = read_state(ctrl)
                    if st:
                        rec["state_after"] = st
            except Exception:
                pass
        self.pending = still

    # -- window lifecycle ------------------------------------------------
    def poll_windows(self, now):
        cur = list_windows()

        if self.prev is None:  
            self.prev = {}
            start = []
            for hwnd, info in cur.items():
                app, exe, _ = resolve_app(hwnd, info["pid"])
                self.prev[hwnd] = {"app": app, "title": info["title"], "minimized": info["minimized"],
                                   "class": info["class"]}
                start.append({"app": app, "title": info["title"], "window_class": info["class"]})
            self.emit({"t": now, "action": "session_start", "windows": start, "host_apps": HOST_APPS})
            print(f"(Ignoring the program that hosts this assistant: {', '.join(HOST_APPS) or 'none found'})")
            return

        for hwnd, info in cur.items():
            if hwnd not in self.prev:
                app, exe, ok = resolve_app(hwnd, info["pid"])
                if not ok:  
                    n = self.unresolved.get(hwnd, 0) + 1
                    self.unresolved[hwnd] = n
                    if n < 5:
                        continue
                self.unresolved.pop(hwnd, None)
                self.prev[hwnd] = {"app": app, "title": info["title"], "minimized": info["minimized"],
                                   "class": info["class"]}
                if app.lower() != FRAME_HOST:
                    self.title_app[info["title"]] = app
                app = self.fix_app(app, info["title"])
                self.emit(
                    {"t": now, "action": "window_opened", "app": app, "title": info["title"],
                     "window_class": info["class"], "exe_path": exe},
                    f"window opened: {info['title']!r} ({app})",
                )
            else:
                old = self.prev[hwnd]
                if old["app"].lower() == FRAME_HOST:   
                    app2, _, ok2 = resolve_app(hwnd, info["pid"])
                    old["app"] = app2 if ok2 else self.fix_app(old["app"], info["title"])
                if info["minimized"] != old["minimized"]:
                    act = "window_minimized" if info["minimized"] else "window_restored"
                    self.emit(
                        {"t": now, "action": act, "app": old["app"], "title": info["title"],
                         "window_class": old["class"]},
                        f"{act.replace('_', ' ')}: {info['title']!r} ({old['app']})",
                    )
                    old["minimized"] = info["minimized"]
                old["title"] = info["title"]

        for hwnd in list(self.prev):
            if hwnd not in cur:
                old = self.prev.pop(hwnd)
                old["app"] = self.fix_app(old["app"], old["title"])
                self.emit(
                    {"t": now, "action": "window_closed", "app": old["app"], "title": old["title"],
                     "window_class": old["class"]},
                    f"window closed: {old['title']!r} ({old['app']})",
                )

        fg = user32.GetForegroundWindow()
        if fg and int(fg) != self.last_fg:
            self.last_fg = int(fg)
            try:
                app = app_of_hwnd(int(fg))[0]
                self.emit({"t": now, "action": "window_activated", "app": app,
                           "title": window_text(fg), "window_class": window_class(fg)})
            except Exception:
                pass

    # -- main loop -------------------------------------------------------
    def run(self):
        with auto.UIAutomationInitializerInThread(debug=False):
            while True:
                try:
                    ev = events_q.get(timeout=0.1)
                except queue.Empty:
                    ev = TICK
                if ev is None:
                    break
                now = time.time()
                if ev is not TICK:
                    try:
                        self.handle(ev)
                    except Exception as exc:
                        print(f"Warning: could not process {ev.get('kind')} event: {exc}")
                try:
                    self.process_pending(now)
                    if now - self.last_poll >= POLL_SECONDS:
                        self.last_poll = now
                        self.poll_windows(now)
                except Exception as exc:
                    print(f"Warning: background check failed: {exc}")
            self.process_pending(float("inf"))


# ---------------------------------------------------------------------------
def main():
    print("=== Session Recorder v3 ===")
    print("Use your computer normally: open apps, close them, click around, in any order.")
    print("F7 = pause/resume (use before typing anything private)")
    print("F8 = stop and save\n")

    worker = Worker()
    wt = threading.Thread(target=worker.run, daemon=True)
    wt.start()

    ml = mouse.Listener(on_click=on_click)
    ml.start()
    try:
        with keyboard.Listener(on_press=on_press, on_release=on_release) as kl:
            kl.join()
    finally:
        # runs on F8, Ctrl+C or an error: whatever was recorded is still saved
        ml.stop()
        events_q.put(None)
        wt.join(timeout=15)
        records = sorted(worker.records, key=lambda e: e["t"])
        save_json(OUTPUT_FILE, records)
        print(f"\nSaved {len(records)} events to '{OUTPUT_FILE}'.")


if __name__ == "__main__":
    main()