"""What a shim does: pass a call through, or lock, check, record and run it."""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

from . import locks, records, targets
from .common import (
    DEFAULT_TIMEOUT,
    EXIT_CONFIG,
    EXIT_REFUSED,
    PEERS,
    SET_MODE_HINT,
    AlockError,
    Repo,
    find_repo,
    git,
    machine,
    mode_problem,
    warn,
)

BIN_DIR = Path(__file__).resolve().parent.parent.parent / "bin"


def find_real(tool: str) -> str | None:
    """The first tool on PATH after the shims' own directory."""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            if Path(entry).resolve() == BIN_DIR:
                continue
        except OSError:
            continue
        candidate = Path(entry) / tool
        if candidate.is_file() and os.access(candidate, os.X_OK):
            if candidate.resolve().parent == BIN_DIR:
                continue
            return str(candidate)
    return None


def timeout() -> float:
    try:
        return max(0.0, float(os.environ.get("ALOCK_TIMEOUT", DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT


class Terminate(BaseException):
    """SIGTERM or SIGHUP before the child runs: unwind, releasing locks."""


def _terminate(signum, frame):
    raise Terminate(signum)


def make_blocking() -> None:
    """Ansible refuses to start when stdin, stdout or stderr is non-blocking,
    as Claude Code's Bash tool hands them over. Make them blocking first."""
    for fd in (0, 1, 2):
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass  # closed, or not a file this process may change


def main(tool: str, args: list[str]) -> int:
    real = find_real(tool)
    if real is None:
        warn(f"alock: there's no {tool} on PATH besides alock's shim.")
        return 127
    if tool == targets.ANSIBLE_PLAYBOOK:
        make_blocking()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _terminate)
    try:
        call = targets.classify(tool, args, Path.cwd())
        if not call.changing:
            os.execv(real, [tool, *args])
        repo = find_repo(call.directory)
        if repo is None or not repo.configured:
            os.execv(real, [tool, *args])
        problem = mode_problem(repo)
        if problem:
            raise AlockError(
                f"alock: {problem}, so this change in {repo.top} was refused and "
                f"didn't run.\n{SET_MODE_HINT}\nTell the user; you can't set it yourself.",
                EXIT_CONFIG,
            )
        if repo.mode == "bypass":
            warn(BYPASS_WARNING)
            os.execv(real, [tool, *args])
        return run_locked(repo, call, real)
    except AlockError as e:
        warn(str(e))
        return e.code
    except Terminate as t:
        return 128 + t.args[0]
    except KeyboardInterrupt:
        return 130


def refuse_dirty(repo: Repo) -> None:
    status = git("status", "--porcelain", "--untracked-files=normal", cwd=repo.top).stdout
    if status.strip():
        entries = status.splitlines()
        shown = "\n".join("  " + e for e in entries[:10])
        more = f"\n  ...and {len(entries) - 10} more" if len(entries) > 10 else ""
        raise AlockError(
            f"alock: {repo.top} has uncommitted changes, so this change was refused and "
            f"didn't run:\n{shown}{more}\nCommit them on your worktree branch, then apply "
            "from that commit.",
            EXIT_REFUSED,
        )


def check_plan(call: targets.Call, head: str) -> None:
    if call.plan_file is None:
        return
    match = re.search(r"@([0-9a-f]{7,64})\.tfplan$", call.plan_file.name)
    if not match or not head.startswith(match.group(1)):
        raise AlockError(
            f"alock: {call.plan_file.name} must be named <anything>@<commit>.tfplan "
            f"for the commit it was made from, which must be HEAD ({head[:12]}). It "
            "didn't run. Make the plan again from HEAD, named for it.",
            EXIT_REFUSED,
        )


def missing_records(repo: Repo, head: str, wanted: list[str]) -> list[str]:
    missing, checked = [], {}  # a root's hosts usually share one record
    for target in wanted:
        for ref, sha in records.records(repo, target):
            if sha not in checked:
                checked[sha] = records.contains(repo, head, sha)
            if not checked[sha]:
                missing.append(f"  {target}: {records.describe_commit(repo, sha)}  [{ref}]")
    return missing


BYPASS_WARNING = (
    "alock: alock.mode is bypass, so this change runs unchecked: no locks, no check "
    "for uncommitted changes, no check against the last applied commit, and it "
    "isn't recorded. What's live may end up matching no commit, with no trace in "
    "alock's records. Tell the user it ran unprotected."
)


def moved_note(moved: list[str], head: str) -> str:
    if len(moved) == 1:
        subject, verb, them = moved[0], "now records", "it"
    else:
        subject, verb, them = ", ".join(moved), "now record", "them"
    return (
        f"alock: {subject} {verb} {head[:12]} as the last apply. Agents in other "
        f"worktrees or clones can't apply to {them} until their HEAD contains this "
        f"commit. You're encouraged to tell {PEERS}, so they can rebase onto it."
    )


def run_locked(repo: Repo, call: targets.Call, real: str) -> int:
    refuse_dirty(repo)
    head = repo.head()
    check_plan(call, head)
    env = dict(os.environ)
    if call.tool in targets.TF:
        wanted = targets.tf_targets(repo, call, env)
    else:
        wanted = targets.ansible_targets(real, call, env)
    if not wanted:
        os.execv(real, [call.tool, *call.args])

    backend = locks.backend_for(repo)
    holder_base = dict(
        session=os.environ.get("ALOCK_SESSION", "(no session)"),
        machine=machine(),
        worktree=str(repo.top),
        branch=repo.branch(),
        head=head,
        command=[call.tool, *call.args],
        pids=[locks.pid_entry(os.getpid())],
    )

    def first_record(target):
        found = records.records(repo, target)
        return found[0][1] if found else None

    acquired = locks.acquire(repo, backend, wanted, holder_base, timeout(), first_record)
    try:
        records.harvest(repo)
        origin = records.fetch_origin(repo, wanted) if repo.mode == "remote" else {}
        missing = missing_records(repo, head, wanted)
        if missing:
            raise AlockError(
                "alock: HEAD doesn't contain what was last applied to these targets, so "
                "applying it could undo someone's change. Nothing ran:\n"
                + "\n".join(missing)
                + "\nTo keep those changes: merge or rebase onto the branch they came from "
                "(usually: wait for it to reach master, then bring master in), commit, and "
                "apply again.\nIf HEAD already has them, but they were squashed, or rebased "
                "or amended where alock couldn't follow (outside an alock session, or in "
                "another clone that hasn't applied since), alock can't tell. If they were "
                "squashed, check out the squash commit and apply from it first; if that's "
                "refused too, apply from their branch's tip before it. Otherwise, make a "
                "scratch branch from HEAD, git merge the applied commit into it, resolving "
                "any conflicts toward what should be live, and apply from there. Either "
                "way, HEAD then contains what was last applied.\n"
                "To undo them on purpose: git revert the applied commits on a branch that "
                "contains them (merge their branch in first if it never landed), and apply "
                "the revert. That's alock's only way to roll back.",
                EXIT_REFUSED,
            )
        moved = [
            t for t in wanted
            if all(sha != head for _, sha in records.records(repo, t))
        ]
        records.mark_applied(repo, head)
        # Record before running: a run that fails part-way has still changed its targets.
        if repo.mode == "remote":
            records.push_records(repo, head, origin)
            olds = {}
            for target in wanted:
                olds[records.local_ref(target)] = records.read_ref(repo, records.local_ref(target))
                olds[records.origin_ref(target)] = records.read_ref(repo, records.origin_ref(target))
            records.update_local(repo, head, olds)
        else:
            olds = {records.local_ref(t): records.read_ref(repo, records.local_ref(t)) for t in wanted}
            records.update_local(repo, head, olds)
        code = run_child(repo, acquired, wanted, holder_base, real, call)
        if moved:
            warn(moved_note(moved, head))
        return code
    finally:
        try:
            acquired.release_all()
        except AlockError as e:
            warn(
                f"{e}\nalock: a lock wasn't released. This machine's next alock run "
                "releases it once its processes are gone; elsewhere the user runs "
                "/alock:break."
            )


def run_child(repo, acquired, wanted, holder_base, real, call) -> int:
    # The child shares the shim's process group, so a signal sent to the group
    # (a terminal's ^C, or a harness stopping the command) reaches it directly,
    # once. The shim only waits it out, so it can release the locks afterwards.
    def wait_out(signum, frame):
        pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(signum, wait_out)
    child = subprocess.Popen([real, *call.args], pass_fds=acquired.fds())
    for target in wanted:
        holder = locks.read_holder(repo, target)
        if holder is not None:
            holder.pids.append(locks.pid_entry(child.pid))
            locks.write_holder(repo, holder)
    while True:
        try:
            code = child.wait()
            break
        except InterruptedError:
            continue
    return 128 - code if code < 0 else code
