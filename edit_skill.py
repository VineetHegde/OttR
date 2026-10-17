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

from common import describe_step, parse_steps, save_json, slug

SKILLS_DIR = "skills"


def show(skill):
    print(f"Name: {skill.get('name')}")
    print(f"Description: {skill.get('description')}")
    for i, s in enumerate(skill.get("steps", []), 1):
        print(f"  {i}. {describe_step(s)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("skill")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--keep")
    ap.add_argument("--name")
    ap.add_argument("--description")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if os.path.basename(args.skill) != args.skill:
        print("Give the skill name only (no folders).")
        return
    old_path = os.path.join(SKILLS_DIR, f"{args.skill}.json")
    if not os.path.exists(old_path):
        print(f"Not found: {old_path}")
        return
    with open(old_path, "r", encoding="utf-8") as f:
        skill = json.load(f)

    if args.show or not (args.keep or args.name or args.description):
        show(skill)
        return

    if args.keep:
        try:
            keep = parse_steps(args.keep)
        except ValueError as exc:
            print(f"--keep: {exc}. Nothing changed.")
            return
        steps = [s for i, s in enumerate(skill.get("steps", []), 1) if i in keep]
        if not steps:
            print("No steps would be left. Nothing changed.")
            return
        skill["steps"] = steps
    if args.description:
        skill["description"] = args.description.strip()
    if args.name:
        skill["name"] = slug(args.name) or skill["name"]

    new_path = os.path.join(SKILLS_DIR, f"{skill['name']}.json")
    if new_path != old_path and os.path.exists(new_path) and not args.force:
        print(f"'{new_path}' already exists. Delete it first or add --force.")
        return

    save_json(new_path, skill)
    if new_path != old_path:
        os.remove(old_path)
    print(f"Saved {new_path} with {len(skill['steps'])} steps:")
    for i, s in enumerate(skill["steps"], 1):
        print(f"  {i}. {describe_step(s)}")


if __name__ == "__main__":
    main()
