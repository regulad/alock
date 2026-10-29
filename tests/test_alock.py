"""End-to-end tests: the shims run against stub tofu, ansible-playbook and
ansible-inventory in throwaway repositories, so nothing real is touched.
Remote mode uses a local bare repository as origin and tests/stubs/fake_git_lfs.py
in place of `git lfs`.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIMS = ROOT / "bin"
STUBS = ROOT / "tests" / "stubs" / "bin"
FAKE_LFS = ROOT / "tests" / "stubs" / "fake_git_lfs.py"
HOOK = ROOT / "libexec" / "hook.py"
BREAK = ROOT / "libexec" / "break.py"

CONFIG = {
    "tf": {
        "tofu/net": {"hosts": {"inventory": ["inventory.yml"]}},
        "tofu/plain": {},
    }
}


class Lab:
    """A temporary home with its own git config, an origin, and clones."""

    def __init__(self):
        # Resolved, as git reports paths: macOS's temporary directory is under
        # /var, a symlink to /private/var.
        self.dir = Path(tempfile.mkdtemp(prefix="alock-test-")).resolve()
        self.log = self.dir / "stub.log"
        self.log.touch()
        gitconfig = self.dir / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = Tester\n\temail = tester@example.invalid\n"
            "[commit]\n\tgpgSign = false\n[tag]\n\tgpgSign = false\n"
            "[init]\n\tdefaultBranch = master\n"
        )
        self.env = {
            "HOME": str(self.dir),
            "PATH": os.pathsep.join([str(SHIMS), str(STUBS), os.environ["PATH"]]),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "STUB_LOG": str(self.log),
            "STUB_INV_HOSTS": "h1,h2",
            "ALOCK_TIMEOUT": "1",
            "ALOCK_SESSION": "test-session",
            "ALOCK_GIT_LFS": f"python3 {FAKE_LFS}",
            "FAKE_LFS_DIR": str(self.dir / "lfs"),
            "LANG": "C.UTF-8",
        }
        self.origin = self.dir / "origin.git"
        self.git(self.dir, "init", "-q", "--bare", str(self.origin))
        seed = self.dir / "seed"
        self.git(self.dir, "init", "-q", str(seed))
        (seed / ".alock.json").write_text(json.dumps(CONFIG))
        (seed / ".gitignore").write_text("*.tfplan\n")
        for root in ("tofu/net", "tofu/plain", "tofu/other"):
            (seed / root).mkdir(parents=True)
            (seed / root / "main.tf").write_text("# stub\n")
        (seed / "inventory.yml").write_text("# stub\n")
        (seed / "site.yml").write_text("# stub\n")
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "-q", "-m", "seed")
        self.git(seed, "remote", "add", "origin", str(self.origin))
        self.git(seed, "push", "-q", "origin", "master")

    def clone(self, name: str, mode: str | None = "local") -> Path:
        path = self.dir / name
        self.git(self.dir, "clone", "-q", str(self.origin), str(path))
        if mode:
            self.git(path, "config", "alock.mode", mode)
        return path

    def git(self, cwd, *args) -> str:
        proc = subprocess.run(["git", *args], cwd=cwd, env=self.env, text=True, capture_output=True)
        if proc.returncode != 0:
            raise AssertionError(f"git {args} failed: {proc.stderr}")
        return proc.stdout.strip()

    def commit(self, repo: Path, name: str, text: str = "x", message: str | None = None) -> str:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", message or f"change {name}")
        return self.git(repo, "rev-parse", "HEAD")

    def run(self, cwd, *argv, env=None, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            list(argv), cwd=cwd, env={**self.env, **(env or {})}, text=True,
            capture_output=True, timeout=60, **kwargs,
        )

    def start(self, cwd, *argv, env=None) -> subprocess.Popen:
        return subprocess.Popen(
            list(argv), cwd=cwd, env={**self.env, **(env or {})},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )

    def wait_for_log(self, needle: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.log.read_text():
                return
            time.sleep(0.05)
        raise AssertionError(f"{needle!r} never appeared in the stub log:\n{self.log.read_text()}")

    def ran(self, needle: str) -> bool:
        return needle in self.log.read_text()

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class AlockTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)

    # --- passing through -----------------------------------------------------

    def test_unconfigured_repository_passes_through(self):
        other = self.lab.dir / "other"
        self.lab.git(self.lab.dir, "init", "-q", str(other))
        result = self.lab.run(other, "tofu", "apply", "-auto-approve")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.lab.ran("tofu apply -auto-approve"))

    def test_read_only_calls_pass_through_even_unset(self):
        repo = self.lab.clone("a", mode=None)
        result = self.lab.run(repo / "tofu/plain", "tofu", "plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.lab.run(repo, "ansible-playbook", "site.yml", "--check", "--diff")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.lab.ran("ansible-playbook site.yml --check --diff"))

    def test_ansible_gets_blocking_handles(self):
        # Claude Code's Bash tool hands commands non-blocking stdout and stderr.
        repo = self.lab.clone("a")
        for argv in (["--check"], ["--limit", "h1"]):  # passed through, then locked
            r, w = os.pipe()
            os.set_blocking(w, False)
            proc = subprocess.Popen(
                ["ansible-playbook", "site.yml", *argv], cwd=repo, env=self.lab.env,
                stdin=subprocess.DEVNULL, stdout=w, stderr=w,
            )
            os.close(w)
            with os.fdopen(r) as out:
                output = out.read()
            self.assertEqual(proc.wait(timeout=60), 0, output)
            self.assertTrue(self.lab.ran(f"ansible-playbook site.yml {' '.join(argv)}"))

    # --- modes ---------------------------------------------------------------

    def test_unset_mode_refuses(self):
        repo = self.lab.clone("a", mode=None)
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 78)
        self.assertIn("alock.mode isn't set", result.stderr)
        self.assertFalse(self.lab.ran("tofu apply"))

    def test_invalid_mode_is_named(self):
        repo = self.lab.clone("a", mode="lokal")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 78)
        self.assertIn("'lokal'", result.stderr)

    def test_global_mode_is_inherited(self):
        self.lab.git(self.lab.dir, "config", "--global", "alock.mode", "local")
        repo = self.lab.clone("a", mode=None)
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_command_scoped_config_cant_set_the_mode(self):
        repo = self.lab.clone("a", mode=None)
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "alock.mode", "GIT_CONFIG_VALUE_0": "bypass"}
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply", env=env)
        self.assertEqual(result.returncode, 78)

    def test_bypass_runs_with_a_warning(self):
        repo = self.lab.clone("a", mode="bypass")
        self.lab.commit(repo, "dirty-but-bypassed")
        (repo / "uncommitted").write_text("x")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bypass", result.stderr)
        self.assertIn("no check for uncommitted changes", result.stderr)
        self.assertIn("isn't recorded", result.stderr)
        self.assertIn("Tell the user", result.stderr)
        self.assertEqual(self.lab.git(repo, "for-each-ref", "refs/alock"), "")

    # --- rules ---------------------------------------------------------------

    def test_dirty_worktree_refuses(self):
        repo = self.lab.clone("a")
        (repo / "tofu/plain/main.tf").write_text("# edited\n")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 77)
        self.assertIn("uncommitted changes", result.stderr)
        self.assertFalse(self.lab.ran("tofu apply"))

    def test_unknown_root_refuses(self):
        repo = self.lab.clone("a")
        result = self.lab.run(repo / "tofu/other", "tofu", "apply")
        self.assertEqual(result.returncode, 78)
        self.assertIn("tofu/other isn't a root", result.stderr)

    def test_chdir_selects_the_root(self):
        repo = self.lab.clone("a")
        result = self.lab.run(repo, "tofu", "-chdir=tofu/plain", "apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        head = self.lab.git(repo, "rev-parse", "HEAD")
        self.assertEqual(self.lab.git(repo, "rev-parse", "refs/alock/tf/tofu%2Fplain"), head)

    def test_saved_plan_must_be_named_for_head(self):
        repo = self.lab.clone("a")
        head = self.lab.git(repo, "rev-parse", "HEAD")
        (repo / "tofu/plain/wrong@deadbeef.tfplan").write_text("plan")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply", "wrong@deadbeef.tfplan")
        self.assertEqual(result.returncode, 77)
        self.assertIn("must be named", result.stderr)
        good = f"plain@{head[:12]}.tfplan"
        (repo / "tofu/plain" / good).write_text("plan")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply", good)
        self.assertEqual(result.returncode, 0, result.stderr)

    # --- records -------------------------------------------------------------

    def test_silent_revert_is_refused(self):
        repo = self.lab.clone("a")
        base = self.lab.git(repo, "rev-parse", "HEAD")
        self.lab.git(repo, "switch", "-q", "-c", "feature")
        feature = self.lab.commit(repo, "tofu/plain/feature.tf")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lab.git(repo, "rev-parse", "refs/alock/tf/tofu%2Fplain"), feature)
        # master doesn't have the feature: applying it would undo it.
        self.lab.git(repo, "switch", "-q", "master")
        self.assertEqual(self.lab.git(repo, "rev-parse", "HEAD"), base)
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 77)
        self.assertIn("doesn't contain what was last applied", result.stderr)
        self.assertIn("tf/tofu/plain", result.stderr)
        # Another root is unaffected.
        result = self.lab.run(repo, "ansible-playbook", "site.yml", "--limit", "h9")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_descendant_and_rebased_applies_are_accepted(self):
        repo = self.lab.clone("a")
        self.lab.git(repo, "switch", "-q", "-c", "feature")
        self.lab.commit(repo, "tofu/plain/feature.tf")
        self.assertEqual(self.lab.run(repo / "tofu/plain", "tofu", "apply").returncode, 0)
        # master moves on; the feature is rebased onto it, changing its commit.
        self.lab.git(repo, "switch", "-q", "master")
        self.lab.commit(repo, "unrelated.txt")
        self.lab.git(repo, "switch", "-q", "feature")
        self.lab.git(repo, "rebase", "-q", "master")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_moving_a_record_suggests_telling_other_agents(self):
        repo = self.lab.clone("a")
        first = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("tf/tofu/plain now records ", first.stderr)
        self.assertIn("apply to it until", first.stderr)
        self.assertIn("working on this repository", first.stderr)
        self.assertIn("rebase onto it", first.stderr)
        again = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("now record", again.stderr)
        # A root with hosts moves several records at once.
        several = self.lab.run(repo / "tofu/net", "tofu", "apply")
        self.assertEqual(several.returncode, 0, several.stderr)
        self.assertIn("host/h2 now record ", several.stderr)
        self.assertIn("apply to them until", several.stderr)

    def test_rollback_is_a_revert(self):
        repo = self.lab.clone("a")
        self.lab.git(repo, "switch", "-q", "-c", "feature")
        applied = self.lab.commit(repo, "tofu/plain/feature.tf")
        self.assertEqual(self.lab.run(repo / "tofu/plain", "tofu", "apply").returncode, 0)
        # Checking out the old commit would undo the feature unrecorded: refused.
        self.lab.git(repo, "switch", "-q", "--detach", "HEAD~1")
        refused = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(refused.returncode, 77)
        self.assertIn("git revert", refused.stderr)
        # Reverting it is the way: the revert contains the record.
        self.lab.git(repo, "switch", "-q", "feature")
        self.lab.git(repo, "revert", "--no-edit", applied)
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_record_is_written_even_when_the_run_fails(self):
        repo = self.lab.clone("a")
        head = self.lab.git(repo, "rev-parse", "HEAD")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply", env={"STUB_EXIT": "3"})
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.lab.git(repo, "rev-parse", "refs/alock/tf/tofu%2Fplain"), head)

    # --- locks ---------------------------------------------------------------

    def test_root_locks_its_hosts(self):
        repo = self.lab.clone("a")
        apply = self.lab.start(repo / "tofu/net", "tofu", "apply", env={"STUB_SLEEP": "4"})
        self.lab.wait_for_log("tofu apply")
        blocked = self.lab.run(repo, "ansible-playbook", "site.yml", "--limit", "h1")
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.assertIn("host/h1", blocked.stderr)
        self.assertIn(f"/alock:break -C {repo} host/h1", blocked.stderr)
        self.assertIn("waiting for 1 lock,", blocked.stderr)
        self.assertIn("tofu apply", blocked.stderr)  # names what the holder runs
        free = self.lab.run(repo, "ansible-playbook", "site.yml", "--limit", "h3")
        self.assertEqual(free.returncode, 0, free.stderr)
        self.assertEqual(apply.wait(timeout=30), 0)
        after = self.lab.run(repo, "ansible-playbook", "site.yml", "--limit", "h1")
        self.assertEqual(after.returncode, 0, after.stderr)

    def test_waits_for_a_lock_within_the_timeout(self):
        repo = self.lab.clone("a")
        apply = self.lab.start(repo / "tofu/plain", "tofu", "apply", env={"STUB_SLEEP": "1"})
        self.lab.wait_for_log("tofu apply")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply", env={"ALOCK_TIMEOUT": "20"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(apply.wait(timeout=30), 0)

    def test_worktrees_share_locks_and_records(self):
        repo = self.lab.clone("a")
        linked = self.lab.dir / "linked"
        self.lab.git(repo, "worktree", "add", "-q", "-b", "other", str(linked))
        apply = self.lab.start(repo / "tofu/plain", "tofu", "apply", env={"STUB_SLEEP": "3"})
        self.lab.wait_for_log("tofu apply")
        blocked = self.lab.run(linked / "tofu/plain", "tofu", "apply")
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.assertEqual(apply.wait(timeout=30), 0)
        self.lab.git(repo, "switch", "-q", "-c", "ahead")
        self.lab.commit(repo, "tofu/plain/ahead.tf")
        self.assertEqual(self.lab.run(repo / "tofu/plain", "tofu", "apply").returncode, 0)
        behind = self.lab.run(linked / "tofu/plain", "tofu", "apply")
        self.assertEqual(behind.returncode, 77, behind.stderr)

    def test_lock_outlives_a_killed_shim(self):
        repo = self.lab.clone("a")
        apply = self.lab.start(repo / "tofu/plain", "tofu", "apply", env={"STUB_SLEEP": "3"})
        self.lab.wait_for_log("tofu apply")
        os.kill(apply.pid, signal.SIGKILL)  # the shim alone; its child keeps running
        apply.wait()
        blocked = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.lab.wait_for_log("tofu done")
        time.sleep(0.2)
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 0, result.stderr)

    # --- remote mode ---------------------------------------------------------

    def test_remote_records_and_locks_span_clones(self):
        one = self.lab.clone("one", mode="remote")
        two = self.lab.clone("two", mode="remote")
        self.lab.git(one, "switch", "-q", "-c", "feature")
        feature = self.lab.commit(one, "tofu/plain/feature.tf")
        result = self.lab.run(one / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        on_origin = self.lab.git(self.lab.origin, "rev-parse", "refs/alock/tf/tofu%2Fplain")
        self.assertEqual(on_origin, feature)
        # The other clone, still on master, would undo the feature.
        result = self.lab.run(two / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 77, result.stderr)
        # Its lock is seen from the other clone too.
        apply = self.lab.start(one / "tofu/plain", "tofu", "apply", env={"STUB_SLEEP": "3"})
        deadline = time.monotonic() + 20
        while self.lab.log.read_text().count("tofu apply") < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        blocked = self.lab.run(two / "tofu/plain", "tofu", "apply")
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.assertIn("LFS lock", blocked.stderr)
        self.assertEqual(apply.wait(timeout=30), 0)
        self.assertEqual(list((self.lab.dir / "lfs").iterdir()), [])  # released

    def test_local_history_reaches_origin_after_switching(self):
        repo = self.lab.clone("a", mode="local")
        self.lab.git(repo, "switch", "-q", "-c", "feature")
        feature = self.lab.commit(repo, "tofu/plain/feature.tf")
        self.assertEqual(self.lab.run(repo / "tofu/plain", "tofu", "apply").returncode, 0)
        self.lab.git(repo, "config", "alock.mode", "remote")
        # The local record still binds this clone in remote mode.
        self.lab.git(repo, "switch", "-q", "master")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 77, result.stderr)
        self.lab.git(repo, "switch", "-q", "feature")
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lab.git(self.lab.origin, "rev-parse", "refs/alock/tf/tofu%2Fplain"), feature)

    def test_remote_orphan_on_this_machine_is_released(self):
        repo = self.lab.clone("a", mode="remote")
        apply = self.lab.start(repo / "tofu/plain", "tofu", "apply", env={"STUB_SLEEP": "30"})
        self.lab.wait_for_log("tofu apply")
        os.killpg(apply.pid, signal.SIGKILL)  # the shim and its child: a crash
        apply.wait()
        result = self.lab.run(repo / "tofu/plain", "tofu", "apply")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_break_touches_nothing_where_alock_isnt_on(self):
        other = self.lab.dir / "other"
        self.lab.git(self.lab.dir, "init", "-q", str(other))
        self.lab.git(other, "config", "alock.mode", "local")
        out = self.lab.run(other, "python3", str(BREAK), "--from-skill", "host/h1").stdout
        self.assertIn("has no .alock.json", out)
        self.assertIn("-C", out)
        self.assertFalse((other / ".git" / "alock").exists())
        repo = self.lab.clone("a")
        out = self.lab.run(repo, "python3", str(BREAK), "--from-skill", "host/never").stdout
        self.assertIn("not locked", out)
        self.assertFalse((repo / ".git" / "alock" / "locks" / "host" / "never").exists())
        # -C names the repository when the session's directory is elsewhere.
        out = self.lab.run(other, "python3", str(BREAK), "--from-skill", "-C", str(repo), "host/never").stdout
        self.assertIn("host/never: not locked", out)

    def test_break_force_unlocks_a_lock_held_elsewhere(self):
        repo = self.lab.clone("a", mode="remote")
        held = self.lab.run(repo, "python3", str(FAKE_LFS), "lock", "--json", "+host/h1")
        self.assertEqual(held.returncode, 0, held.stderr)
        blocked = self.lab.run(repo, "ansible-playbook", "site.yml", "--limit", "h1")
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        refused = self.lab.run(repo, "python3", str(BREAK), "host/h1")
        self.assertIn("only as the user's /alock:break", refused.stdout)
        broken = self.lab.run(repo, "python3", str(BREAK), "--from-skill", "host/h1")
        self.assertIn("host/h1: BROKEN", broken.stdout)
        result = self.lab.run(repo, "ansible-playbook", "site.yml", "--limit", "h1")
        self.assertEqual(result.returncode, 0, result.stderr)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)

    def hook(self, event_name, event, env=None):
        proc = self.lab.run(
            self.lab.dir, "python3", "-I", str(HOOK), event_name,
            input=json.dumps(event), env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def decision(self, command):
        out = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": command}})
        return out.get("hookSpecificOutput", {}).get("permissionDecision", "allow")

    def test_denies_changing_alock_config(self):
        for command in (
            "git config alock.mode remote",
            "git config --global alock.mode bypass",
            "git -C /tmp config --local alock.mode local",
            "git config --unset alock.mode",
            "git config set alock.mode local",
            "git config --remove-section alock",
            "git -c alock.mode=bypass status",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alock.mode GIT_CONFIG_VALUE_0=bypass tofu apply",
            "cd x && git config alock.mode local",
            "echo $(git config alock.mode remote)",
            "git config --edit",
        ):
            self.assertEqual(self.decision(command), "deny", command)

    def test_allows_reading_alock_config_and_other_commands(self):
        for command in (
            "git config --get alock.mode",
            "git config alock.mode",
            "git config get alock.mode",
            "git config --list --show-origin",
            "git config user.name Someone",
            "tofu apply -auto-approve",
            "ansible-playbook site.yml",
        ):
            self.assertEqual(self.decision(command), "allow", command)

    def test_denies_real_binaries_by_path_and_the_break_script(self):
        self.assertEqual(self.decision("/usr/bin/tofu apply"), "deny")
        self.assertEqual(self.decision(f"{SHIMS}/tofu apply"), "allow")
        self.assertEqual(self.decision(f"python3 {BREAK} --from-skill host/a; echo"), "deny")
        self.assertEqual(self.decision(f"python3 {BREAK} host/a"), "deny")
        self.assertEqual(self.decision("python3 ~/repositories/alock/libexec/break.py --from-skill host/a"), "deny")
        self.assertEqual(self.decision("cd ~/repositories/alock && python3 libexec/break.py --from-skill host/a"), "deny")
        self.assertEqual(self.decision(f"/usr/bin/python3 {BREAK} --from-skill host/a"), "deny")
        # The skill's injection doesn't reach the hook, so even its exact form is the agent's.
        self.assertEqual(self.decision(f'python3 "{BREAK}" --from-skill host/a'), "deny")

    def test_denies_the_agent_invoking_the_break_skill(self):
        def skill(data):
            out = self.hook("PreToolUse", {"tool_name": "Skill", "tool_input": data})
            return out.get("hookSpecificOutput", {}).get("permissionDecision", "allow")

        self.assertEqual(skill({"skill": "alock:break", "args": "host/a"}), "deny")
        self.assertEqual(skill({"skill": "/alock:break host/a"}), "deny")
        self.assertEqual(skill({"skill": "other:break"}), "allow")
        self.assertEqual(skill({"skill": "simplify"}), "allow")

    def test_break_calls_are_logged_for_comparison(self):
        data = self.lab.dir / "plugin-data"
        event = {"tool_name": "Bash", "tool_input": {"command": f"python3 {BREAK} host/a"}}
        self.hook("PreToolUse", event, env={"CLAUDE_PLUGIN_DATA": str(data)})
        self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                  env={"CLAUDE_PLUGIN_DATA": str(data)})
        lines = (data / "break-events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertFalse(json.loads(lines[0])["allowed"])

    def test_denies_editing_alock_in_git_config_files(self):
        def edit(path, new):
            out = self.hook("PreToolUse", {
                "tool_name": "Edit",
                "tool_input": {"file_path": path, "old_string": "x", "new_string": new},
            })
            return out.get("hookSpecificOutput", {}).get("permissionDecision", "allow")

        self.assertEqual(edit("/r/.git/config", "[alock]\n\tmode = bypass"), "deny")
        self.assertEqual(edit(f"{self.lab.dir}/.gitconfig", "mode = x [alock]"), "deny")
        self.assertEqual(edit("/r/.git/config", "[user]\n\tname = x"), "allow")
        self.assertEqual(edit("/r/src/config", "alock"), "allow")

    def test_session_start_unset_mode_warns_the_user(self):
        env_file = self.lab.dir / "env.sh"
        out = self.hook(
            "SessionStart", {"session_id": "abc", "cwd": str(self.lab.dir)},
            env={"CLAUDE_ENV_FILE": str(env_file), "CLAUDE_PLUGIN_OPTION_LOCK_TIMEOUT": "42"},
        )
        self.assertIn("alock.mode isn't set", out["systemMessage"])
        self.assertIn("don't try to set it", out["hookSpecificOutput"]["additionalContext"])
        text = env_file.read_text()
        self.assertIn("ALOCK_SESSION=abc", text)
        self.assertIn("ALOCK_TIMEOUT=42", text)
        path = subprocess.run(
            ["sh", "-c", f'. "{env_file}"; . "{env_file}"; printf %s "$PATH"'],
            env={"PATH": "/usr/bin:/bin"}, text=True, capture_output=True,
        ).stdout
        self.assertEqual(path, f"{SHIMS}:/usr/bin:/bin")  # first, and only once

    def test_session_start_bypass_and_status(self):
        repo = self.lab.clone("a", mode="bypass")
        out = self.hook("SessionStart", {"session_id": "abc", "cwd": str(repo)})
        self.assertIn("bypass", out["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("systemMessage", out)
        self.lab.git(repo, "config", "alock.mode", "local")
        self.assertEqual(self.lab.run(repo / "tofu/plain", "tofu", "apply").returncode, 0)
        out = self.hook("SessionStart", {"session_id": "abc", "cwd": str(repo)})
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("local mode", context)
        self.assertIn("Held locks:\n  none", context)


if __name__ == "__main__":
    unittest.main()
