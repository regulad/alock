"""The SessionStart and PreToolUse hooks."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from . import locks, records
from .common import (
    DEFAULT_TIMEOUT,
    MODES,
    PEERS,
    SET_MODE_HINT,
    AlockError,
    Repo,
    find_repo,
    mode_problem,
    read_mode,
)

ROOT = Path(__file__).resolve().parent.parent.parent
BIN_DIR = ROOT / "bin"
BREAK_SCRIPT = "libexec/break.py"


# --- SessionStart ----------------------------------------------------------


def env_lines(session_id: str, timeout: str) -> str:
    bin_dir = shlex.quote(str(BIN_DIR))
    return (
        f"export ALOCK_SESSION={shlex.quote(session_id)}\n"
        f"export ALOCK_TIMEOUT={shlex.quote(timeout)}\n"
        # The shims must come before the real binaries: put their directory
        # first unless it already is.
        f'case ":$PATH:" in ":"{bin_dir}":"*) ;; *) PATH={bin_dir}":$PATH"; export PATH ;; esac\n'
    )


def lock_timeout() -> str:
    value = os.environ.get("CLAUDE_PLUGIN_OPTION_LOCK_TIMEOUT", "")
    try:
        return f"{max(0.0, float(value)):g}"
    except ValueError:
        return f"{DEFAULT_TIMEOUT:g}"


def session_start(event: dict) -> dict:
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        with open(env_file, "a") as f:
            f.write(env_lines(str(event.get("session_id", "")), lock_timeout()))
    cwd = event.get("cwd") or os.getcwd()
    if not Path(cwd).is_dir():
        cwd = os.getcwd()
    try:
        repo = find_repo(cwd)
    except AlockError as e:
        return context(str(e))
    if repo is None:  # outside a repository, only the mode's global value matters
        raw = read_mode(cwd)
        repo = Repo(top=Path(cwd), common=Path(cwd), config=None,
                    mode=raw if raw in MODES else None, raw_mode=raw)
    problem = mode_problem(repo)
    if problem:
        message = (
            f"alock: {problem}. Until it is, alock refuses tf and Ansible changes in "
            f"repositories with an .alock.json.\n{SET_MODE_HINT}"
        )
        out = context(
            f"{message}\nOnly the user can set alock.mode: ask them to, and don't try to "
            "set it yourself."
        )
        out["systemMessage"] = message
        return out
    if repo.mode == "bypass":
        return context(
            "alock: alock.mode is bypass, so tf and Ansible changes run unchecked: no "
            "locks, no check for uncommitted changes, no check against the last applied "
            "commit, and nothing is recorded. Tell the user that any change you apply "
            "runs unprotected."
        )
    if not repo.configured:
        return {}
    return context(status(repo))


def context(text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


def status(repo: Repo) -> str:
    lines = [
        f"alock is on in {repo.top}, in {repo.mode} mode. tofu, terraform and "
        "ansible-playbook changes there lock their targets and check their records; "
        "commit on your worktree branch before applying. An apply is refused when "
        "HEAD lacks the commit last applied to a target. To roll a target back, git "
        "revert the applied commit on a branch that contains it and apply the revert; "
        "there's no other way to undo an apply. When your apply moves a target's "
        "record forward, agents in other worktrees or clones can't apply to it until "
        f"they contain your commit, so you're encouraged to tell {PEERS} that they may "
        "need to rebase onto it."
    ]
    backend = locks.backend_for(repo)
    held = []
    try:
        if repo.mode == "remote":
            records.fetch_origin(repo)
            for lock in backend.locks():
                target = str(lock.get("path", ""))[1:]
                holder = locks.read_holder(repo, target)
                who = holder.describe() if holder else f"owner {(lock.get('owner') or {}).get('name')}"
                held.append(f"  {target}: since {lock.get('locked_at')}, {who}")
        else:
            for holder in locks.all_holders(repo):
                if backend.is_held(holder.target):
                    held.append(f"  {holder.target}: {holder.describe()}")
                else:
                    locks.remove_holder(repo, holder.target)  # left by a finished run
    except AlockError as e:
        lines.append(f"Couldn't read the locks: {e}")
    lines.append("Held locks:\n" + ("\n".join(held) if held else "  none"))
    try:
        head = repo.head()
        missing = []
        refs = records.git(
            "for-each-ref", "--format=%(refname) %(objectname)",
            records.LOCAL_NS, records.ORIGIN_NS, cwd=repo.top,
        ).stdout.splitlines()
        for line in refs:
            ref, sha = line.split()
            if not records.contains(repo, head, sha):
                missing.append(f"  {ref}: {records.describe_commit(repo, sha)}")
        lines.append(
            "Targets whose last apply this HEAD doesn't contain (applying them from here "
            "is refused):\n" + ("\n".join(missing) if missing else "  none")
        )
    except AlockError as e:
        lines.append(f"Couldn't check the records: {e}")
    return "\n".join(lines)


# --- PreToolUse ------------------------------------------------------------

GIT_CONFIG_WRITE_FLAGS = {
    "--add", "--replace-all", "--unset", "--unset-all", "--rename-section",
    "--remove-section", "-e", "--edit",
}
GIT_CONFIG_WRITE_SUBCOMMANDS = {"set", "unset", "rename-section", "remove-section", "edit"}
GIT_CONFIG_READ_FLAGS = {
    "--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list",
    "--get-color", "--get-colorbool", "--show-origin", "--show-scope", "--name-only",
}
GIT_CONFIG_VALUE_OPTIONS = {
    "--file", "-f", "--blob", "--type", "--default", "--comment", "--value",
}
GIT_GLOBAL_VALUE_OPTIONS = {"-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
REAL_TOOLS = {"tofu", "terraform", "ansible-playbook"}
SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", "{", "}", "!"}


def deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


BREAK_IS_USERS = (
    "alock's break is the user's to run, as /alock:break <target>. Tell the user "
    "which lock to break and why; you can't run it yourself."
)


def pre_tool_use(event: dict) -> dict:
    tool = event.get("tool_name")
    data = event.get("tool_input") or {}
    if tool == "Bash":
        command = str(data.get("command", ""))
        reason = check_command(command)
        if names_break_script(command):
            log_break_event(event, allowed=reason is None)
    elif tool == "Skill":
        reason = BREAK_IS_USERS if invokes_break_skill(data) else None
        if reason:
            log_break_event(event, allowed=False)
    elif tool in ("Edit", "Write", "MultiEdit"):
        reason = check_edit(data)
    else:
        reason = None
    return deny(reason) if reason else {}


def names_break_script(command: str) -> bool:
    return BREAK_SCRIPT in command or str(ROOT / BREAK_SCRIPT) in command


def invokes_break_skill(data: dict) -> bool:
    """The Skill tool is how the agent invokes a skill; the user's /alock:break
    is expanded by Claude Code without it. disable-model-invocation should stop
    the agent too, but didn't for this plugin loaded with --plugin-dir."""
    names = [str(v).strip().lstrip("/") for v in data.values() if isinstance(v, str)]
    return any(n == "alock:break" or n.startswith("alock:break ") for n in names)


def simple_commands(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    commands, current = [], []
    for token in lexer:
        if token in SEPARATORS or token == "\n":
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def check_command(command: str) -> str | None:
    # The skill's own injection doesn't pass through this hook, so every Bash
    # call naming the script is the agent's.
    if names_break_script(command):
        return BREAK_IS_USERS
    try:
        commands = simple_commands(command.replace("\n", " ; "))
    except ValueError:
        if "alock" in command and "config" in command:
            return "alock: couldn't parse this command, and it may change alock.* config."
        return None
    for argv in commands:
        reason = check_argv(argv)
        if reason:
            return reason
    return None


def log_break_event(event: dict, allowed: bool) -> None:
    """Keep every attempt on break, as Bash calls naming its script and Skill
    calls invoking it, so it's visible how each one reached the hook. Only
    these are kept: they hold targets, not secrets."""
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not data_dir:
        return
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        with open(Path(data_dir) / "break-events.jsonl", "a") as f:
            f.write(json.dumps({"allowed": allowed, "event": event}) + "\n")
    except OSError:
        pass


MODE_IS_USERS = (
    "alock.* is the user's to set: only they change alock.mode, in their own terminal "
    "(git config [--global] alock.mode local|remote|bypass). Reading it is fine. Tell "
    "the user what you need instead."
)


def check_argv(argv: list[str]) -> str | None:
    i = 0
    while i < len(argv) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[i]):
        name, _, value = argv[i].partition("=")
        if name.startswith("GIT_CONFIG") and "alock" in value.lower():
            return MODE_IS_USERS
        i += 1
    while i < len(argv) and argv[i] in ("env", "command", "exec", "nohup", "time", "sudo"):
        i += 1
        while i < len(argv) and (argv[i].startswith("-") or "=" in argv[i]):
            name, _, value = argv[i].partition("=")
            if name.startswith("GIT_CONFIG") and "alock" in value.lower():
                return MODE_IS_USERS
            i += 1
    if i >= len(argv):
        return None
    program, rest = argv[i], argv[i + 1 :]
    name = Path(program).name
    if name in REAL_TOOLS and "/" in program and Path(program).resolve().parent != BIN_DIR:
        return (
            f"Run {name} by its bare name, so alock's shim can lock what it changes. "
            "Calling it by a path skips the shim."
        )
    if name == "git":
        return check_git(rest)
    return None


def check_git(args: list[str]) -> str | None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if arg == "-c" and i + 1 < len(args):
            if args[i + 1].lower().startswith("alock."):
                return MODE_IS_USERS
            i += 2
            continue
        if arg.startswith("--config-env") and "alock" in arg.lower():
            return MODE_IS_USERS
        i += 2 if arg in GIT_GLOBAL_VALUE_OPTIONS else 1
    if i >= len(args) or args[i] != "config":
        return None
    return check_git_config(args[i + 1 :])


def check_git_config(args: list[str]) -> str | None:
    words, flags = [], []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            flags.append(arg.split("=", 1)[0])
            if arg in GIT_CONFIG_VALUE_OPTIONS:
                i += 1
        else:
            words.append(arg)
        i += 1
    if words and words[0] in GIT_CONFIG_WRITE_SUBCOMMANDS:
        sub, keys = words[0], words[1:]
        if sub == "edit" or any(k.lower().startswith("alock") for k in keys):
            return MODE_IS_USERS
        return None
    if words and words[0] in ("get", "list"):
        return None
    if any(f in ("-e", "--edit") for f in flags):
        return MODE_IS_USERS
    names_alock = any(w.lower() == "alock" or w.lower().startswith("alock.") for w in words)
    if not names_alock:
        return None
    if any(f in GIT_CONFIG_WRITE_FLAGS for f in flags):
        return MODE_IS_USERS
    if any(f in GIT_CONFIG_READ_FLAGS for f in flags):
        return None
    # The old form: `git config key` reads, `git config key value` writes.
    return MODE_IS_USERS if len(words) >= 2 else None


def is_git_config_file(path: str) -> bool:
    p = Path(os.path.expanduser(path))
    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    if p in (home / ".gitconfig", xdg / "git" / "config", Path("/etc/gitconfig")):
        return True
    parts = p.parts
    return p.name in ("config", "config.worktree") and (
        ".git" in parts[:-1] or (len(parts) >= 3 and parts[-3] == "worktrees")
    )


def check_edit(data: dict) -> str | None:
    path = str(data.get("file_path", ""))
    if not path or not is_git_config_file(path):
        return None
    texts = [str(data.get(k, "")) for k in ("old_string", "new_string", "content")]
    for edit in data.get("edits") or []:
        texts += [str(edit.get("old_string", "")), str(edit.get("new_string", ""))]
    if any("alock" in t.lower() for t in texts):
        return MODE_IS_USERS
    return None
