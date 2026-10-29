"""/alock:retrofit: note the commits applied before alock followed rebases, so
rebasing them from now on keeps their records. The user runs it once per
clone; its output goes to the agent with the command's instructions."""

from __future__ import annotations

import os

from . import records
from .common import AlockError, Repo, find_repo, git, mode_problem

USAGE = (
    "Usage: /alock:retrofit [-C <repository>]. Without -C, the repository is the "
    "session's working directory."
)


def main(args: list[str]) -> int:
    # Always exit 0: a failing command aborts the user's /alock:retrofit, and
    # the agent should see what happened instead.
    directory = os.getcwd()
    if len(args) >= 2 and args[0] == "-C":
        directory = os.path.expanduser(args[1])
        args = args[2:]
    if args:
        print(USAGE)
        return 0
    try:
        repo = find_repo(directory) if os.path.isdir(directory) else None
        if repo is None:
            print(f"alock: {directory} isn't in a git repository, so there's nothing to retrofit.\n{USAGE}")
        elif not repo.configured:
            print(
                f"alock: {repo.top} has no .alock.json, so alock keeps no records there and "
                f"there's nothing to retrofit. Name the repository with -C.\n{USAGE}"
            )
        elif mode_problem(repo) or repo.mode == "bypass":
            print(
                f"alock: {mode_problem(repo) or 'alock.mode is bypass'}, so alock keeps no "
                "records and there's nothing to retrofit."
            )
        else:
            print(retrofit(repo))
    except AlockError as e:
        print(e)
    return 0


def retrofit(repo: Repo) -> str:
    if repo.mode == "remote":
        records.fetch_origin(repo)
    missing = records.unnoted(repo)
    if not missing:
        return f"alock: every record in {repo.top} is already noted. Nothing to do."
    heads = list(dict.fromkeys(sha for _, sha in missing))
    # A noted record's commits were noted when it was applied.
    noted = {sha for _, sha in records.records_in(repo)} - set(heads)
    brought = git(
        "rev-list", f"--max-count={records.NOTE_LIMIT}", *heads, "--not", *noted, cwd=repo.top,
    ).stdout.split()
    written = records.note_applied(repo, heads + brought)
    lines = [
        f"alock: noted {written} commits in {repo.top}: the commits these records were "
        f"applied from, and the commits they brought (up to the most recent "
        f"{records.NOTE_LIMIT}):"
    ]
    lines += [f"  {ref}: {records.describe_commit(repo, sha)}" for ref, sha in missing]
    lines.append(
        "From now on, rebasing or amending them in an alock session keeps their records. "
        "A rewrite made before now can't be linked: if an apply is refused over one, "
        "follow the refusal's message."
    )
    if repo.mode == "remote":
        lines.append(
            "Notes stay in the clone that writes them: run /alock:retrofit in each clone "
            "whose agents rebase applied commits."
        )
    return "\n".join(lines)
