# Contributing

YANDI is experimental. Small, focused changes are easiest to review.

## Principles

- **Tests are required.** A behaviour change comes with a regression test that fails without it.
  Run `scripts/test-core.sh` before opening a pull request; see [docs/TESTING.md](docs/TESTING.md).
- **Small logical commits.** One concern per commit, with a message that explains why.
- **Do not commit runtime or private data.** No conversation history, memory or database dumps,
  logs, episode exports, model weights, credentials, or machine-specific paths. Stage files by name;
  avoid `git add .` / `git add -A`.
- **Respect the architecture invariants** (see [README.md](README.md) and
  [docs/](docs/README.md)): `MODEL != LOCATION != RUNTIME != ADAPTER != PROVIDER`;
  one generation attempt = one resolved target = one adapter; `TRUST != TRUTH`;
  `UNKNOWN != EMPTY`; `VISIBLE REPLY != INTERNAL STATE`; memory may affect a reply but must not
  become a new user event.
- **Tests use synthetic data.** Synthetic users, synthetic grievances, mocked models.

## Before you open a pull request

1. Run the core suite and any suite for the area you touched.
2. Check `git status` and `git diff --cached` for anything that should not be published.
3. Update the relevant document in `docs/` if you change documented behaviour, and add newly
   discovered limitations to `docs/KNOWN_ISSUES.md`.

Security issues: see [SECURITY.md](SECURITY.md).
