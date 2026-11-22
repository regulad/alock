# Agent notes for `regulad/alock`

This repository is alock, a Claude Code plugin that locks OpenTofu, Terraform
and Ansible applies so parallel agents can't overlap or undo each other.
[README.md](README.md) covers its use and [DESIGN.md](DESIGN.md) its design.

## Releases

- A release commit bumps `version` in `.claude-plugin/plugin.json` and the
  `ref` in README.md's settings example, as 1a5fc27 did for 0.2.1.
- Once its CI (`test.yml`) passes on Ubuntu and macOS, tag it with
  `claude plugin tag --push`, which writes `alock--v<version>`.
