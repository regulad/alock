"""/alock:break: force-unlock locks whose holders are gone. The user runs it;
its output goes to the agent with the command's instructions."""

from __future__ import annotations

import os

from . import locks, records
from .common import AlockError, find_repo, mode_problem, split_target

USAGE = (
    "Usage: /alock:break [-C <repository>] <target>... where a target is tf/<root> "
    "or host/<name>. Without -C, the repository is the session's working directory."
)


def main(args: list[str]) -> int:
    # Always exit 0: a failing command aborts the user's /alock:break, and the
    # agent should see what happened instead.
    if not args or args[0] != "--from-skill":
        print("alock: break runs only as the user's /alock:break command.")
        return 0
    args = args[1:]
    directory = os.getcwd()
    if len(args) >= 2 and args[0] == "-C":
        directory = os.path.expanduser(args[1])
        args = args[2:]
    wanted = [a.lstrip("+") for a in args if a.strip()]
    if not wanted:
        print(USAGE)
        return 0
    try:
        repo = find_repo(directory) if os.path.isdir(directory) else None
    except AlockError as e:
        print(e)
        return 0
    if repo is None:
        print(f"alock: {directory} isn't in a git repository, so there's nothing to break.\n{USAGE}")
        return 0
    if not repo.configured:
        print(
            f"alock: {repo.top} has no .alock.json, so alock takes no locks there and "
            f"there's nothing to break. Name the repository that holds the lock with "
            f"-C.\n{USAGE}"
        )
        return 0
    problem = mode_problem(repo)
    if problem or repo.mode == "bypass":
        print(f"alock: {problem or 'alock.mode is bypass'}, so no locks are taken and none can be broken.")
        return 0
    for target in wanted:
        try:
            split_target(target)
            print(break_one(repo, target))
        except AlockError as e:
            print(f"alock: {target}: {e}")
    return 0


def break_one(repo, target: str) -> str:
    holder = locks.read_holder(repo, target)
    found = records.records(repo, target)
    record = records.describe_commit(repo, found[0][1]) if found else "no recorded commit"
    who = holder.describe() if holder else "no holder file in this clone"
    if repo.mode == "local":
        backend = locks.LocalBackend(repo)
        if not backend.is_held(target):
            locks.remove_holder(repo, target)
            return f"{target}: not locked. Nothing to break."
        return (
            f"{target}: held in local mode by {who}. A local lock lasts exactly as long as "
            "the processes holding it, so ending them releases it; alock doesn't kill "
            "processes for you. Nothing was broken."
        )
    backend = locks.LfsBackend(repo)
    lock = backend.lock_for(target)
    if lock is None:
        locks.remove_holder(repo, target)
        return f"{target}: not locked. Nothing to break."
    backend.release(str(lock["id"]), force=True)
    locks.remove_holder(repo, target)
    owner = (lock.get("owner") or {}).get("name", "unknown")
    return (
        f"{target}: BROKEN. It was LFS lock {lock['id']}, owned by {owner} since "
        f"{lock.get('locked_at')}; holder: {who}. Its record is kept: last applied from "
        f"{record}."
    )
