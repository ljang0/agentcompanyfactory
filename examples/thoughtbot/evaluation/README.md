# Software delivery tasks and team trials

This continuation adds two tasks to Harbor Loom Software's accepted operating
world. It reuses the original company history, native records and private worker
materials. The [earlier world snapshot](../README.md) remains available separately.

| Task | People | Work to complete |
| --- | --- | --- |
| [Product scope](tasks/thoughtbot_o-product-scope/assignment.json) | Product lead, designer and backend developer | Resolve customer-evidence ambiguities, correct four paired GitHub/Jira requirements, recommend a bounded increment and draft a client update |
| [Release readiness](tasks/thoughtbot_o-release-readiness/assignment.json) | Product lead, designer, backend and frontend developers | Review three change proposals, record remaining checks and owners, account for capacity and recommend a release scope or hold |

All three independent reviewers accepted both tasks. Each has an independent
reference, Python verifier and calibrated semantic criteria. Both references ran
through the native worker proxies, passed their mechanical checks and restored
the initial state. Each grader passed all 20 calibration checks, including the
losslessly encoded evidence path.

Assessments, references and verifiers are public for collaborator inspection.
They belong on the host; ordinary workers must not receive them. Only teacher
trials receive reference guidance.

## Trial history

The first teachers exposed a Jira detail-view crash and a stale-tab write bug.
The repairs changed runtime behavior while preserving the accepted business world.
Fresh browser acceptance and reference replay passed before the next teams ran.

The second teachers reached their execution time limits. Product scope received
0.30 after its saved evidence was successfully regraded; release readiness received
0.25. The product-scope grade initially exceeded the judge's input limit. A lossless
encoding repair preserved every event and allowed grading without rerunning workers.
The original error receipt and both resulting grades are retained.

| Task | Teacher 1 | Teacher 2 | Teacher 3 |
| --- | ---: | ---: | ---: |
| Product scope | 0.60, failed | 0.30 after saved-evidence regrading, failed | 1.00, all business and collaboration checks passed |
| Release readiness | 0.00, failed | 0.25, failed | 0.80, failed because the client draft was empty; other business and collaboration checks passed |

All six teachers reset successfully. The release-readiness task reached its
three-scored-teacher limit. It cannot proceed to an ordinary trial in its current
form. Read the [attempt history](reports/TRIAL-ATTEMPTS.json) and the latest
[scope grade](trials/thoughtbot_o-product-scope/teacher-grade.json) and
[release-readiness grade](trials/thoughtbot_o-release-readiness/teacher-grade.json).

Before the third teachers began, the execution budget was set to 3,000 seconds,
250 actions per worker and 800 shared worker model calls. Preparation and export
share the execution budget; grading has a separate allowance. The same limits
apply to subsequent ordinary and ablation comparisons. Earlier runs used 1,500
seconds, 150 actions and 450 calls. See [the recorded plan](reports/PLAN.json) and
[runtime repair details](../../../docs/PIPELINE-REPAIRS.md).

## Ordinary result and interpretation

The product-scope ordinary run scored **0.70** and failed, with all three
collaboration checks and reset passing. It corrected all eight tracker
descriptions and produced a shared brief and client draft. It did not update or
explicitly supersede the existing summer research summary, so criterion 1 failed.
Read the [receipt](trials/thoughtbot_o-product-scope/ordinary.json) and
[grade](trials/thoughtbot_o-product-scope/ordinary-grade.json).

The public brief explicitly names the tracker corrections and a shared explanation,
but does not explicitly name that existing summary. The company correspondence
establishes the summary's relevance. The [operator review note](trials/thoughtbot_o-product-scope/ordinary-review.json)
records this alignment question without changing the grade. The teacher/ordinary
score gap should not be treated as clean evidence of difficulty until the required
public output is reviewed.

No ordinary retry or ablation was run. The predeclared ablation plan requires a
successful ordinary baseline. The current task and its outcomes remain intact;
a future change to the assignment needs a separate version and review.

## Inspect and replay

Start with the [successful teacher walkthrough](walkthrough/README.md): the private
inputs, corrected research, scope decision, client draft and actual screenshots.
The task folders contain each [scope assessment](tasks/thoughtbot_o-product-scope/assessment.json)
and [release assessment](tasks/thoughtbot_o-release-readiness/assessment.json),
independent references, Python verifiers and calibration receipts.

The [full inspection release](https://github.com/ljang0/agentcompanyfactory/releases/tag/thoughtbot-inspection-2026-09-14)
contains the accepted world, all seven attempt receipts, available historical grades,
current teacher and ordinary traces, delivered files and final native states.
It also retains the runtime and calibration repairs. The earlier world-only release
remains unchanged.

[PROVENANCE.json](PROVENANCE.json) maps this selection to exact archive paths and
hashes. The [verification receipt](reports/VERIFIED.json) identifies the tested
archive, source and companion replay proof. Clean extraction checks the payload,
replays both references through native worker proxies, runs isolated mechanics and
verifies reset, with no model calls. These checks do not promote the unfinished
ordinary or ablation gates. Follow [setup and replay](../../../docs/COLLABORATOR.md)
for the full archive; this Git selection alone is not a runnable checkpoint.
