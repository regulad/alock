"""Which calls change infrastructure, and which targets they change."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .common import EXIT_CONFIG, AlockError, Repo

TF = ("tofu", "terraform")
ANSIBLE_PLAYBOOK = "ansible-playbook"

# tf subcommands that change state or real infrastructure. `test` runs real
# applies unless its providers are mocked. Anything not listed in READ_ONLY
# counts as changing, so an unfamiliar subcommand fails closed.
TF_READ_ONLY = {
    "plan", "show", "output", "validate", "fmt", "init", "providers", "version",
    "graph", "console", "get", "modules", "metadata", "login", "logout", "help",
}
TF_STATE_READ_ONLY = {"list", "show", "pull"}
TF_WORKSPACE_READ_ONLY = {"list", "show", "select"}
TF_INIT_CHANGING_FLAGS = ("-migrate-state", "-force-copy")

# Flags that may take their value as the next argument, so it isn't positional.
TF_VALUE_FLAGS = {
    "-var", "-var-file", "-target", "-replace", "-exclude", "-lock-timeout",
    "-parallelism", "-state", "-state-out", "-backup", "-config", "-chdir",
}

# ansible-playbook options that make a run read-only.
ANSIBLE_READ_ONLY_FLAGS = {
    "--check", "-C", "--syntax-check", "--list-hosts", "--list-tasks", "--list-tags",
    "--help", "-h", "--version",
}
# Short options taking no value, which may be bundled with -C, as in -vC.
ANSIBLE_BUNDLABLE = set("vbKkCDh")

# Plays on the controller act on it, not on a host another run locks.
IMPLICIT_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass
class Call:
    tool: str  # "tofu", "terraform" or "ansible-playbook"
    args: list[str]
    cwd: Path
    changing: bool
    directory: Path  # the directory whose repository decides how alock treats the call
    subcommand: str | None = None
    plan_file: Path | None = None
    extra: dict = field(default_factory=dict)


def classify(tool: str, args: list[str], cwd: Path) -> Call:
    if tool in TF:
        return classify_tf(tool, args, cwd)
    if tool == ANSIBLE_PLAYBOOK:
        return classify_ansible(args, cwd)
    raise AlockError(f"alock: no shim for {tool!r}.")


def classify_tf(tool: str, args: list[str], cwd: Path) -> Call:
    chdir = None
    rest = list(args)
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag.startswith("-chdir="):
            chdir = flag.split("=", 1)[1]
        elif flag in ("-version", "--version", "-v", "-help", "--help", "-h"):
            return Call(tool, args, cwd, changing=False, directory=cwd)
    root = (cwd / chdir).resolve() if chdir else cwd.resolve()
    if not rest:
        return Call(tool, args, cwd, changing=False, directory=root)
    sub, sub_args = rest[0], rest[1:]
    words = [a for a in sub_args if not a.startswith("-")]
    if sub == "state":
        changing = not words or words[0] not in TF_STATE_READ_ONLY
    elif sub in ("workspace", "env"):
        changing = not words or words[0] not in TF_WORKSPACE_READ_ONLY
    elif sub == "init":
        changing = any(a.split("=", 1)[0] in TF_INIT_CHANGING_FLAGS for a in sub_args)
    else:
        changing = sub not in TF_READ_ONLY
    call = Call(tool, args, cwd, changing=changing, directory=root, subcommand=sub)
    if sub == "apply":
        call.plan_file = plan_file_of(sub_args, root)
    return call


def plan_file_of(sub_args: list[str], root: Path) -> Path | None:
    positional = []
    skip = False
    for arg in sub_args:
        if skip:
            skip = False
            continue
        if arg.startswith("-"):
            if "=" not in arg and arg.lstrip("-") and ("-" + arg.lstrip("-")) in TF_VALUE_FLAGS:
                skip = True
            continue
        positional.append(arg)
    for candidate in positional:
        path = Path(candidate)
        path = path if path.is_absolute() else root / path
        if path.is_file():
            return path
    return None


def classify_ansible(args: list[str], cwd: Path) -> Call:
    changing = True
    for arg in args:
        if arg == "--":
            break
        name = arg.split("=", 1)[0]
        if name in ANSIBLE_READ_ONLY_FLAGS:
            changing = False
        elif re.fullmatch(r"-[A-Za-z]+", arg) and "C" in arg and set(arg[1:]) <= ANSIBLE_BUNDLABLE:
            changing = False
    return Call(ANSIBLE_PLAYBOOK, args, cwd, changing=changing, directory=cwd)


# --- target resolution -----------------------------------------------------


def tf_roots(repo: Repo) -> dict[str, dict]:
    roots = repo.config.get("tf", {}) if repo.config else {}
    if not isinstance(roots, dict):
        raise AlockError("alock: .alock.json's \"tf\" must map root paths to objects.", EXIT_CONFIG)
    return {normalise_root(k): (v or {}) for k, v in roots.items()}


def normalise_root(path: str) -> str:
    parts = [p for p in Path(path).as_posix().split("/") if p not in ("", ".")]
    return "/".join(parts) or "."


def tf_targets(repo: Repo, call: Call, run_env: dict[str, str]) -> list[str]:
    rel = normalise_root(os.path.relpath(call.directory, repo.top))
    roots = tf_roots(repo)
    if rel not in roots:
        known = ", ".join(sorted(roots)) or "none"
        raise AlockError(
            f"alock: {rel} isn't a root in {repo.top}/.alock.json (roots: {known}), so "
            "this call can't be locked and didn't run. Ask the user to add it to "
            "the \"tf\" map, with the hosts it produces.",
            EXIT_CONFIG,
        )
    targets = [f"tf/{rel}"]
    hosts = roots[rel].get("hosts")
    if hosts:
        targets += [f"host/{h}" for h in inventory_hosts(repo, rel, hosts, run_env)]
    return targets


def inventory_hosts(repo: Repo, root: str, spec: dict, run_env: dict[str, str]) -> list[str]:
    """The hosts in a root's Ansible inventory, from ansible-inventory."""
    if not isinstance(spec, dict) or not spec.get("inventory"):
        raise AlockError(
            f"alock: .alock.json's hosts for {root} need an \"inventory\" list.", EXIT_CONFIG
        )
    directory = (repo.top / spec.get("dir", ".")).resolve()
    env = dict(run_env)
    extra = [str((directory / p).resolve()) for p in spec.get("path", [])]
    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    cmd = ["ansible-inventory", "--list", "--flush-cache"]
    for source in spec["inventory"]:
        cmd += ["-i", source]
    proc = subprocess.run(cmd, cwd=directory, env=env, text=True, capture_output=True)
    if proc.returncode != 0:
        raise AlockError(
            f"alock: listing the hosts {root} produces failed, so nothing ran:\n"
            f"  {' '.join(cmd)}  (in {directory})\n{proc.stderr.strip()}"
        )
    try:
        inventory = json.loads(proc.stdout)
    except ValueError:
        raise AlockError(f"alock: ansible-inventory printed no JSON:\n{proc.stdout[:500]}")
    names = set(inventory.get("_meta", {}).get("hostvars", {}))
    for name, group in inventory.items():
        if name != "_meta" and isinstance(group, dict):
            names.update(group.get("hosts", []))
    return sorted(names - IMPLICIT_HOSTS)


def ansible_targets(real: str, call: Call, run_env: dict[str, str]) -> list[str]:
    """The hosts a run resolves to, from the same command with --list-hosts."""
    cmd = [real, *call.args, "--list-hosts"]
    proc = subprocess.run(cmd, cwd=call.cwd, env=run_env, text=True, capture_output=True)
    if proc.returncode != 0:
        raise AlockError(
            "alock: listing the hosts this run targets failed, so nothing ran:\n"
            f"{(proc.stdout + proc.stderr).strip()}"
        )
    return [f"host/{h}" for h in sorted(parse_list_hosts(proc.stdout) - IMPLICIT_HOSTS)]


def parse_list_hosts(output: str) -> set[str]:
    hosts: set[str] = set()
    header_indent = None
    for line in output.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if re.fullmatch(r"hosts \(\d+\):", stripped):
            header_indent = indent
            continue
        if header_indent is not None:
            if stripped and indent > header_indent:
                hosts.add(stripped)
                continue
            header_indent = None
    return hosts
