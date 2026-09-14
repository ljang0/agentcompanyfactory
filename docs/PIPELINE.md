# The company pipeline

The pipeline turns accepted research into a shared workplace, then derives tasks
and evaluates teams in that workplace. Each stage produces an inspectable artifact
and a checkpoint. A later stage must validate the earlier inputs before proceeding.

Company research captures evidence about real operations, roles and software. A
synthetic dossier turns that evidence into a coherent manager/worker team. The
world author creates shared history and role-specific information, then native app
population and worker access make that world executable. Real desktop trials give
each worker its own VM.

The three boundaries are construction, acceptance and evaluation: build an ordinary
operating world, prove its apps and access work, then ask agents to do unfinished
work in that world. The [SanMar example](PILOT.md) shows a measured execution.

Use these commands one stage at a time on an exported company folder. Follow
[getting started](GETTING-STARTED.md) to obtain the accepted research input. For the
saved example, download the [inspection archive](COLLABORATOR.md) and reuse its checkpoints.
Run `company-envs COMMAND --help` for options. Prefix commands below with
`uv run company-envs --config configs/company.local.toml`.

| Stage | Commands | Checkpoint and purpose |
| --- | --- | --- |
| 1. Foundations | `export-company RUN_ID COMPANY_ID --dossier-only --output FOLDER`, `doctor --folder FOLDER`, `preflight` | Research bundle, app contracts and source provenance |
| 2. Canonical world | `seed-core FOLDER --review-rounds 2` | `world/CORE.json`: history, identities, access and desktop materials |
| 3. Native population | `populate-world FOLDER` | `world/POPULATION.json`: native app records and population checks |
| 4. Acceptance | `review-world FOLDER --round N`, `verify-world FOLDER --work WORK`, `freeze-world FOLDER` | `world/FROZEN.json`: accepted review and browser/write/reset evidence |
| 5. Tasks | `author-tasks FOLDER --count 2` | Accepted assignments and private assessments bound to the frozen world |
| 6. Verification | `author-golden FOLDER TASK`, `author-verifier FOLDER TASK`, `calibrate-verifier FOLDER TASK --work WORK` | Independent reference, verifier and calibration |
| 7. Team trials | `trial-task FOLDER TASK --kind KIND --work WORK` | Real teacher, ordinary and ablation outcomes with grades and reset |
| 8. Handoff | `prepare-collaborator FOLDER --inspection --output DEST`, `verify-collaborator --work WORK` | Curated payload, clean replay and verification receipt |

## Reuse and repairs

Matching accepted core and population checkpoints are rechecked without model
calls. Changed inputs or native state drift are refused for inspection. The raw
`world/world.json` is the Stage 2 base; `world/population/EFFECTIVE-WORLD.json`
contains accepted additions. A valid `FROZEN.json` binds the accepted baseline.

`seed-core FOLDER --resume` reviews deliberate source corrections without calling
the original world author again. It still requires unchanged dossier, app, schema,
specification and author-skill inputs. `patch-population FOLDER PATCH.json` requires
an exact previous baseline and explicit old/new field values and reasons.
`recheck-population FOLDER` handles compiler/checker updates without generating
new records. Data changes invalidate earlier acceptance.

SanMar population also used the historical helpers now under `scripts/pilots/sanmar/`.
The saved outputs are
inspectable and replayable; those helper steps are not yet a generic generator.

## Task evaluation

Tasks and assessments are authored together and independently reviewed. Each
worker's contribution must be grounded in visible records, authority or work that
another participant needs. Local access checks establish record visibility;
necessity is a separate empirical question for team and ablation trials.

Reference and verifier authors are independent. Calibration checks full, partial,
no-op, alternative and negative outcomes. Python mechanics run in bubblewrap;
semantic judgments evaluate business meaning and consumed contributions.

Trial kinds are `teacher`, `ordinary`, `worker-ablation` and `input-ablation`.
Worker ablation requires `--unavailable-worker WORKER_ID`; input ablation requires
`--input-worker WORKER_ID`. Only the teacher receives reference guidance. Teacher and ordinary outcomes are
recorded separately; a model or infrastructure outage is not evidence that a task
is difficult. The current pipeline preserves both successes and failures, and its
packaging modes have the explicit acceptance requirements described below.

Use a new positive `--attempt` only after diagnosing the prior result. Each task
allows at most three scored trials of each kind. Infrastructure failures that
prevent grading remain archived and do not consume that limit. Trial and grading
budgets are separate. The pilot allows 450 shared worker model calls, 150 actions
per worker and 1,500 seconds per worker-execution/grading phase.

`diagnose-trial` inspects an environment failure without changing its status.
`regrade-trial` can grade a healthy saved episode after verifier repair and
recalibration, preserving the original receipt. Both may use judgment calls;
neither reruns the workers.

## Inspection and full acceptance

`pilot-status FOLDER` reads the saved checkpoint index without inference.
Inspection packaging requires accepted world/tasks/calibration and preserves the
actual retained trial results. Default packaging without `--inspection` additionally
enforces successful teacher/ordinary trials and ablation evidence.

From a clean extraction, `verify-collaborator` checks payload hashes, lineage and
saved trial records, then replays references in native apps, runs isolated mechanics
and checks exact reset. It spends no model calls and does not establish new team
or semantic success. See [setup and replay](COLLABORATOR.md).
