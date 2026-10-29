---
name: break
description: Force-unlock alock locks whose holders are gone. Only the user runs this, as /alock:break [-C <repository>] <target>.
disable-model-invocation: true
argument-hint: [-C <repository>] <target>...
allowed-tools: Bash(python3 *)
---

alock's break ran for `$ARGUMENTS`. Its output:

!`python3 "${CLAUDE_PLUGIN_ROOT}/libexec/break.py" --from-skill $ARGUMENTS`

Only the user may run this command. If you invoked it yourself rather than the
user typing it, say so to the user first, plainly, whatever the output says.

Then:

1. Tell the user what was broken, whose lock it was and since when, and what
   the output says about any target that wasn't broken.
2. For every target marked BROKEN, message the other agents working on this
   repository (in Claude Code: the sessions `ListAgents` returns that work in
   this repository, its worktrees or its clones, through `SendMessage`; not
   unrelated sessions). Say which lock was broken, whose it was, and who
   broke it. A holder that was still running must stop applying to that
   target and check with the user.
3. Don't apply anything yourself as part of this command.
