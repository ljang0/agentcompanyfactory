# Architecture

AgentCompanyFactory implements company environment generation and validation.
Research, world construction, worker desktops and task evaluation are the core
components of this repository.

The staged pipeline separates construction, acceptance and evaluation. Each stage
writes receipts alongside its outputs. Downstream commands check those inputs and
hashes before reusing a checkpoint; a saved file alone does not count as acceptance.

## Code map

| Area | Main modules under `src/company_envs/` |
| --- | --- |
| Commands and configuration | `__main__.py`, `config.py`, `doctor.py`, `preflight.py` |
| Research and dossiers | `pipeline.py`, `sources.py`, `research_bundle.py` |
| Canonical world and native population | `world/blueprint.py`, `world/population.py`, `world/population_*.py` |
| World acceptance | `world/world_acceptance.py`, `world/runtime_acceptance.py` |
| Apps, worker access and desktops | `world/hub_app.py`, `world/hub_world.py`, `world/hub_identity.py`, `world/hub_vm.py` |
| Tasks and assessments | `world/task_author.py`, `world/task_assessment.py` |
| Reference and grading | `world/golden.py`, `world/python_verifier.py`, `world/staged_calibration.py`, `world/staged_grading.py` |
| Team trials | `world/staged_trials.py`, `world/trial_access.py`, `world/regrade_trial.py` |
| Evidence and distribution | `receipt.py`, `pilot_status.py`, `collaborator.py` |

`catalogs/` defines app schemas and probes. `.agents/skills/` supplies authoring and
review instructions. `scripts/pilots/sanmar/` contains the historical SanMar population
helpers; these are still specific to that pilot.

## Company layout

A full exported company contains:

```text
company.json, apps.json, MANIFEST.json
research/          captured evidence and provenance
world/             canonical history, native states, identities and desktops
world/acceptance/  review and runtime evidence
world/FROZEN.json  accepted baseline and source fingerprints
tasks/<task>/      assignment, assessment, reference, verifier and calibration
runtime/           trial state, delivered files, actor history and desktop traces
proofs/            indexes of retained trials and grades
```

The small Git example is an inspection selection, not a runnable company folder.
Download the release archive for the complete layout.

## Execution boundaries

Workers use their own app proxies and generated desktops. The private assessment,
reference and verifier remain on the host. A teacher receives reference guidance;
an ordinary team does not. Trial outputs retain actor attribution so grading can
check both the business result and whether another worker consumed a contribution.

Python verification runs under Linux bubblewrap with read-only inputs, restricted
resources and no network or host credentials. Semantic judgments use a separate
model allowance. Reference replay uses native app APIs; desktop trials use real
VMs and browser actions. Their receipts answer different questions.

The older `generate`, batch and `run-company` routes remain for compatibility.
Use the numbered stage commands for the shared-world pipeline described here.
