#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""askmate skill publisher: build per-skill zips + manifests, upload to the
self-hosted server's dist/ directory (the CLI self-upgrade channel).

The dist layout per skill:
    dist/<skill>.zip    SKILL.md (from skills/<skill>/) + askme.py + askme_gh.py (from cli/)
    dist/<skill>.json   {"skill", "version", "changeDesc", "releasedAt", "history": [...]}

Usage:
    python scripts/publish.py --version 1.3.0 --desc "..." --host myserver
    python scripts/publish.py --version 1.3.0 --desc "..." --local   # build dist/ only
"""
import argparse
import json
import os
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = ("ask-partner", "answer-partner")
PY_FILES = ("askme.py", "askme_gh.py")
DIST = os.path.join(HERE, "dist")


def version_tuple(v):
    try:
        return tuple(int(p) for p in str(v or "").split("."))
    except ValueError:
        return (0,)


def build_zip(skill, version):
    md_path = os.path.join(HERE, "skills", skill, "SKILL.md")
    if not os.path.isfile(md_path):
        sys.exit("✗ missing %s" % md_path)
    zip_path = os.path.join(DIST, skill + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        with open(md_path, "rb") as f:
            z.writestr("%s/SKILL.md" % skill, f.read())
        for name in PY_FILES:
            with open(os.path.join(HERE, "cli", name), "rb") as f:
                z.writestr("%s/%s" % (skill, name), f.read())
    return zip_path


def update_manifest(skill, version, desc):
    path = os.path.join(DIST, skill + ".json")
    manifest = {"skill": skill, "version": version, "changeDesc": desc,
                "releasedAt": time.strftime("%Y-%m-%d %H:%M"), "history": []}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            manifest["history"] = json.load(f).get("history") or []
        if version_tuple(version) <= max(
                [version_tuple(h.get("version")) for h in manifest["history"]] or [(0,)]):
            sys.exit("✗ %s: version %s must be greater than the latest history entry" % (skill, version))
    manifest["history"].insert(0, {"version": version, "changeDesc": desc,
                                   "releasedAt": manifest["releasedAt"]})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return path


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="askmate skill publisher (zip + manifest + upload)")
    p.add_argument("--version", required=True, help="e.g. 1.3.0; must be strictly greater than the previous")
    p.add_argument("--desc", required=True, help="changelog line shown to users on upgrade")
    p.add_argument("--host", default=os.environ.get("ASKME_PUBLISH_HOST"),
                   help="ssh host alias of the server running askmate (required unless --local)")
    p.add_argument("--remote-user", default=os.environ.get("ASKME_PUBLISH_USER"),
                   help="ssh user (default: your current user)")
    p.add_argument("--remote-dir", default=os.environ.get("ASKME_PUBLISH_DIR", "~/askmate/dist"),
                   help="remote dist directory (default ~/askmate/dist, matching server/deploy.sh)")
    p.add_argument("--local", action="store_true", help="build dist/ only, no upload")
    args = p.parse_args()

    if not args.local and not args.host:
        p.error("--host (or ASKME_PUBLISH_HOST) is required unless --local")

    os.makedirs(DIST, exist_ok=True)
    made = []
    for skill in SKILLS:
        made.append(build_zip(skill, args.version))
        made.append(update_manifest(skill, args.version, args.desc))
    for m in made:
        print("✓", os.path.relpath(m, HERE))

    if not args.local:
        target = "%s@%s" % (args.remote_user, args.host) if args.remote_user else args.host
        rdir = args.remote_dir
        subprocess.run(["ssh", target, "mkdir -p %s" % rdir], check=True)
        subprocess.run(["scp", "-q"] + made + ["%s:%s/" % (target, rdir)], check=True)
        print("✓ uploaded to %s:%s" % (target, rdir))
        r = subprocess.run(["ssh", target,
                            "curl -s 'http://127.0.0.1:8730/api/cli/version?skill=ask-partner'"],
                           capture_output=True, text=True)
        print("server check:", (r.stdout or r.stderr).strip())
    else:
        print("(--local: not uploaded)")


if __name__ == "__main__":
    main()
