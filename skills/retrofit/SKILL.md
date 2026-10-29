---
name: retrofit
description: Note the commits applied before alock followed rebases, so rebasing them keeps their records. The user runs this once per clone, as /alock:retrofit [-C <repository>].
disable-model-invocation: true
argument-hint: [-C <repository>]
allowed-tools: Bash(python3 *)
---

alock's retrofit ran for `$ARGUMENTS`. Its output:

!`python3 "${CLAUDE_PLUGIN_ROOT}/libexec/retrofit.py" $ARGUMENTS`

Tell the user what was noted, or why nothing was. If the output says to run it
in other clones, pass that on. Don't apply anything as part of this command.
