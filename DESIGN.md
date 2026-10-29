# alock

A Claude Code plugin that keeps parallel agents from overlapping or undoing
each other's OpenTofu, Terraform and Ansible applies. Agents run `tofu`,
`terraform` and `ansible-playbook` as usual; the plugin intercepts the calls
that change infrastructure, works out what they change, and locks it. OpenTofu
and Terraform are treated alike, as `tf`.

## Problem

Several agents work on one repository at once, each in its own git worktree.
Git keeps their edits apart, but they all apply to the same live
infrastructure, and two failures follow:

- **Overlap:** two runs change the same target at the same time.
- **Silent revert:** an agent applies from a branch that lacks a change someone
  else already applied, and undoes it. A state lock doesn't catch this, and
  neither does a plan, where an in-place update looks routine.

alock prevents both. It does not replace a human's approval of each apply; it
sits underneath it.

## Model

- **A target** is `tf/<root>` for an OpenTofu or Terraform root, by its
  directory relative to the repository root (`tf/tofu/oracle`), or
  `host/<inventory_hostname>` for an Ansible host.
- **Every change runs under a lock on each target it touches.** An Ansible run
  locks each host it resolves to, so a broad run locks all of them and waits
  for every single-host run on those hosts. A root's apply locks the root and
  every host its repository config says the root produces, because the apply
  can create, change or replace them.
- **Each target's record** is the ref `refs/alock/<target>`, pointing at the
  commit the target was last applied from and keeping that commit reachable.
- **The rule:** a change to a target is allowed only if HEAD contains the
  commit its record points at. Applies to a target therefore move forward
  along a single line, and none can undo another.
- **Changes run from commits.** An agent commits on its own worktree branch,
  then applies.
- **Rolling back is a `git revert`.**
  - The agent reverts the applied commit on a branch that contains it, merging
    that commit's branch in first if it never landed, and applies the revert
    like any other change.
  - The revert contains the record, so the rule lets it through, and every
    other agent then needs it too, as with any change.
  - alock has no command that erases or rewinds a record. The refusal message
    and the SessionStart context both tell agents this.
- **Moving a record forward is worth announcing.**
  - Once an apply records a new commit for a target, agents in other worktrees
    or clones can't apply to it until they contain that commit.
  - After such an apply, the shim prints a note naming the targets and the
    commit. It encourages the agent to tell the other agents working on this
    repository, so they can rebase onto it. The SessionStart context says the
    same.
  - **Whom to tell,** here and for a broken lock: in Claude Code, the
    sessions `ListAgents` returns that work in this repository, its worktrees
    or its clones. `ListAgents` lists every local session, unrelated ones
    included, so every message alock writes names that scope.
  - A re-apply of the commit already recorded prints nothing.

## Interception

The plugin's `bin/` holds shims named `tofu`, `terraform` and
`ansible-playbook`, which come first on the Bash tool's `PATH`. Each shim sees
its exact argv and working directory, so compound commands, pipelines,
`sh -c` and scripts are all covered: they inherit `PATH`.

- **Outside a configured repository**, in `bypass` mode, or for a call that
  changes nothing, the shim runs the real binary directly.
  - **tf's read-only calls** are `plan`, `show`, `output`, `validate`, `fmt`,
    `init` (without `-migrate-state` or `-force-copy`), `providers`,
    `version`, `graph`, `console`, `get`, `modules`, `metadata`, `login`,
    `logout`, `state list|show|pull` and `workspace list|show|select`. Any
    other subcommand counts as changing, so an unfamiliar one fails closed.
    That includes `test`, which applies for real unless its providers are
    mocked.
  - **`ansible-playbook`'s read-only calls** have `--check`/`-C` (also bundled,
    as in `-vC`), `--syntax-check` or a `--list-*` option.
- **A changing call** goes through the apply steps below, then runs the real
  binary as its child.
- **To the agent**, an allowed change looks like the bare command, only slower
  while it waits for a lock. A refused one fails without running, with the
  reason on stderr.

The real binary is the first match on `PATH` after the plugin's own `bin/`.

The `ansible-playbook` shim makes its stdin, stdout and stderr blocking before
running anything. Claude Code's Bash tool hands commands non-blocking handles,
and Ansible refuses to start on those, so a bare `ansible-playbook` would
otherwise fail there.

## Repository config

A repository opts in with `.alock.json` at its root. The shims pass through in
any repository without one.

```json
{
  "tf": {
    "tofu/gonkputer": {
      "hosts": {
        "dir": "ansible-generated",
        "inventory": ["inventory/gonkputer.terraform_state.yml"],
        "path": ["bin"]
      }
    },
    "tofu/backblaze": {}
  }
}
```

- **`tf`** maps each OpenTofu or Terraform root, by its directory relative to
  the repository root, to how to list the hosts it produces. A root missing
  from the map is refused, so a new root can't be applied unlocked.
- **`hosts`** runs `ansible-inventory --list --flush-cache` with each
  `inventory` source:
  - **`dir`**, relative to the repository root, is where it runs, so that
    directory's `ansible.cfg` applies. It defaults to the root.
  - **`path`** lists directories, relative to `dir`, put first on its `PATH`,
    for inventory plugins that run a wrapper from there.
- **Ansible hosts** need no config: `ansible-playbook --list-hosts` resolves a
  run's `--limit`, patterns and groups exactly.
- **The controller is never a target:** `localhost`, `127.0.0.1` and `::1` are
  left out. Plays there act on the machine running Ansible, and locking it
  would make every run that has such a play wait for every other one.

## Modes

The mode is the git config value `alock.mode`, in one of four states:

| `alock.mode` | Locks | Records |
|---|---|---|
| unset | none: every changing call in a configured repository is refused | none |
| `bypass` | none: changing calls run unlocked and unchecked, with a warning | none |
| `local` | `flock` on `<common dir>/alock/locks/<target>` | `refs/alock/<target>` in this clone |
| `remote` | a Git LFS lock on the synthetic path `+<target>`, through the hosting service's locking API | `refs/alock/<target>` in this clone and on `origin`, pushed with its commit |

- **It is read by ordinary git config lookup:** system, global, the clone's
  own config and per-worktree config. A global value therefore sets the mode
  for every clone that doesn't set its own.
  - The shim ignores command-scoped overrides: `git -c`, which travels as
    `GIT_CONFIG_PARAMETERS`, and `GIT_CONFIG_COUNT` with its keys and values.
    A single command can't change its own mode.
  - Any value other than `local`, `remote` or `bypass` is treated like unset,
    and the refusal names it.
- **Only a human sets it,** with `git config [--global] alock.mode
  local|remote|bypass` in their own terminal. Agents can read it but not
  change it (see Hooks).
- **`bypass`** is for a user who doesn't need alock. The shims run every call
  straight through. Each changing call first prints a warning on stderr
  saying that it runs unchecked: no locks, no check for uncommitted changes,
  no check against the last applied commit, and no record, so what's live
  may end up matching no commit. The warning tells the agent to tell the
  user. Turning alock back on starts from the records as they were.
- **Switching needs no migration.** Records are written to this clone's
  `refs/alock/<target>` in both modes, and a remote-mode run checks this
  clone's record as well as `origin`'s. A clone's local-mode history reaches
  `origin` with its first remote-mode apply to each target, because that apply
  pushes a HEAD containing it. Until then, local mode's own condition, that no
  other clone applies, is what keeps the gap safe.
- **`local`** is safe only while every agent that applies works in this one
  clone.
- **`remote`** works across clones and machines.
  - Locking a path that's already locked fails with 409 Conflict.
  - Every lock is owned by the one account the clones push as, and LFS locks
    never expire.
  - `git lfs install` is not run: its pre-push hook would check locks on every
    push, and pushes would fail whenever the locks API does.
  - `origin`'s records are fetched into `refs/alock-origin/<target>`, so they
    never overwrite this clone's own. The default fetch refspec includes
    neither namespace, so ordinary fetches and clones never see the records.

**Git's own layout.**
- **The common dir** is `git rev-parse --git-common-dir`, the git directory
  every worktree of a clone shares. Git leaves directories it doesn't know
  there alone. alock never resolves its paths with `git rev-parse --git-path`,
  which maps paths Git doesn't know to the worktree's own directory.
- **Refs under `refs/alock/` and `refs/alock-origin/`** are shared by all of a
  clone's worktrees, as are all refs but `refs/bisect`, `refs/worktree` and
  `refs/rewritten`.

**Holder files.** Each lock taken has a holder file in this clone,
`<common dir>/alock/held/<kind>/<encoded name>.json`, such as
`held/host/scratch-a.json`. It records:
- the session, the shim's and the child's pids with their start times;
- the machine, worktree, branch and command;
- when the lock was taken, after any wait.

A subagent carries its session's `ALOCK_SESSION`, so a holder file can't tell
a subagent's run from its parent's. Their pids and commands still differ.

## Apply steps

1. **Refuse a dirty worktree.** Tracked or untracked changes mean what's
   applied wouldn't be HEAD.
2. **Resolve the targets.**
   - **`tofu` or `terraform`:** the root from `-chdir` or the working
     directory, plus the hosts from `ansible-inventory --list --flush-cache`
     over the root's configured sources.
   - **`ansible-playbook`:** the same command with `--list-hosts`.
3. **Acquire every lock in sorted target order, waiting up to the lock
   timeout.**
   - Sorted order means two multi-target runs can never deadlock.
   - In local mode it waits on `flock`. In remote mode it polls with backoff.
   - Each acquired lock gets its holder file.
   - **On timeout**, it releases everything it took and fails the call without
     running anything. For each blocking target, its message gives the holder
     (from the holder file on the same machine; otherwise the LFS owner and
     locked-at time), the commit the record points at, and what to do next:
     - Report this to the user.
     - If the holder is gone, the user runs `/alock:break -C <repository>
       <target>` in a Claude Code session, with the repository filled in. The
       agent can't run it.
     - Before that, message the other agents working on this repository,
       naming the target, the holder and why you believe it is gone. A live
       holder can then object. In Claude Code, that's the sessions
       `ListAgents` returns that work in this repository, its worktrees or its
       clones, through `SendMessage`, not every session it lists.
4. **In remote mode, fetch the targets' records** from `origin` into
   `refs/alock-origin/<target>`, along with the commits they point at.
5. **Check each target's records:** this clone's `refs/alock/<target>` and, in
   remote mode, `refs/alock-origin/<target>`. HEAD contains a record when
   either:
   - the commit is an ancestor of HEAD (`git merge-base --is-ancestor`); or
   - merging it into HEAD changes nothing (`git merge-tree --write-tree`),
     which covers a rebase or squash of an applied commit.

   Otherwise the call fails without running, naming the commit and its target.
6. **Record HEAD as each target's last-applied commit, before running.** A run
   that fails or is killed part-way has still changed its targets.
   - **Local mode:** one `git update-ref --stdin` transaction, checking each
     ref's old value.
   - **Remote mode:** first `git push --atomic origin`, with
     `--force-with-lease=refs/alock/<target>:<old>` for each target, `<old>`
     being the value just fetched. This publishes HEAD's commit and its
     ancestry. Then the same `git update-ref` transaction sets this clone's
     `refs/alock/<target>` and `refs/alock-origin/<target>`.

   A failed compare-and-swap means a lock was bypassed. The call stops without
   running.
7. **Run the real binary as a child.**
   - **Signals:** the child shares the shim's process group, so a signal sent
     to the group, such as a terminal's ^C or a harness stopping the command,
     reaches it directly and only once. Forwarding would deliver it twice, and
     a second interrupt makes OpenTofu stop without cleaning up. The shim
     itself waits signals out until the child exits.
   - **Locks:** in local mode the child inherits the lock descriptors, so its
     locks last as long as it does, even if the shim is killed first.
8. **Release every lock and delete its holder file** once the child has
   exited, so this also happens on failure or interrupt. A lock that fails to
   release keeps its holder file, so this machine's next run can release it
   as an orphan.

**Exit codes.** A call alock refuses exits without running anything:
- **75:** a lock stayed held past the timeout.
- **77:** a rule refused it: a dirty worktree, a missing record, a plan not
  made from HEAD.
- **78:** configuration needs the user: `alock.mode`, or `.alock.json`.

Otherwise the shim exits with the child's status.

A saved plan must come from the HEAD it's applied at. When an apply names a
plan file, the shim reads the commit it was made from out of the file's name
(`<anything>@<sha>.tfplan`) and refuses one that isn't HEAD. OpenTofu and
Terraform themselves already refuse a saved plan whose state has moved on.

## Lock timeout

- **Where it's set:** a `lock_timeout` option in the plugin's `userConfig`, in
  seconds, with a default of 60. It is shown in `/config` and saved under
  `pluginConfigs` in `settings.json`.
- **How it reaches the shims:** Claude Code exports options to hook processes
  only, not to Bash commands. The SessionStart hook therefore writes it to
  `CLAUDE_ENV_FILE` as `ALOCK_TIMEOUT`.
- **It counts against the Bash call's own timeout,** two minutes by default.
  An apply long enough to need more runs with a longer timeout or in the
  background, like any other long command.

## Orphans and `break`

- **Local mode** has no orphans. A lock lasts exactly as long as the processes
  holding it, and ending them is the break.
- **Remote mode, this machine:** a lock whose holder file names only dead pids,
  checked against their start times, was left by a killed run. The next shim
  run here releases it. Its record already names the commit that run started.
- **Remote mode, anywhere else:** a lock stays held until the user runs the
  plugin's `/alock:break [-C <repository>] <target>...` command in a Claude
  Code session.
  - **What it does:** it force-unlocks the target's LFS lock, deletes its
    holder file if one is on this machine, and leaves the records as they
    are. Its output reaches the agent with the command's instructions: tell
    the user what was broken and whose it was, then tell every other agent it
    can reach through the harness that the lock was broken.
  - **Which repository:** the command's injection runs in the session's
    working directory, so `-C` names the repository holding the lock when
    that's elsewhere. In a repository without `.alock.json`, the script
    reports that there's nothing to break and touches nothing.
  - **The break itself** is the command's `` !`…` `` shell injection, which
    Claude Code runs when the command is invoked, before the agent sees
    anything. Its script lives in the plugin's `libexec/`, off `PATH`, and
    refuses to run without `--from-skill`.
- **Only the user can run `/alock:break`:**
  - **The skill sets `disable-model-invocation: true`.** In testing, Claude
    Code refused the agent's Skill call itself, before any PreToolUse hook
    ran. Earlier in the same `--plugin-dir` session, before a
    `/reload-plugins`, the same call ran. Why is unknown.
  - **So the PreToolUse hook also denies Skill calls** that invoke
    `alock:break`, as a backstop for whenever Claude Code lets one through.
    The user's typed command is expanded by Claude Code without a Skill call.
  - **The skill's injection doesn't pass through the PreToolUse hook,** as
    observed. Every Bash call naming the script is therefore the agent's, and
    the hook denies them all.
  - **A log:** the hook appends each attempt it denies, Bash or Skill, to
    `${CLAUDE_PLUGIN_DATA}/break-events.jsonl`. Skill calls that Claude Code
    refuses first never reach the hook, so they aren't logged. The calls hold
    targets, not secrets.
  - **If the agent invokes it anyway,** the command's text tells the agent to
    say so to the user first.

**Failures:** if the locks API or `origin` is unreachable or erroring in remote
mode, the call fails closed. If acquisition fails part-way, everything taken
is released before reporting.

## Hooks

- **SessionStart** writes to `CLAUDE_ENV_FILE`:
  - `ALOCK_SESSION=<session_id>`, the identity in holder files;
  - `ALOCK_TIMEOUT`;
  - the plugin's `bin/` first on `PATH`, unless it already is, so the shims
    come before the real binaries whatever order Claude Code adds them in.

  It then reports on the mode:
  - **With the mode unset or invalid,** in every session while the plugin is
    installed, it shows the user a `systemMessage`. The message says that
    changes in alock repositories are refused until a mode is set, and gives
    the `git config` command to set it. It also adds context telling the
    agent that only the user can set the mode and that it should ask them to.
  - **With the mode `bypass`,** it adds context telling the agent that tf and
    Ansible changes run unchecked, as the shims' warning lists, and that it
    should tell the user so.
  - **Otherwise,** it prints the mode, the held locks with their holders, and
    every target whose records this HEAD doesn't contain, fetching `origin`'s
    first in remote mode.
- **PreToolUse (Bash)** denies, with a reason the agent sees:
  - calls naming the break script in the plugin's `libexec/`;
  - `git config` changing the `alock` section or any of its keys: setting,
    unsetting, renaming or removing them, or opening the editor. Reading them
    is allowed;
  - `git -c alock.…`, and `GIT_CONFIG_*` assignments naming `alock`;
  - calls to a real `tofu`, `terraform` or `ansible-playbook` by absolute path.
- **PreToolUse (Edit, Write)** denies edits to git config files (a clone's
  `config` and `config.worktree`, `~/.gitconfig`, `$XDG_CONFIG_HOME/git/config`
  and the system file) whose old or new text mentions `alock`.

The hooks parse commands and can be fooled. With the shims, they guard against
mistakes, not against an agent set on getting around them.

## Using it in a repository

- **`.alock.json`** at the repository root.
- **`alock.mode`,** set once by each user, globally or per clone.
- **The committed `.claude/settings.json`** declares the marketplace in
  `extraKnownMarketplaces` and the plugin in `enabledPlugins`.
  - **To pin a release tag,** use a `github` marketplace source with `ref`.
  - **To pin an exact commit,** use an inline `settings` marketplace instead:
    a marketplace source can pin only a branch or tag, while a plugin source
    also takes `sha`.
- **A one-line project PreToolUse hook** that denies changing `tofu`,
  `terraform` and `ansible-playbook` calls when `ALOCK_SESSION` is unset.
  That fails closed in a session where the plugin didn't load.

## Not covered

- **Changes by other means:** ad hoc `ansible`, `pct`, `qm`, `ssh`, and web
  interfaces.
- **Tasks with `check_mode: false`:** `--check` runs take no locks, so these
  tasks must stay read-only.
- **Delegated tasks:** an Ansible run can reach objects outside its hosts, such
  as a host's account on a directory server. Only the hosts are locked.
- **Shells without the shims on `PATH`,** such as a human's own terminal,
  unless they put the plugin's `bin/` there themselves.

## Implementation

Python 3, standard library only, plus `git` and `git-lfs`. Ansible already
needs Python on the machine that runs it.

| Path | Contents |
|---|---|
| `bin/` | the `tofu`, `terraform` and `ansible-playbook` shims, and nothing else |
| `libexec/shim.py`, `hook.py`, `break.py` | entry points for the shims, the hooks and `/alock:break` |
| `libexec/alock/` | the shared code |
| `skills/break/SKILL.md` | the `/alock:break` command |
| `hooks/hooks.json` | the SessionStart and PreToolUse hooks |
| `tests/` | end-to-end tests against stub binaries |

**Tests** run the shims against stub `tofu`, `ansible-playbook` and
`ansible-inventory` binaries, in throwaway repositories with their own git
config, so nothing real is touched. In remote mode:
- **`origin`** is a local bare repository;
- **`git lfs`** is replaced by a file-backed stand-in, through `ALOCK_GIT_LFS`,
  the one setting that exists for tests.

```sh
python3 -m unittest discover -s tests -v
```
