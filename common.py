"""
common.py - small helpers shared by every module.

Pure Python on purpose (no Windows imports), so it can be imported and tested anywhere.
Before this file existed, slug / clean_label / parse_steps / describe were copy-pasted
into 3-4 modules and had started to drift apart.
"""
import html
import json
import os
import re
import time

# One place to change the local model. Override without editing code:
#   set ASSISTANT_MODEL=llama3.2:3b
MODEL = os.environ.get("ASSISTANT_MODEL", "llama3.2:1b")


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------
def slug(text, limit=40):
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:limit]


def clean_label(name):
    """Strip changing suffixes such as ' - 2 running windows' or ' - Memory usage - 120 MB'."""
    name = re.sub(r"\s*-\s*\d+\s+running\s+windows?.*$", "", name or "")
    name = re.sub(r"\s*-\s*Memory usage\s*-\s*[\d.,]+\s*[KMGT]?B.*$", "", name, flags=re.I)
    return name.strip()


def norm(text):
    return "".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def stem(w):
    """Very small plural-stripper: searches->search, files->file."""
    w = w.replace("colour", "color")
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokenize(text):
    return {stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower())}


def parse_steps(text):
    """'1,4' or '2-5,9' -> {1, 4} / {2, 3, 4, 5, 9}.  Raises ValueError with a readable message."""
    chosen = set()
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                a, b = int(a), int(b)
                if a > b:
                    a, b = b, a
                chosen.update(range(a, b + 1))
            else:
                chosen.add(int(part))
        except ValueError:
            raise ValueError(f"'{part}' is not a step number or a range like 2-5") from None
    return chosen


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def describe_step(s, detailed=False):
    """One readable line for any kind of step (element click, position click, typing, key, password)."""
    a = s.get("action", "?")
    if a.endswith("_element"):
        t = s.get("target") or {}
        label = t.get("name") or t.get("automation_id") or "(unnamed element)"
        if not t.get("name") and t.get("parent_name"):
            label += f" in {t['parent_name']}"
        extra = f" ({t['control_type']})" if detailed and t.get("control_type") else ""
        return f"{a} '{label}'{extra} in {s.get('app') or '?'}"
    if a.endswith("_position"):
        rel = s.get("rel_in_window")
        where = f" at {rel[0]:.0%},{rel[1]:.0%} of the window" if rel and len(rel) == 2 else ""
        return f"{a}{where} in {s.get('app') or '?'}"
    if a == "type_text":
        return f"type {s.get('text')!r}"
    if a == "press_key":
        times = f" x{s['repeat']}" if s.get("repeat") else ""
        return f"press {s.get('key')}{times}"
    if a == "enter_password":
        return "enter password (asked at run time)"
    return a


def read_state(ctrl):
    """State of a UI Automation control that has one (selected / toggled / expanded)."""
    st = {}
    try:
        p = ctrl.GetSelectionItemPattern()
        if p:
            st["selected"] = bool(p.IsSelected)
    except Exception:
        pass
    try:
        p = ctrl.GetTogglePattern()
        if p:
            st["toggle"] = int(p.ToggleState)
    except Exception:
        pass
    try:
        p = ctrl.GetExpandCollapsePattern()
        if p:
            st["expanded"] = int(p.ExpandCollapseState)
    except Exception:
        pass
    return st


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------
def save_json(path, data):
    """Write JSON atomically so a crash never leaves a half-written skill/profile."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# flowchart pages (Mermaid)
# ---------------------------------------------------------------------------
_MERMAID_ESCAPES = (("#", "#35;"), ("&", "#38;"), ('"', "#34;"), ("<", "#60;"), (">", "#62;"))


def mermaid_label(text, limit=120):
    """Make arbitrary text safe inside a quoted Mermaid label (and inside the HTML around it)."""
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[: limit - 1] + "..."
    for ch, code in _MERMAID_ESCAPES:
        text = text.replace(ch, code)
    return text


def write_flowchart_html(folder, prefix, title, mermaid_lines):
    """Save a Mermaid diagram as a small HTML page. Returns the path, or None on failure.

    The page needs internet for the Mermaid script; the diagram source is also included
    in a collapsed block so it is still readable offline.
    """
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.html")
        code = html.escape("\n".join(mermaid_lines), quote=False)
        page = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{html.escape(title)}</title>
    <script type="module">
        import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
        mermaid.initialize({{ startOnLoad: true, theme: 'dark' }});
    </script>
</head>
<body style="background-color: #1e1e1e; color: #ffffff; font-family: sans-serif; padding: 2rem;">
    <h2>{html.escape(title)}</h2>
    <pre class="mermaid">
{code}
    </pre>
    <details style="margin-top: 2rem;">
        <summary>Diagram source (readable offline)</summary>
        <pre>{code}</pre>
    </details>
</body>
</html>"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        return path
    except Exception as exc:
        print(f"(Could not write flowchart: {exc})")
        return None
