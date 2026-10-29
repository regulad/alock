#!/usr/bin/env python3
"""A stand-in for `git lfs` lock, unlock and locks, keeping locks as
directories under $FAKE_LFS_DIR. Tests point ALOCK_GIT_LFS at it."""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote

STORE = Path(os.environ["FAKE_LFS_DIR"])


def all_locks():
    found = []
    for entry in sorted(STORE.iterdir()) if STORE.is_dir() else []:
        try:
            found.append(json.loads((entry / "lock.json").read_text()))
        except (FileNotFoundError, ValueError):
            found.append({"id": "pending", "path": unquote(entry.name)})
    return found


def main(args):
    cmd = args[0]
    if cmd == "lock":
        path = args[-1]
        STORE.mkdir(parents=True, exist_ok=True)
        entry = STORE / quote(path, safe="")
        try:
            entry.mkdir()
        except FileExistsError:
            print(f"Locking {path} failed: Lock exists", file=sys.stderr)
            print("[]")
            return 2
        lock = {
            "id": uuid.uuid4().hex[:8],
            "path": path,
            "owner": {"name": "tester"},
            "locked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = entry / "lock.json.tmp"
        tmp.write_text(json.dumps(lock))
        tmp.rename(entry / "lock.json")
        print(json.dumps([{k: lock[k] for k in ("id", "path", "locked_at")}]))
        return 0
    if cmd == "unlock":
        lock_id = args[args.index("--id") + 1]
        for entry in STORE.iterdir() if STORE.is_dir() else []:
            try:
                lock = json.loads((entry / "lock.json").read_text())
            except (FileNotFoundError, ValueError):
                continue
            if lock["id"] == lock_id:
                (entry / "lock.json").unlink()
                entry.rmdir()
                print(json.dumps([{"id": lock_id, "unlocked": True}]))
                return 0
        print(f"Unable to unlock: no lock with id {lock_id}", file=sys.stderr)
        return 2
    if cmd == "locks":
        path = next((a.split("=", 1)[1] for a in args if a.startswith("--path=")), None)
        locks = [l for l in all_locks() if path is None or l["path"] == path]
        print(json.dumps(locks))
        return 0
    print(f"fake git lfs: unknown command {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
