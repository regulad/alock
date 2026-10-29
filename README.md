# alock

A Claude Code plugin that keeps parallel agents from overlapping or undoing
each other's OpenTofu, Terraform and Ansible applies.

Agents in separate git worktrees keep their edits apart, but they all apply to
the same live infrastructure. alock guards against the two ways that goes
wrong:

- **Overlap:** two runs change the same target at once. alock locks each
  OpenTofu or Terraform root, and each Ansible host, while a change runs.
- **Silent revert:** an agent applies from a branch that lacks a change someone
  else already applied, and undoes it. alock records the commit each target was
  last applied from, and refuses an apply whose HEAD doesn't contain it.

Agents run `tofu`, `terraform` and `ansible-playbook` as usual. alock's shims
pass read-only calls straight through and wrap the ones that change something.
[DESIGN.md](DESIGN.md) has the whole design.

## Requirements

- Claude Code, on Linux or macOS
- Python 3.8 or later, and git 2.38 or later
- For remote mode, git-lfs and a remote whose host supports the Git LFS
  locking API, such as GitHub

## Install

```sh
claude plugin marketplace add regulad/alock
claude plugin install alock@alock
```

To have every session in a repository load it, commit this to the repository's
`.claude/settings.json`, pinned to a release tag:

```json
{
  "extraKnownMarketplaces": {
    "alock": {
      "source": { "source": "github", "repo": "regulad/alock", "ref": "alock--v0.2.0" }
    }
  },
  "enabledPlugins": { "alock@alock": true }
}
```

## Set up

1. **Set the mode** in your own terminal. Agents can read it but not change it.

   ```sh
   git config --global alock.mode local    # every agent applies from one clone
   git config --global alock.mode remote   # agents apply from several clones or machines
   git config --global alock.mode bypass   # no locking or checks at all
   ```

   Leave out `--global` to set it for one clone. Until it's set, alock warns at
   every session start and refuses changes in configured repositories.

2. **Opt a repository in** with `.alock.json` at its root. It maps each
   OpenTofu or Terraform root to the Ansible inventory listing the hosts that
   root creates, so applying the root also locks those hosts:

   ```json
   {
     "tf": {
       "tofu/cluster": {
         "hosts": {
           "dir": "ansible",
           "inventory": ["inventory/cluster.terraform_state.yml"],
           "path": ["bin"]
         }
       },
       "tofu/storage": {}
     }
   }
   ```

   - **`dir`:** the directory `ansible-inventory` runs in, relative to the
     repository root.
   - **`path`:** directories, relative to `dir`, put first on its `PATH`.

   Repositories without an `.alock.json` are untouched. In one that has it, a
   root it doesn't list is refused, so a new root can't be applied unlocked.

3. **Optionally set the lock timeout,** 60 seconds by default, with
   `/plugin configure alock`.

## Working with it

- **Commit before applying.** Changes apply from a clean worktree, from the
  commit at HEAD.
- **A held lock** makes a change wait, up to the timeout. Then it fails without
  running, naming the holder. If the holder is gone, the user runs
  `/alock:break -C <repository> <target>`. Agents can't.
- **To roll a target back,** `git revert` the applied commit on a branch that
  contains it, and apply the revert. alock has no other way to undo an apply.
- **Once an apply moves a target's record forward,** agents in other worktrees
  or clones can't apply to that target until they contain the commit. The
  shim says so after the apply, and encourages the agent to tell the others so
  they can rebase onto it.
- **Rebase freely.** Rebasing, amending or fixing up a commit after applying
  it is fine in an agent's session: alock follows the rewrite through git
  notes. A hosting service's rebase-merge is followed too. A squash merge is
  followed until its lines change again; to keep it simple, apply from the
  squash commit once it lands. For your own terminal's rebases to be followed
  as well, run `git config --global --add notes.rewriteRef refs/notes/alock`.
- **Upgrading from 0.1.0,** run `/alock:retrofit` once in each clone. It notes
  the commits 0.1.0 applied, so rebasing them is followed from then on. A
  rebase made before the retrofit can't be followed.
- **Remote mode** keeps its locks with the remote's Git LFS locking API and its
  records as refs under `refs/alock/` on `origin`, so an apply pushes the
  commit it applies from. It also pushes the rewrites it has followed, under
  `refs/alock-rewrites/`, so other clones can follow them too.

## Test

```sh
python3 -m unittest discover -s tests -v
```

The tests run the shims against stub binaries in throwaway repositories. In
remote mode they use a local bare repository and a file-backed stand-in for
`git lfs`, so nothing real is touched.

## License

Copyright © 2026 Parker Wahle.

alock is free software: you can redistribute it and/or modify it under the
terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; see [LICENSE.md](LICENSE.md) for details.
