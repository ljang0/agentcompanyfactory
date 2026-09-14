# Measured status and remaining work

The September 14, 2026 SanMar inspection snapshot demonstrates the pipeline from accepted
research through world construction, tasks, grading, desktop trials and a clean
native replay/reset. It is an inspection release, not a fully accepted team benchmark.

| Stage | Measured result |
| --- | --- |
| 1. Foundations | Accepted research, source provenance and prerequisite checks |
| 2. Canonical world | Accepted shared history, identities and worker materials |
| 3. Native population | Eight app states populated and checked |
| 4. Acceptance | Three independent reviewers accepted; all runtime gates passed; baseline frozen |
| 5. Tasks | Two independently reviewed tasks and assessments accepted |
| 6. Verification | Both calibrated; ten cases each also checked in losslessly encoded form |
| 7. Team trials | Acquisition teacher passed; assortment did not achieve a full pass; ordinary and ablation gates incomplete |
| 8. Inspection | Clean extraction, both native reference replays, isolated mechanics and exact reset passed with zero model calls |

The [example index](../examples/sanmar/README.md) links to the original receipts.
The release archive retains nine attempt receipts, available grades and complete
traces for the latest teacher of each task and the latest ordinary trial.

## Second company

The [thoughtbot-derived company](../examples/thoughtbot/README.md) contains four
workers, seven apps and a year of operating history. Its original world snapshot
reached Stage 4 through the shared staged commands. All three content reviewers
accepted it, retaining seven warning-level findings. Native acceptance covered all
seven gates and 28 worker/app views; a clean extraction repeated those checks and
reset with no model calls.

The continuation reuses that company's accepted inputs and adds two independently
reviewed tasks: product scope and release readiness. Both native references passed
replay, isolated mechanics and exact reset. Both graders passed all 20 calibration
checks, including complete losslessly encoded evidence.

The first teacher attempts exposed a Jira issue-view crash and a stale-tab write
bug. The runtime was repaired, accepted again and explicitly rebound to the same
business world before reference replay and new trials. The second teachers reached
their execution limits. Product scope scored 0.30 after a lossless encoding repair
allowed its saved evidence to be graded; release readiness scored 0.25. Missing
final deliverables remained failures. See [the repair record](PIPELINE-REPAIRS.md).

The final teacher attempts used a budget declared before those runs: 3,000 seconds,
250 actions per worker and 800 shared worker model calls. Subsequent ordinary and
ablation comparisons must use those same limits. Earlier trials and their original
limits remain recorded.

| Task | Third teacher result | Next gate |
| --- | --- | --- |
| Product scope | Passed: 1.00, all four business criteria, all three collaboration checks, reset passed | Ordinary attempt 1 scored 0.70 and failed; all collaboration checks and reset passed |
| Release readiness | Failed: 0.80; code reviews, scope, Jira work and all four collaboration checks passed, but the client draft was empty; reset passed | Three scored teachers used; ordinary trial is blocked for the current task |

The ordinary team corrected all eight tracker descriptions and produced a shared
brief and client draft. It left the existing summer research summary unchanged.
All three judges failed the first criterion on that basis. The public brief names
the tracker corrections and a shared explanation, but does not explicitly name
that existing summary; seeded correspondence establishes its relevance. This
public-brief/assessment alignment needs review before interpreting the score gap
as evidence of difficulty. The recorded grade remains unchanged.

No ordinary retry or ablation was run. There was no runtime or grading failure to
repair, and the predeclared ablation plan requires a successful ordinary baseline.
The [task inspection release](https://github.com/ljang0/agentcompanyfactory/releases/tag/thoughtbot-inspection-2026-09-14)
retains all seven trial attempts, the alignment note, calibration history, runtime
repair evidence and complete latest teacher and ordinary traces. Its separate
verification receipt identifies the archive and fresh replay proof by SHA256.

Population and runtime work required operator-directed repairs. This second company
exercises the shared pipeline; unattended generation for arbitrary companies remains
unproven.

## Remaining work

- Diagnose the assortment task's failed dependency consumption and later failed
  business outcomes before considering a new task revision. Its existing task
  reached the three-scored-teacher limit; infrastructure errors did not count.
- Obtain a successful ordinary acquisition trial after the unscored provider outage.
- Measure worker and input ablations against successful ordinary trials. Missing
  dependency evidence alone does not prove a reduction in business quality.
- Review the product-scope brief's required research-summary output before a new
  task version or a claim about difficulty. Preserve the existing grade and trials.
- Review release-readiness completion behavior before a new task version; its
  three scored teachers are exhausted and the final client draft remained empty.
- Reduce the operator-directed repairs still needed during population. The second
  company exercised the shared path; unattended generation for arbitrary companies
  remains unproven.

Reuse matching accepted checkpoints. Do not reseed or repeat paid review to obtain
a different result on unchanged data. Changed task or world inputs require new
lineage and the relevant acceptance checks. Earlier failures remain evidence.

The existing archive records `inspection_verified_not_published` because its
verification preceded this GitHub release. That historical receipt remains intact;
GitHub publication does not promote any benchmark gate.
