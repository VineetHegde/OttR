"""
edit_skill.py - tidy a saved skill without editing JSON by hand.

  python edit_skill.py good_skill --show
  python edit_skill.py good_skill --keep 1,4 --name open_home_files --description "Opens the Home page in the Files app"

--keep        step numbers to keep (e.g. 1,4 or 1-3). Others are removed.
--name        new skill name (the file is renamed to match; the old file is removed).
--description new description.
--force       allow overwriting an existing skill that has the new name.
"""
import argparse
import json
import os
import re

SKILLS_DIR = "skills"


def summarize(s):
    a = s["action"]
    if a.endswith("click_element"):
        t = s["target"]
        return f"{a} '{t.get('name') or t.get('automation_id')}' in {s.get('app')}"
    if a == "type_text":
        return f"type {s['text']!r}"
    if a == "press_key":
        times = f" x{s['repeat']}" if s.get("repeat") else ""
        return f"press {s['key']}{times}"
    return a


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


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("skill")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--keep")
    ap.add_argument("--name")
    ap.add_argument("--description")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    old_path = os.path.join(SKILLS_DIR, f"{args.skill}.json")
    if not os.path.exists(old_path):
        print(f"Not found: {old_path}")
        return
    with open(old_path, "r", encoding="utf-8") as f:
        skill = json.load(f)

    if args.show or not (args.keep or args.name or args.description):
        print(f"Name: {skill.get('name')}")
        print(f"Description: {skill.get('description')}")
        for i, s in enumerate(skill.get("steps", []), 1):
            print(f"  {i}. {summarize(s)}")
        return

    if args.keep:
        keep = parse_steps(args.keep)
        skill["steps"] = [s for i, s in enumerate(skill["steps"], 1) if i in keep]
        if not skill["steps"]:
            print("No steps would be left. Nothing changed.")
            return
    if args.description:
        skill["description"] = args.description.strip()
    if args.name:
        skill["name"] = slug(args.name) or skill["name"]

    new_path = os.path.join(SKILLS_DIR, f"{skill['name']}.json")
    if new_path != old_path and os.path.exists(new_path) and not args.force:
        print(f"'{new_path}' already exists. Delete it first or add --force.")
        return

    with open(new_path, "w", encoding="utf-8") as f:
        json.dump(skill, f, indent=2, ensure_ascii=False)
    if new_path != old_path:
        os.remove(old_path)
    print(f"Saved {new_path} with {len(skill['steps'])} steps:")
    for i, s in enumerate(skill["steps"], 1):
        print(f"  {i}. {summarize(s)}")


if __name__ == "__main__":
    main()