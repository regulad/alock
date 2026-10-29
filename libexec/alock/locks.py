"""Locks: flock in local mode, Git LFS locks in remote mode, and holder files."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .common import (
    EXIT_TIMEOUT,
    PEERS,
    AlockError,
    Holder,
    Repo,
    machine,
    network_env,
    target_key,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- processes -------------------------------------------------------------


_HAVE_PROC = Path("/proc/self/stat").exists()


def process_start(pid: int) -> str | None:
    """An identifier for when pid started, so a reused pid isn't taken as alive."""
    if _HAVE_PROC:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        # Field 22, counted after the parenthesised command name, which may hold spaces.
        return stat.rsplit(")", 1)[1].split()[19]
    proc = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], text=True, capture_output=True
    )
    return proc.stdout.strip() or None


def alive(pid_entry: dict) -> bool:
    start = process_start(pid_entry["pid"])
    return start is not None and start == pid_entry.get("start")


def pid_entry(pid: int) -> dict:
    return {"pid": pid, "start": process_start(pid)}


# --- holder files ----------------------------------------------------------


def holder_path(repo: Repo, target: str) -> Path:
    return repo.state_dir / "held" / f"{target_key(target)}.json"


def write_holder(repo: Repo, holder: Holder) -> None:
    path = holder_path(repo, holder.target)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(holder), indent=2) + "\n")
    os.replace(tmp, path)


def read_holder(repo: Repo, target: str) -> Holder | None:
    try:
        data = json.loads(holder_path(repo, target).read_text())
        return Holder(**data)
    except (FileNotFoundError, ValueError, TypeError):
        return None


def remove_holder(repo: Repo, target: str) -> None:
    holder_path(repo, target).unlink(missing_ok=True)


def all_holders(repo: Repo) -> list[Holder]:
    held = repo.state_dir / "held"
    holders = []
    for path in sorted(held.glob("*/*.json")) if held.is_dir() else []:
        try:
            holders.append(Holder(**json.loads(path.read_text())))
        except (ValueError, TypeError):
            continue
    return holders


# --- backends --------------------------------------------------------------


class LocalBackend:
    """flock on a file in the clone's common dir. A lock lives exactly as long
    as some process holds its descriptor, so a run can't leave one behind."""

    name = "local"

    def __init__(self, repo: Repo):
        self.repo = repo

    def path(self, target: str) -> Path:
        return self.repo.state_dir / "locks" / target_key(target)

    def try_acquire(self, target: str):
        path = self.path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        os.set_inheritable(fd, True)  # the real binary keeps the lock if the shim dies
        return fd

    def release(self, handle) -> None:
        try:
            os.close(handle)
        except OSError:
            pass

    def is_held(self, target: str) -> bool:
        """Probe without creating anything: a lock never taken isn't held."""
        try:
            fd = os.open(self.path(target), os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def lfs_id(self, handle) -> None:
        return None

    def describe_lock(self, target: str) -> str | None:
        return None


class LfsBackend:
    """Git LFS locks on synthetic paths, through the remote's locking API.
    ALOCK_GIT_LFS replaces `git lfs` with another command, for tests."""

    name = "remote"

    def __init__(self, repo: Repo):
        self.repo = repo
        override = os.environ.get("ALOCK_GIT_LFS")
        self.cmd = shlex.split(override) if override else ["git", "lfs"]

    @staticmethod
    def lock_path(target: str) -> str:
        return "+" + target

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # Paths are relative to the current directory inside the repository, so
        # every call runs from its top level.
        return subprocess.run(
            [*self.cmd, *args],
            cwd=self.repo.top,
            env=network_env(),
            text=True,
            capture_output=True,
        )

    def try_acquire(self, target: str):
        proc = self._run("lock", "--json", self.lock_path(target))
        if proc.returncode == 0:
            try:
                return str(json.loads(proc.stdout)[0]["id"])
            except (ValueError, LookupError, TypeError):
                raise AlockError(f"alock: unexpected output from git lfs lock: {proc.stdout!r}")
        if "exists" in proc.stderr.lower():
            return None
        raise AlockError(
            f"alock: git lfs lock {self.lock_path(target)} failed, so nothing ran:\n"
            f"{proc.stderr.strip()}"
        )

    def release(self, handle, force: bool = False) -> None:
        args = ["unlock", "--json", "--id", handle]
        if force:
            args.append("--force")
        proc = self._run(*args)
        if proc.returncode != 0:
            raise AlockError(f"alock: git lfs unlock --id {handle} failed:\n{proc.stderr.strip()}")

    def locks(self, target: str | None = None) -> list[dict]:
        args = ["locks", "--json"]
        if target is not None:
            args.append(f"--path={self.lock_path(target)}")
        proc = self._run(*args)
        if proc.returncode != 0:
            raise AlockError(f"alock: git lfs locks failed:\n{proc.stderr.strip()}")
        try:
            found = json.loads(proc.stdout or "[]")
        except ValueError:
            raise AlockError(f"alock: unexpected output from git lfs locks: {proc.stdout!r}")
        return [lock for lock in found if str(lock.get("path", "")).startswith("+")]

    def lock_for(self, target: str) -> dict | None:
        path = self.lock_path(target)
        return next((lock for lock in self.locks(target) if lock.get("path") == path), None)

    def is_held(self, target: str) -> bool:
        return self.lock_for(target) is not None

    def lfs_id(self, handle) -> str:
        return handle

    def describe_lock(self, target: str) -> str | None:
        lock = self.lock_for(target)
        if lock is None:
            return None
        owner = (lock.get("owner") or {}).get("name", "unknown")
        return f"LFS lock {lock.get('id')} owned by {owner} since {lock.get('locked_at')}"


def backend_for(repo: Repo):
    return LfsBackend(repo) if repo.mode == "remote" else LocalBackend(repo)


# --- acquisition -----------------------------------------------------------


def release_orphan(repo: Repo, backend, target: str) -> bool:
    """Release a remote lock left by a dead run of this clone on this machine."""
    if not isinstance(backend, LfsBackend):
        return False
    holder = read_holder(repo, target)
    if holder is None or holder.machine != machine() or not holder.lfs_id:
        return False
    if any(alive(p) for p in holder.pids):
        return False
    lock = backend.lock_for(target)
    if lock is None or str(lock.get("id")) != holder.lfs_id:
        return False
    backend.release(holder.lfs_id)
    remove_holder(repo, target)
    return True


def blocker_report(repo: Repo, backend, target: str, record: str | None) -> str:
    lines = [f"  {target}"]
    holder = read_holder(repo, target)
    if holder is not None:
        lines.append(f"    held by {holder.describe()}")
    lock = backend.describe_lock(target)
    if lock:
        lines.append(f"    {lock}")
    if holder is None and not lock:
        lines.append("    held by a process this clone has no record of")
    lines.append(f"    last applied from {record[:12] if record else 'no recorded commit'}")
    return "\n".join(lines)


class Acquired:
    def __init__(self, repo: Repo, backend):
        self.repo = repo
        self.backend = backend
        self.handles: dict[str, object] = {}

    def release_all(self) -> None:
        errors = []
        for target, handle in reversed(list(self.handles.items())):
            try:
                self.backend.release(handle)
            except AlockError as e:
                # The holder file stays, so this machine's next run finds the
                # lock's pids dead and releases it as an orphan.
                errors.append(str(e))
                continue
            remove_holder(self.repo, target)
        self.handles.clear()
        if errors:
            raise AlockError("\n".join(errors))

    def fds(self) -> list[int]:
        return [h for h in self.handles.values() if isinstance(h, int)]


def acquire(repo: Repo, backend, targets: list[str], holder_base: dict, timeout: float, records):
    """Take every lock in sorted order, waiting up to timeout. On timeout,
    release everything taken and refuse, naming each target still held."""
    acquired = Acquired(repo, backend)
    deadline = time.monotonic() + timeout
    ordered = sorted(set(targets))
    try:
        for index, target in enumerate(ordered):
            delay = 0.2
            while True:
                handle = backend.try_acquire(target)
                if handle is not None:
                    acquired.handles[target] = handle
                    write_holder(
                        repo,
                        Holder(
                            **{**holder_base, "taken_at": now()},
                            target=target,
                            lfs_id=backend.lfs_id(handle),
                        ),
                    )
                    break
                if release_orphan(repo, backend, target):
                    continue
                if time.monotonic() >= deadline:
                    blocked = [target] + [t for t in ordered[index + 1 :] if backend.is_held(t)]
                    acquired.release_all()
                    raise AlockError(timeout_message(repo, backend, blocked, timeout, records), EXIT_TIMEOUT)
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                delay = min(delay * 2, 5.0 if backend.name == "remote" else 1.0)
    except BaseException:
        acquired.release_all()
        raise
    return acquired


def timeout_message(repo: Repo, backend, blocked: list[str], timeout: float, records) -> str:
    reports = "\n".join(blocker_report(repo, backend, t, records(t)) for t in blocked)
    targets = " ".join(blocked)
    count = "1 lock" if len(blocked) == 1 else f"{len(blocked)} locks"
    return (
        f"alock: timed out after {timeout:g}s waiting for {count}, so nothing ran "
        f"and every lock this call took was released:\n{reports}\n\n"
        "What to do:\n"
        "  - Report this to the user, with the holders above.\n"
        "  - If a holder is still working, wait for it and try again later.\n"
        f"  - If a holder is gone, first message {PEERS}, naming the target, the "
        "holder and why you believe it is gone, so a live holder can object.\n"
        f"  - Then the user breaks the lock by running\n"
        f"      /alock:break -C {shlex.quote(str(repo.top))} {targets}\n"
        "    in a Claude Code session. You can't run it yourself."
    )
