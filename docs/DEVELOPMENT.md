# Development

Install with `make setup`. Use Python 3.13 to match CI, plus Node/npm, Linux
bubblewrap and Chromium for the runtime checks. No test should make a live model
call; the shared test fixture enforces that boundary.

```sh
make lint
make test
# Or both:
make check
```

For a focused change, invoke the relevant test module directly:

```sh
uv run pytest -q tests/test_collaborator.py
uv run pytest -q tests/test_render_check.py
```

Install the CLI headless-shell build and select the version matching Playwright:

```sh
make browser
export COMPANY_ENVS_BROWSER="$(uv run python scripts/browser_path.py)"
make check
```

The render check uses Chromium's `--dump-dom` interface. The full Chromium 151
build installed by Playwright timed out on that interface in local and CI probes;
the matching headless-shell build passed the hydration and stuck-page checks.
A compatible Chrome on `PATH` can also be used. [Playwright documents the separate
headless-shell distribution](https://playwright.dev/python/docs/browsers#chromium-headless-shell).
Some catalog and app-source checks activate only when the external hub exists.
A CI pass therefore does not certify a new native app or a real desktop trial.

## Where to make a change

| Change | Start here | Verify |
| --- | --- | --- |
| Company and workflow contracts | `src/company_envs/schemas.py`, `.agents/skills/` | Schema and author/reviewer payload tests |
| Configuration or CLI behavior | `config.py`, `__main__.py`, `doctor.py`, `preflight.py` | Config selection, CLI and prerequisite checks |
| App support | `catalogs/`, `world/hub_*.py` | Schema, worker access, native persistence and reset |
| Canonical history or population | `world/blueprint.py`, `world/population_*.py` | Record joins, provenance, volume and world coherence |
| Task assessment or grading | `world/task_assessment.py`, `world/staged_grading.py`, `world/python_verifier.py` | Alternative solutions, negative cases, attribution and verifier isolation |
| Desktop policy or trials | `world/hub_vm.py`, `world/staged_trials.py` | Export, access, budgets, actor evidence and exact reset |
| Distribution | `collaborator.py` | Payload selection, portability, hashes and clean extraction |

Paths in the table are relative to `src/company_envs/` unless a full path is shown.
Read [architecture](ARCHITECTURE.md) for the data flow and boundaries.

## Change evidence deliberately

A code change can invalidate the fingerprints in accepted world or task receipts.
Retain the prior snapshot and generate the relevant new proof; do not edit old
hashes to make a changed runtime appear accepted. The published SanMar archive
stays unchanged even as this repository develops.

For behavioral fixes, include a regression that exercises the failure. For app or
VM changes, also record which native or desktop checks ran. Distinguish mechanical
verification, semantic judgment and real team outcomes in the pull request.

## Fixtures and generated outputs

`tests/fixtures/`, `companies/cort/` and the small `experiments/` directories contain
retained test inputs. They are not active experiments. Keeping their historical
paths preserves existing test and evidence references.

Use ignored `workspace/` and `runs/` for new work. Large artifacts belong in a
curated release archive with checksums and a readable output index. Package only
the evidence needed to inspect or reproduce the stated result.
