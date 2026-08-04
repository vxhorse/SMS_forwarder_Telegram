@AGENTS.md

## Claude Code specifics

The instructions above are the authoritative ones and are shared with every
other agent tool working in this repository. Only notes that are genuinely
specific to Claude Code belong below.

- `.superpowers/` is scratch space for multi-agent runs and is git-ignored.
  Nothing in it is part of the project.
- `docs/superpowers/` holds planning documents from earlier work and is
  git-ignored on purpose — the repository stays focused on what users need, not
  on how it was built.
- `docs/deployments/` holds local records that name specific hosts, and is
  git-ignored for that reason. Like the two above it is not part of the
  project, and nothing in it belongs in a comment, a log message or a commit.
