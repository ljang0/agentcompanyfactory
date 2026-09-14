# Contributing

Start with the [architecture](docs/ARCHITECTURE.md) and [stage guide](docs/PIPELINE.md).
Issues and pull requests are welcome. Describe the problem, the behavior you
changed, and how you checked it. Include a small reproducer for a bug when possible.

See [development](docs/DEVELOPMENT.md) for extension points and focused checks.
[AGENTS.md](AGENTS.md) contains the repository-wide working guide for coding agents.

## Checks

```sh
uv sync --frozen --extra browser
uv run ruff check src scripts tests
uv run pytest -q
```

The full suite requires Node/npm, Linux bubblewrap and Chromium. It does not call
models. The [setup guide](docs/COLLABORATOR.md) covers these dependencies.

For the optional commit helper, `scripts/check.sh` runs checks without committing.
Supplying a message and explicit paths formats those paths, checks the source,
commits those paths and pushes to `origin/main`:

```sh
scripts/check.sh "Fix export handling" src/company_envs/world/hub_vm.py tests/test_hub_vm.py
```

Use ordinary Git commands if you prefer. Review your diff before either approach.
Never add credentials, local configs, model account directories, VM disks or caches.

## Evidence and tests

Add a regression test when fixing behavior that could change results. An offline
test passing does not establish that a browser, desktop trial or task succeeded.
Report those separately, with the measured receipt or an explicit untested limit.

Treat frozen worlds and accepted task files as versioned data. A data or runtime
change can invalidate their acceptance hashes; retain the old evidence and create
new proof for the changed inputs. Keep assessments, references and graders off
worker desktops. Tasks sharing a world should remain in the same benchmark split.

The published inspection archive is the measured September 14 snapshot. Source
changes in this repository do not retroactively change its grades or verification.
