# Working on AgentCompanyFactory

This guide applies to the repository. Its supported scope is **company environment
generation and validation**: research dossiers, operating worlds, worker desktops,
tasks, references, verifiers and team trials. Keep implementation and documentation
within that scope; describe only capabilities the code actually supports.

Use [README.md](README.md) for the project explanation,
[docs/PIPELINE.md](docs/PIPELINE.md) for stage operation, and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the code and data layout.

## Before changing anything

1. Check `git status --short`, `git branch --show-current`, and `git remote -v`.
   Use `git rev-parse --show-toplevel` to confirm the checkout, especially when
   working inside a larger development workspace.
2. Preserve existing edits, accepted inputs and saved outputs. Do not reset,
   clean, reseed or overwrite unrelated work to obtain a tidy tree.
3. Read the relevant module and nearby tests. Use `rg` and `rg --files` to locate
   contracts, callers and fixtures before changing an interface.
4. For an existing company, inspect its status and receipts before running a stage.
   Reuse matching checkpoints. A missing external dependency is a setup issue,
   not a reason to regenerate the company.

Keep code changes narrow and explanations concrete. State the behavior, inputs,
outputs and measured checks; avoid promotional language and unsupported claims.
Keep the code lean as an ongoing constraint: extend an existing entrypoint or
contract before adding another layer, keep company data out of generic modules,
and remove duplication within the files you change. Preserve historical evidence
and avoid unrelated cleanup. New scripts need a clear owner and a documented use.

## Setup and prerequisites

The package requires Python 3.12+. CI uses Python 3.13, Node 22, uv 0.10.2 and
Ubuntu 22.04. Dependency versions come from [uv.lock](uv.lock); follow the current
[CI workflow](https://github.com/ljang0/agentcompanyfactory/blob/main/.github/workflows/ci.yml)
if these setup values change.

```sh
make setup
make doctor
make smoke
```

`make setup` installs locked dependencies with the browser extra and creates
`configs/company.local.toml` only if it does not exist. `make doctor` checks the
configuration, catalogs and pins without inference. The catalogs are already
included; `bootstrap` is for a deliberate catalog import, not routine setup.

| Work | Additional requirements |
| --- | --- |
| Read example outputs | None beyond a file viewer or browser |
| Run regression tests | Node/npm, Linux bubblewrap and a compatible headless browser for runtime checks |
| Serve native apps or replay references | Compatible CUA-Gym hub checkout, Node/npm, local socket access; bubblewrap for isolated verification |
| Run world browser acceptance | Browser extra, browser executable and native app prerequisites |
| Author or independently review content | Configured model CLI/account, writable profile and available provider capacity |
| Run desktop teams | QEMU/KVM, access to `/dev/kvm`, sshpass, compatible desktop image and guest browser distribution |

The external hub and VM image are not bundled. See
[getting started](docs/GETTING-STARTED.md) for acquisition and setup requirements.
The guest uses Python GTK 3 bindings and xdotool for clipboard text entry.

### Browser setup that matches CI

```sh
make browser
export COMPANY_ENVS_BROWSER="$(uv run python scripts/browser_path.py)"
```

The helper selects the headless-shell build associated with the installed
Playwright version. Do not hardcode a cached browser revision. The full Chromium
151 build timed out on the render check's `--dump-dom` interface during local and
CI probes; the matching headless shell passed. A compatible system Chrome can also
work. See [development](docs/DEVELOPMENT.md) for the recorded distinction.

### Configuration

Select a config explicitly for live work:

```sh
uv run company-envs --config configs/company.local.toml doctor
uv run company-envs --config configs/company.local.toml preflight --scope runtime
uv run company-envs --config configs/company.local.toml preflight --scope models
```

Selection order is `--config`, then `COMPANY_ENVS_CONFIG`, then root `config.toml`.
Paths resolve relative to the selected TOML, and `~` expands to the user's home.
An explicit missing config is an error. Resumed research runs keep their captured
configuration. The root config retains broader regression defaults; use the starter
preset for a new company.

| Setting | Purpose |
| --- | --- |
| `design.hub_root`, `design.hub_revision` | External hub location and exact runtime revision |
| `design.available_runtime_apps` | Allowed app surface; each app needs a catalog contract |
| `design.model_pin`, `models.*` | Model identifiers, review settings and account profiles |
| `models.codex_homes` | Profile directories; an empty list uses the default account |
| `design.vm_base_image`, `design.vm_browser_dir` | Guest assets for desktop trials |
| `generation.*`, `design.*`, `worker_policy.*` | Scale, review counts, call budgets and execution limits |

[configs/company.example.toml](configs/company.example.toml) starts with one company,
two tasks, four workers and the pilot's eight-app surface. Its hub revision is
`1e50b797200f8afd6f11ca8e3ee04412de97b0f2`. It allows 500 seed calls, 450 shared
worker model calls per trial, 150 actions per worker and 1,500 seconds for trial
preparation, execution and export. Grading has a separate allowance. Read the selected config
and actual receipts when estimating use; these values are defaults, not usage claims.

Keep credentials and machine-specific paths in ignored `*.local.toml` files or
external account directories. Never commit account files, keys or model histories.

## Commands and side effects

The Python import is `company_envs`; the CLI is `company-envs`. Use
`uv run company-envs COMMAND --help` for the exact arguments.

| Command | What it does |
| --- | --- |
| `make doctor` | Reads prerequisites and catalogs; no models, app builds or VMs |
| `make preflight` | Measures runtime socket access; no inference |
| `make smoke` | Runs two bundled native-server integration checks; requires Node, no models or VMs |
| `preflight --scope models` | Measures disposable profile writes and model environment access; no inference |
| `make status COMPANY=workspace/companies/ID` | Reads saved pipeline status without model calls |
| `verify-world` | Starts apps, checks worker views and browser writes, verifies persistence and reset; no models |
| `verify-collaborator` | Checks an extracted package and performs native reference replay, isolated mechanics and reset; no models |
| Authoring, review, calibration, `trial-task` | May use models and, for trials, real desktops |
| `diagnose-trial`, `regrade-trial` | Inspect or regrade saved evidence; may use judgments but do not rerun workers |

Model-using commands are part of explicitly scoped pipeline work, not routine
setup, documentation checks or CI. Read budgets before using them. Account-file
presence does not establish authentication, quota or provider availability.

## Pipeline contract

The implemented sequence builds and accepts the world before creating its tasks.
Run one stage at a time and inspect its result. The full commands and repair options
are in [docs/PIPELINE.md](docs/PIPELINE.md).

| Stage | Main entrypoints | Evidence to inspect |
| --- | --- | --- |
| 1. Foundations | `export-company --dossier-only`, `doctor`, `preflight` | Dossier, research provenance, app contracts and manifest |
| 2. Canonical world | `seed-core` | `world/CORE.json`, history, identities, access and desktop materials |
| 3. Native population | `populate-world` | `world/POPULATION.json`, native states and effective world |
| 4. Acceptance | `review-world`, `verify-world`, `freeze-world` | Review/runtime receipts and `world/FROZEN.json` |
| 5. Tasks | `author-tasks`, `review-task` | Accepted assignments and private assessments bound to the frozen world |
| 6. Verification | `author-golden`, `author-verifier`, `calibrate-verifier` | Independent reference, verifier and calibration cases |
| 7. Team trials | `trial-task` | Teacher/ordinary/ablation receipts, grades, exports and reset |
| 8. Distribution | `prepare-collaborator`, `verify-collaborator` | Payload manifest and clean-extraction replay/reset proof |

The research entrypoint `generate` also performs older workflow drafting. Export
its accepted dossier with `--dossier-only` to enter the shared-world sequence.
The older batch and `run-company` routes are not shortcuts around its acceptance
gates. `scripts/pilots/sanmar/` contains historical company-specific population helpers;
do not describe those helpers as a generic one-command generator.

## Checkpoints, repair and evaluation rules

- Treat accepted inputs and receipts as versioned evidence. A saved JSON file alone
  is not proof of acceptance. Validate lineage, data hashes and relevant code pins.
- `world/world.json` is the canonical base. Stage 3 additions are represented by
  `world/population/EFFECTIVE-WORLD.json`; do not silently fold them into the base.
- Reuse accepted core, population, task and calibration checkpoints when they match.
  Do not repeat paid review to seek a different verdict on unchanged data.
- Use the explicit repair commands. `patch-population` requires the prior baseline
  and old/new field values; `recheck-population` revalidates existing records after
  compiler/checker changes without model generation. Data changes invalidate proof.
- After runtime-only repair, run `verify-world`, then `freeze-world --refresh-tasks`.
  This archives old proof and bindings and requires unchanged population and content
  review. Replay references before new teams; preserve previous calibration and trial
  outcomes, including scored failures and attempt limits.
- Some receipts pin raw source bytes. Broad formatting, comments or path cleanup
  can invalidate a snapshot even when behavior seems unchanged. Format only the
  files being changed and inspect the affected fingerprints.
- Keep independent authors and reviewers separate. A writer's self-review is not
  a fresh independent vote. Preserve original review and failure receipts.
- Worker desktops contain their public assignment and permitted materials.
  Assessments, references and verifiers stay on the host. Only teacher trials
  receive reference guidance; ordinary trials use the same harness without it.
- Grade actual state, delivered files and trusted actor evidence. A business score
  of 1.0 can still fail required contribution/consumption checks. Preserve both.
- Native reference replay proves API execution and reset. It does not establish
  desktop feasibility, ordinary-team success or worker/input necessity.
- `trial-task` reopens a matching completed attempt. Use a new positive `--attempt`
  only after diagnosis. The cap is three scored attempts per task and trial kind;
  infrastructure failures that prevented grading are retained but unscored.
- Worker ablation uses `--kind worker-ablation --unavailable-worker ID`; input
  ablation uses `--kind input-ablation --input-worker ID`. A missing dependency
  check alone is not evidence of lower business quality.
- Keep tasks from one company world in the same benchmark split.

## Where to work

Paths below are relative to `src/company_envs/` unless stated otherwise.

| Change | Start here |
| --- | --- |
| CLI and config | `__main__.py`, `config.py`, `doctor.py`, `preflight.py` |
| Schemas and research | `schemas.py`, `pipeline.py`, `sources.py`, `research_bundle.py` |
| World/history/population | `world/blueprint.py`, `world/population.py`, `world/population_*.py` |
| App access and desktops | `world/hub_app.py`, `world/hub_identity.py`, `world/hub_world.py`, `world/hub_vm.py` |
| Acceptance | `world/world_acceptance.py`, `world/runtime_acceptance.py` |
| Tasks, verification and trials | `world/task_author.py`, `world/task_assessment.py`, `world/python_verifier.py`, `world/staged_*.py` |
| Packaging and status | `collaborator.py`, `pilot_status.py`, `receipt.py` |
| Native contracts and probes | Root `catalogs/` and `experiments/app-audit/probes.json` |

Authoring instructions are under [.agents/skills/](.agents/skills/). Read the relevant
`SKILL.md` when operating or changing that stage. Discovery, research, workflow
writing, review, world seeding/review, references and graders have different roles;
do not apply a similarly named skill to a different stage. The implementation and
stage contracts determine which instructions are active on the staged path.

`tests/fixtures/`, `companies/cort/` and the small `experiments/` directories are
retained regression inputs. Keep their paths stable. New work goes in ignored
`workspace/` and `runs/`; curated public selections go in `examples/`.

## Tests and change workflow

```sh
make lint
uv run pytest -q tests/test_collaborator.py  # example focused suite
make check                                # full lint and tests
```

Choose focused tests for changed behavior. Run the full suite for broad changes or
before a source release; docs-only edits need link/command validation rather than
new tests that merely repeat prose. Shared fixtures prohibit live model calls.
Some app-source checks run only when the external hub is present, so test counts
and skips depend on the host. Record skips and the scope of any native/VM checks.

Lint covers `src scripts tests`. Use `uv run ruff format PATH` only on intended
files. For catalog changes, check dependent manifest hashes and fixtures. For
packaging changes, inspect `collaborator.source_inputs`: new root files must be
selected explicitly, while docs/examples directories are included by traversal.

`scripts/check.sh` with no arguments runs checks without committing. A message
plus explicit paths formats those paths, checks, commits and **pushes to
`origin/main`**. `--all` covers only `src scripts tests .agents`; it does not include
root docs or configs. Prefer ordinary Git commands for a docs change. Stage only
intended files, inspect the staged diff, and verify the target remote before a push.

## Troubleshooting from measured runs

| Symptom | Next step |
| --- | --- |
| Browser hangs or produces empty DOM | Use the matching headless shell above; inspect the browser error instead of treating an empty page as success |
| TCP/loopback denied | Run runtime preflight and fix host socket permissions before starting app servers |
| Model profile is read-only | Configure an external writable account profile; do not copy credentials into the repo |
| Bubblewrap fails to create namespaces | Check Linux user/PID/network namespace permissions; do not replace isolated verification with unrestricted execution |
| Desktop prerequisite failure | Check `doctor --profile desktop`, `/dev/kvm`, image and guest browser paths |
| `model_unavailable` | Retain the unscored attempt and provider result; do not rewrite it as a task failure |
| Saved evidence exceeds the judge input limit | Preserve all events; diagnose the lossless encoding, recalibrate after a grading change, and regrade the saved episode without rerunning workers |
| Saved proof is stale | Compare input/code fingerprints and use the relevant recheck/repair path; do not edit hashes by hand |
| Receipt appeared to precede its own call | Filesystem timestamps can round below wall time; compare before/after file versions, as the batch refusal check does |

See [host troubleshooting](docs/SANDBOX-TROUBLESHOOTING.md) and
[pilot repairs](docs/PIPELINE-REPAIRS.md) for context. Host permission changes are
separate from environment acceptance; a settings file alone does not grant access.

## Inspection releases and reporting

[examples/sanmar/README.md](examples/sanmar/README.md) indexes the published pilot.
The small Git example is not a runnable company checkpoint. Its
[PROVENANCE.json](examples/sanmar/PROVENANCE.json) maps files to the full release
archive, which contains its exact measured source and data. Use that source when
reproducing the archive's fingerprints; do not substitute the current checkout.

The earlier [software delivery world snapshot](examples/thoughtbot/README.md)
records Stage 4, with seven retained review warnings and a separate clean-extraction
receipt. Its provenance file maps those curated files to the original archive.
The [task and team-trial continuation](examples/thoughtbot/evaluation/README.md)
reuses that company and preserves the earlier world snapshot. Product scope has
a passing teacher and a failed ordinary run with a public-brief/assessment alignment
question; release readiness exhausted its teacher limit. Keep the recorded grades
and that caveat together. See [measured status](docs/END-TO-END-PLAN.md).

`prepare-collaborator --inspection` records accepted world/tasks/calibration and
actual retained trial outcomes. Default packaging additionally enforces the full
teacher/ordinary and ablation gates. Neither packaging nor GitHub publication
promotes a missing or failed benchmark gate. Keep the measured archive immutable;
create a new version when inputs change.

Report what changed, which checks ran, what they establish, and what remains
unmeasured. The September 14 snapshot passed both native reference replays and
exact resets, but ordinary-team and ablation acceptance remains incomplete.
[Measured status](docs/END-TO-END-PLAN.md) records the task-level limits. Do not
convert an inspection release or a passing unit suite into a claim of complete
benchmark validation.
