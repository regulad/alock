"""Repository discovery, git helpers, configuration and messages."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

MODES = ("local", "remote", "bypass")
REMOTE = "origin"
CONFIG_FILE = ".alock.json"

# Exit codes for a call alock refuses without running anything.
EXIT_TIMEOUT = 75  # a lock stayed held past the timeout
EXIT_REFUSED = 77  # a rule refused the call: dirty worktree, missing record...
EXIT_CONFIG = 78  # alock.mode or .alock.json needs the user's attention

DEFAULT_TIMEOUT = 60.0

# Command-scoped config overrides: `git -c` travels in GIT_CONFIG_PARAMETERS,
# and GIT_CONFIG_COUNT with GIT_CONFIG_KEY_<n>/GIT_CONFIG_VALUE_<n> set more.
# The mode is read without them, so one command can't choose its own mode.


def config_free_env(env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if env is None else env)
    for name in list(env):
        if name in ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT") or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            del env[name]
    return env


class AlockError(Exception):
    """A refusal: the call doesn't run, and the message goes to stderr."""

    def __init__(self, message: str, code: int = EXIT_REFUSED):
        super().__init__(message)
        self.code = code


def git(
    *args: str,
    cwd: str | Path,
    check: bool = True,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        input=input,
        text=True,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise AlockError(
            f"git {' '.join(args)} failed ({proc.returncode}):\n{proc.stderr.strip()}"
        )
    return proc


def network_env() -> dict[str, str]:
    """Environment for git calls that reach the remote: never prompt on a TTY."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


@dataclass
class Repo:
    top: Path  # the worktree's top level
    common: Path  # the git directory every worktree of this clone shares
    config: dict | None  # .alock.json, or None when the repository has none
    mode: str | None  # alock.mode, None when unset
    raw_mode: str | None = None  # the value as set, for naming an invalid one

    @property
    def configured(self) -> bool:
        return self.config is not None

    @property
    def state_dir(self) -> Path:
        return self.common / "alock"

    def head(self) -> str:
        proc = git("rev-parse", "--verify", "-q", "HEAD^{commit}", cwd=self.top, check=False)
        if proc.returncode != 0:
            raise AlockError("alock: this worktree has no commit to apply from.")
        return proc.stdout.strip()

    def branch(self) -> str:
        proc = git("symbolic-ref", "--short", "-q", "HEAD", cwd=self.top, check=False)
        return proc.stdout.strip() or "(detached HEAD)"


def find_repo(directory: str | Path) -> Repo | None:
    proc = git(
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-common-dir",
        cwd=directory,
        check=False,
    )
    if proc.returncode != 0:
        return None
    lines = proc.stdout.splitlines()
    if len(lines) < 2:
        return None  # a bare repository has no worktree to apply from
    top, common = Path(lines[0]), Path(lines[1])
    config = None
    config_path = top / CONFIG_FILE
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text())
        except ValueError as e:
            raise AlockError(f"alock: {config_path} isn't valid JSON: {e}", EXIT_CONFIG)
        if not isinstance(config, dict):
            raise AlockError(f"alock: {config_path} must hold a JSON object.", EXIT_CONFIG)
    raw = read_mode(top)
    mode = raw if raw in MODES else None
    return Repo(top=top, common=common, config=config, mode=mode, raw_mode=raw)


def read_mode(directory: str | Path) -> str | None:
    proc = git(
        "config", "--get", "alock.mode", cwd=directory, check=False, env=config_free_env()
    )
    value = proc.stdout.strip()
    return value or None


def mode_problem(repo: Repo) -> str | None:
    """Why the mode stops alock from working, or None when it's set."""
    if repo.mode is not None:
        return None
    if repo.raw_mode is None:
        return "alock.mode isn't set"
    return f"alock.mode is {repo.raw_mode!r}, which isn't local, remote or bypass"


SET_MODE_HINT = (
    "Only the user can set it, in their own terminal:\n"
    "  git config --global alock.mode local    # every agent applies from one clone\n"
    "  git config --global alock.mode remote   # agents apply from several clones or machines\n"
    "  git config --global alock.mode bypass   # no locking or checks at all\n"
    "(Leave out --global to set it for one clone.)"
)

# Whom an agent tells about a broken lock or a record moved forward: only the
# agents working on this repository, not every session the harness can reach.
PEERS = (
    "the other agents working on this repository (in Claude Code: the sessions "
    "ListAgents returns that work in this repository, its worktrees or its clones, "
    "through SendMessage; not unrelated sessions)"
)


# Targets are "tf/<root path>" and "host/<inventory hostname>". Lock files and
# refs need one path component per name, so a name is percent-encoded there.


def encode_name(name: str) -> str:
    encoded = quote(name, safe="-_.")
    encoded = encoded.replace("..", ".%2E")
    if encoded.startswith("."):
        encoded = "%2E" + encoded[1:]
    if encoded.endswith(".lock"):
        encoded = encoded[: -len(".lock")] + "%2Elock"
    return encoded


def split_target(target: str) -> tuple[str, str]:
    kind, _, name = target.partition("/")
    if kind not in ("tf", "host") or not name:
        raise AlockError(f"alock: {target!r} isn't a target (tf/<root> or host/<name>).")
    return kind, name


def target_key(target: str) -> str:
    kind, name = split_target(target)
    return f"{kind}/{encode_name(name)}"


def machine() -> str:
    return socket.gethostname()


def warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@dataclass
class Holder:
    """Who holds a lock, as recorded in this clone's holder file."""

    target: str
    session: str
    machine: str
    worktree: str
    branch: str
    head: str
    command: list[str]
    taken_at: str
    pids: list[dict] = field(default_factory=list)  # {"pid": int, "start": str}
    lfs_id: str | None = None

    def describe(self) -> str:
        pids = ", ".join(str(p["pid"]) for p in self.pids) or "none recorded"
        return (
            f"session {self.session} on {self.machine}, worktree {self.worktree} "
            f"({self.branch}), since {self.taken_at}, pids {pids}, "
            f"running: {' '.join(self.command)}"
        )
