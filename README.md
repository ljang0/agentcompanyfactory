# Agent Company Factory

Build synthetic workplaces for evaluating agents that work together across business apps.

The pipeline starts with a researched company, creates a shared operating history
and worker-specific access, then derives tasks from that world. It records native
app state, desktop actions, task grades and reset checks so a result can be inspected.

The included example is **Cedarline Apparel Supply**, a fictional company informed
by research on SanMar: four workers, eight apps and two tasks. The software and
example are a research release. Some population steps still use pilot-specific
helpers, and full team benchmark acceptance is unfinished.

## Start here

- [Inspect the example](examples/sanmar/README.md): assignments, verifiers, grades and measured limits.
- [Download the full inspection archive](https://github.com/ljang0/agentcompanyfactory/releases/tag/sanmar-inspection-2026-09-14): app states, delivered files, screenshots and replay receipts.
- [Set up and replay](docs/COLLABORATOR.md): local requirements and commands.
- [Follow the pipeline](docs/PILOT.md): eight stages and saved checkpoints.
- [Find the code](docs/ARCHITECTURE.md) or [contribute](CONTRIBUTING.md).

## What has been demonstrated

The SanMar pilot completed world acceptance, task review and verifier calibration.
Both reference solutions were replayed through native worker app proxies, checked
by isolated Python verifiers, and reset to the original baseline from a clean
extraction, with no model calls.

The acquisition teacher passed with a business score of 1.0 and all collaboration
checks. The assortment teacher reached its three-scored-attempt limit without a
complete pass. The first ordinary acquisition trial was unscored because the
model provider was unavailable. Ordinary-team success and worker/input ablations
have **not** been established. See the [evidence index](examples/sanmar/README.md).

## Development

Use Python 3.12+ and [uv](https://docs.astral.sh/uv/). The complete local test suite
also uses Node/npm, Linux bubblewrap and Chromium; setup is in the guide above.

```sh
git clone https://github.com/ljang0/agentcompanyfactory.git
cd agentcompanyfactory
uv sync --frozen --extra browser
uv run company-envs --help
uv run ruff check src scripts tests
uv run pytest -q
```

Tests make no live model calls. Running new authoring, review or desktop trials
requires a configured model account and can incur usage charges. Native replay
also requires the external CUA-Gym hub at the pinned revision; it is not bundled.
The Python package and command remain named `company_envs` and `company-envs`.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/company_envs/` | CLI, generation, app runtime, grading and packaging |
| `examples/sanmar/` | Small, readable selection of measured pilot outputs |
| `docs/` | Setup, stage commands, architecture and known limitations |
| `configs/` | Portable configuration template; local overrides are ignored |
| `catalogs/` | App contracts, probes and occupational source data |
| `.agents/skills/` | Authoring and independent review instructions |
| `tests/` | Regression tests and fixtures |
| `scripts/` | Development tools and pilot population helpers |
| `companies/cort/`, `experiments/` | Retained fixtures used by the tests |

Large run outputs live in release assets. The repository uses [Apache-2.0](LICENSE).
Research citations and original source URLs remain attached to the example data.
