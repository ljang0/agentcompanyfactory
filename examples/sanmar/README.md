# SanMar inspection example

Cedarline Apparel Supply is a fictional workplace informed by SanMar research.
Its four roles are sales supervisor, account representative, order coordinator
and marketing specialist. They use eight native app environments: Gmail, Slack,
Calendar, Drive, Docs, Sheets, HubSpot and Meta Ads.

Start with the [worked example](walkthrough/README.md) for a readable walkthrough
with actual screenshots, output records and the recorded grade. Then inspect
[the company](company.json) and either assignment:

| Task | Assignment | Assessment | Reference | Verifier | Calibration |
| --- | --- | --- | --- | --- | --- |
| Account assortment review | [Brief](tasks/sanmar_account-assortment-review/assignment.json) | [Criteria](tasks/sanmar_account-assortment-review/assessment.json) | [Actions](tasks/sanmar_account-assortment-review/golden.json) | [Python](tasks/sanmar_account-assortment-review/verifier.py) | [Receipt](tasks/sanmar_account-assortment-review/verifier-calibration.json) |
| Qualified acquisition review | [Brief](tasks/sanmar_qualified-acquisition-review/assignment.json) | [Criteria](tasks/sanmar_qualified-acquisition-review/assessment.json) | [Actions](tasks/sanmar_qualified-acquisition-review/golden.json) | [Python](tasks/sanmar_qualified-acquisition-review/verifier.py) | [Receipt](tasks/sanmar_qualified-acquisition-review/verifier-calibration.json) |

Assessments, references and verifiers are benchmark author materials. They are
public here for inspection and must stay off worker desktops during evaluation.
The JSON files are copied verbatim; references to `world/` or `runtime/` resolve
inside the full archive, not this small selection.

## Recorded trials

| Task / trial | Result | Evidence |
| --- | --- | --- |
| Acquisition teacher, latest | Passed; business score 1.0 and all collaboration checks | [Receipt](trials/sanmar_qualified-acquisition-review/teacher.json), [grade](trials/sanmar_qualified-acquisition-review/teacher-grade.json) |
| Assortment teacher, latest | Failed; business score 0.0 | [Receipt](trials/sanmar_account-assortment-review/teacher.json), [grade](trials/sanmar_account-assortment-review/teacher-grade.json) |
| Acquisition ordinary, first | Model provider unavailable; unscored | [Receipt](trials/sanmar_qualified-acquisition-review/ordinary.json) |

The earlier scored assortment teachers received business scores 1.0 and 0.25.
The 1.0 episode failed supervisor-to-coordinator consumption, so it was not a
complete pass. The task exhausted its three-scored-teacher limit. [All nine
retained attempts](reports/TRIAL-ATTEMPTS.json) include environment errors and
regrading history. Ordinary-team success and ablations have not been established.

Both tasks passed fresh native reference replay, isolated Python mechanics and
exact reset from a clean extraction, with no model calls. Read the
[verification receipt](reports/COLLABORATOR-VERIFIED.json) and
[snapshot validation](reports/VALIDATION.json). The latter records 3,130 passing
tests and three expected failures on the archived source; it is not a CI result
for later source revisions.

## Full outputs and provenance

[Download the inspection release](https://github.com/ljang0/agentcompanyfactory/releases/tag/sanmar-inspection-2026-09-14)
for the frozen operating world, native final states, delivered files, trusted actor
history and screenshots. Its `INSPECT.md` links the outputs directly. Earlier
attempts retain receipts and available grades; full traces cover the latest teacher
per task and the latest ordinary trial.

[PROVENANCE.json](PROVENANCE.json) maps each selected file to its archive path and
SHA-256. The archive is 217,605,890 bytes (about 208 MiB):

```text
3852830a8b15ac9682f0e330fb75c5a5e3a8a5f88eed76d32c7a594a2f0d0d7a
```

Follow [setup and replay](../../docs/COLLABORATOR.md) to inspect the complete folder.
The small example alone is not a runnable company checkpoint.
