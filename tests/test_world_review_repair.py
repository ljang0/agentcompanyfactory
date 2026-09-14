"""Reviewer bookkeeping: ballots deduped by cited records, the task scope, and repairs that reach
every copy of a cited record and diff the copies afterwards. No model service."""

import json
from copy import deepcopy
from types import SimpleNamespace

from company_envs.storage import write
from company_envs.world.seed_calls import WorkerMaterial
from company_envs.world.state_seed import scope_collections, task_designs
from company_envs.world.world_review import (
    ReviewFinding,
    WorldReview,
    _by_bytes,
    bound_states,
    cited_ids,
    copy_drift,
    dedupe_findings,
    errors_first,
    make_reviewer,
    merge_ballots,
    narrow_to_cited,
    plan_repair,
    record_holders,
    repair_scope,
    review_coverage,
    review_drift,
    review_inputs,
    review_world,
    sample_collection,
    task_scope,
)

WORLD = {"contracts": [{"id": "L003", "rent": 940, "status": "active"}, {"id": "L004", "rent": 880}]}
STATES = {
    "contractbook_mock": {
        "contracts": [{"id": "L003", "rent": 940, "status": "active"}, {"id": "L004", "rent": 880}]
    },
    "google_drive_mock": {"contracts": [{"id": "L003", "rent": 950, "status": "active"}]},
    "slack_mock": {"messages": [{"id": "sm-1", "text": "hello"}]},
}
APPS = {app: {} for app in STATES}


def finding(target, issue, evidence, severity="error"):
    return ReviewFinding(target=target, severity=severity, issue=issue, evidence=evidence)


def ballot(verdict, findings):
    return WorldReview(verdict=verdict, summary=f"{verdict} reading", findings=findings)


def test_cited_ids_are_known_record_ids_only():
    holders = record_holders(WORLD, STATES)
    assert holders["L003"] == {"world", "contractbook_mock", "google_drive_mock"}
    assert cited_ids("contracts L003, L004 and sm-1; also L999 and status", holders) == {
        "L003",
        "L004",
        "sm-1",
    }


def test_ballots_paraphrasing_one_slip_are_one_issue():
    holders = record_holders(WORLD, STATES)
    ballots = [
        ballot(
            "revise",
            [finding("contractbook_mock", "Signed leases are two-sentence stubs", "contracts L003, L004")],
        ),
        ballot(
            "revise",
            [
                finding(
                    "contractbook_mock", "Most agreements are near-identical placeholders", "L003 and L004"
                )
            ],
        ),
        ballot(
            "revise",
            [finding("contractbook_mock", "Agreements are start-date/rent stubs", "contracts: L004; L003")],
        ),
    ]
    verdict, tally = merge_ballots(ballots, votes=3, scope_apps={"contractbook_mock"}, holders=holders)
    assert verdict.verdict == "revise" and len(verdict.findings) == 1
    assert verdict.findings[0].issue == "Signed leases are two-sentence stubs"
    assert tally == {
        "accepts": 0,
        "raw_findings": 3,
        "distinct_issues": 1,
        "distinct_errors": 1,
        "majority_issues": 1,
        "kept_as_warnings": 0,
    }


def test_findings_without_record_ids_dedupe_on_the_issue_prefix_and_errors_win():
    holders = record_holders(WORLD, STATES)
    same = "The delivery channel rehearses the same distinctions in every thread"
    findings = [
        finding("slack_mock", same, "threads", severity="warning"),
        finding("slack_mock", same, "channels"),
        finding("slack_mock", "Threads have replies but no parent message", "n/a"),
        finding("google_drive_mock", "L003 rent is wrong", "L003", severity="warning"),
        finding("google_drive_mock", "Drive lease shows a different rent", "contracts/L003"),
    ]
    kept = dedupe_findings(findings, holders)
    assert [(f.target, f.severity) for f in kept] == [
        ("slack_mock", "error"),
        ("slack_mock", "error"),
        ("google_drive_mock", "error"),
    ]


def test_accept_by_majority_keeps_the_dissenting_reviewer_findings_as_warnings():
    """Both delivered companies were accepted over a dissenting reviewer, and the accepting ballot
    was returned whole, so every finding the dissenter wrote was dropped on the floor. The world
    ships with them; the next reader gets to see them."""
    holders = record_holders(WORLD, STATES)
    dissent = finding("google_drive_mock", "Drive lease rent differs", "L003")
    ballots = [ballot("accept", []), ballot("revise", [dissent]), ballot("revise", [dissent.model_copy()])]
    verdict, tally = merge_ballots(ballots, votes=3, scope_apps={"google_drive_mock"}, holders=holders)
    assert verdict.verdict == "revise", "two of three reviewers point at the same target"
    ballots = [ballot("accept", []), ballot("accept", []), ballot("revise", [dissent])]
    verdict, tally = merge_ballots(ballots, votes=3, scope_apps=set(), holders=holders)
    assert verdict.verdict == "accept"
    assert [(f.target, f.severity, f.issue) for f in verdict.findings] == [
        ("google_drive_mock", "warning", "Drive lease rent differs")
    ]
    assert tally["raw_findings"] == 1 and tally["distinct_issues"] == 1 and tally["distinct_errors"] == 0
    assert tally["kept_as_warnings"] == 1 and tally["majority_issues"] == 0


def test_task_scope_adds_brief_named_apps_and_grader_collections():
    tasks = [
        {
            "decisive_collections": ["instagram_mock.savedPostIds"],
            "scope_collections": ["google_docs_mock.documents"],
            "public_assignment": {
                "title": "Return packet",
                "brief": "Check the preserved original Drive packet before replying in Zendesk.",
            },
        }
    ]
    apps = {
        a: {}
        for a in ("google_drive_mock", "Zendesk_mock", "instagram_mock", "google_docs_mock", "slack_mock")
    }
    assert task_scope(tasks, apps) == [
        "Zendesk_mock.*",
        "google_docs_mock.documents",
        "google_drive_mock.*",
        "instagram_mock.savedPostIds",
    ]
    assert task_scope([{"public_assignment": {"brief": "Reconcile the ledger."}}], apps) == []


def test_scope_collections_come_from_grader_and_golden_when_present(tmp_path):
    task = tmp_path / "companies" / "acme" / "tasks" / "acme_o1"
    assert scope_collections(task) == []
    write(task / "workflow.json", {"id": "acme_o1", "feature_cell": {"collections": ["slack_mock.messages"]}})
    write(task / "assignment.json", {"workflow_id": "acme_o1", "brief": "Do the thing."})
    write(
        task / "grader.json",
        {
            "grader": {
                "checks": [
                    {
                        "predicate": {
                            "app_id": "tableau_mock",
                            "selector": '$.calculatedFields[?(@.id=="calc-x")]',
                        },
                        "guards": [{"app_id": "tableau_mock", "selector": "$.calculatedFields[0].formula"}],
                        "app_paths": [
                            {"app_id": "google_drive_mock", "selector": '$.items["file-demand"].content'}
                        ],
                    }
                ]
            }
        },
    )
    write(
        task / "golden.json",
        [
            {"worker_id": "w1", "app_id": "slack_mock", "action": "message", "text": "start"},
            {
                "worker_id": "w1",
                "app_id": "google_docs_mock",
                "action": "set_current",
                "state_patch": {"documents": []},
            },
        ],
    )
    expected = ["google_docs_mock.documents", "google_drive_mock.items", "tableau_mock.calculatedFields"]
    assert scope_collections(task) == expected
    designs = task_designs(task.parents[1])
    assert designs[0]["decisive_collections"] == ["slack_mock.messages"]
    assert designs[0]["scope_collections"] == expected


def test_repair_reaches_every_copy_of_a_cited_record():
    holders = record_holders(WORLD, STATES)
    findings = [
        finding("contractbook_mock", "Lease L003 rent differs from the signed copy", "contracts/L003")
    ]
    materials = [
        WorkerMaterial(worker_id="w1", path="leases/L003.md", content="Lease L003 rent USD 940"),
        WorkerMaterial(worker_id="w1", path="notes.md", content="nothing to do with it"),
    ]
    targets, cited, material_findings, feedback = plan_repair(findings, APPS, holders, materials)
    assert targets == {"contractbook_mock", "google_drive_mock"} and cited == {"L003"}
    assert [m["evidence"] for m in material_findings] == ["leases/L003.md", "contracts/L003"]
    drive = feedback["google_drive_mock"]
    assert drive[0]["issue"].startswith("Lease L003") and drive[0]["target"] == "contractbook_mock"
    assert "L003 also exist in contractbook_mock, world" in drive[-1]["issue"]
    assert "slack_mock" not in feedback


def test_world_findings_go_to_every_app_when_none_is_named():
    holders = record_holders(WORLD, STATES)
    findings = [finding("world", "the canonical totals do not reconcile", "billing totals")]
    targets, cited, material_findings, feedback = plan_repair(findings, APPS, holders, [])
    assert targets == set(APPS) and cited == set() and material_findings == []
    assert all(fb == [findings[0].model_dump()] for fb in feedback.values())


def test_copy_drift_reports_only_the_cited_records():
    drift = copy_drift(WORLD, STATES, {"L003"})
    assert drift and all(f["message"].startswith("L003.rent: ") for f in drift)
    assert {f["source"] for f in drift} == {"world~google_drive_mock", "contractbook_mock~google_drive_mock"}
    assert copy_drift(WORLD, STATES, {"L004"}) == []


def test_paraphrases_citing_overlapping_records_are_one_issue():
    """Mount Sinai's final round reported seven errors: three readings of one Azure pricing tier,
    three of one trace snapshot, and one of a completion date -- three defects. The exact-set key
    kept all seven, because a reading citing ``db-eval`` alone and one citing ``db-eval, db-clinical``
    had different keys. Overlap over half the smaller set makes them one; disjoint sets stay two."""
    ids = [
        "db-eval-id",
        "db-clinical-id",
        "occupancy-0907",
        "out-falls-0722",
        *(f"rec-{i}" for i in range(7)),
    ]
    state = {"rows": [{"id": rid} for rid in ids]}
    holders = record_holders({}, {"azure_mock": state})
    findings = [
        finding("azure_mock", "db-eval is S3, the occupancy workbook says S0", "db-eval-id, db-clinical-id"),
        finding("azure_mock", "Azure lists db-eval as S3 while every copy says S0", "db-eval-id"),
        finding(
            "azure_mock",
            "No tier change explains the S3 tier",
            "db-eval-id; db-clinical-id; occupancy-0907",
        ),
        finding(
            "azure_mock",
            "out-falls-0722 completed September 2 in Drive, July 22 in the manifest",
            "out-falls-0722",
        ),
    ]
    kept = dedupe_findings(findings, holders)
    assert [f.issue[:20] for f in kept] == ["db-eval is S3, the o", "out-falls-0722 compl"]
    # The ids a dropped paraphrase adds are carried onto the one kept, so narrowing a repair to the
    # cited records still reaches every record the panel named.
    assert kept[0].evidence == "db-eval-id, db-clinical-id; also cited: occupancy-0907"
    assert kept[1].evidence == "out-falls-0722"
    # Overlap, not chaining: rec-2/3/4 and rec-4/5/6 share one id of three and stay two issues.
    chain = [
        finding("azure_mock", "one", "rec-2, rec-3, rec-4"),
        finding("azure_mock", "two", "rec-4, rec-5, rec-6"),
    ]
    assert len(dedupe_findings(chain, holders)) == 2


def test_a_repair_puts_back_every_record_no_finding_named():
    """23,522 records were rewritten against 578 the reviewers cited, 41 for every one named, and
    each of those is a record the reviewers had already read, re-rolled for nothing."""
    before = {
        "emails": [{"id": "m1", "body": "one"}, {"id": "m2", "body": "two"}, {"id": "m3", "body": "three"}],
        "threads": {"general": [{"id": "t1", "text": "a"}, {"id": "t2", "text": "b"}]},
        "items": {"f1": {"id": "f1", "name": "one"}, "f2": {"id": "f2", "name": "two"}},
        "settings": {"theme": "dark"},
    }
    after = {
        "emails": [
            {"id": "m1", "body": "ONE"},  # cited: the repair stands
            {"id": "m2", "body": "rewritten for nothing"},  # not cited: put back
            {"id": "m4", "body": "a parent the reply needed"},  # added: kept
        ],  # m3 dropped though nothing named it: put back
        "threads": {"general": [{"id": "t1", "text": "A"}, {"id": "t2", "text": "b"}]},
        "items": {"f1": {"id": "f1", "name": "ONE"}, "f2": {"id": "f2", "name": "rewritten"}},
        "settings": {"theme": "light"},
    }
    state, put_back = narrow_to_cited(before, after, {"m1", "f1"})
    assert put_back == ["f2", "m2", "m3", "t1"]
    assert state["emails"] == [
        {"id": "m1", "body": "ONE"},
        {"id": "m2", "body": "two"},
        {"id": "m3", "body": "three"},
        {"id": "m4", "body": "a parent the reply needed"},
    ]
    assert state["threads"]["general"] == before["threads"]["general"]
    assert state["items"] == {"f1": {"id": "f1", "name": "ONE"}, "f2": {"id": "f2", "name": "two"}}
    assert state["settings"] == {"theme": "light"}, "not a record; the author owns it"
    # Nothing cited means nothing may change, and a collection dropped whole comes back.
    state, put_back = narrow_to_cited(before, {"emails": after["emails"]}, set())
    assert state == {**before, "emails": [*before["emails"], after["emails"][-1]]}
    assert set(put_back) == {"m1", "m2", "m3", "t1", "t2", "f1", "f2"}


def test_a_collection_a_finding_calls_uniform_may_be_rewritten_whole():
    """A uniformity defect cannot be repaired one cited row at a time, and a gate that blocks on a
    defect the pipeline cannot repair stops the pipeline. Plante Moran's two findings said all ten
    planning households carry identical balances and cited two of the sheets."""
    state = {"sheets": [{"id": f"sh{i}"} for i in range(6)], "files": [{"id": f"f{i}"} for i in range(4)]}
    holders = record_holders({}, {"google_sheets_mock": state})
    uniform = finding("google_sheets_mock", "All ten households carry identical balances", "sh0; sh1")
    allowed, wide = repair_scope(state, [uniform], holders)
    assert wide == ["sheets"] and allowed == {f"sh{i}" for i in range(6)}
    one_record = finding("google_sheets_mock", "sh0 totals do not add up", "sh0")
    allowed, wide = repair_scope(state, [one_record], holders)
    assert wide == [] and allowed == {"sh0"}
    # Citing several rows is not a collection-wide complaint: of the 24 blocking findings a generous
    # pattern matched, 16 matched on "repeat" alone and several of those name one record.
    three = finding("google_sheets_mock", "sh0, sh1 and sh2 repeat the Drive figures", "sh0, sh1, sh2")
    allowed, wide = repair_scope(state, [three], holders)
    assert wide == [] and allowed == {"sh0", "sh1", "sh2"}
    # A repetition word beside a sameness word does read as collection-wide.
    both = finding("google_sheets_mock", "The cohorts repeat the same twelve dates", "sh0")
    assert repair_scope(state, [both], holders)[1] == ["sheets"]
    # The second repair round opens what the first round's narrow reading failed to settle.
    allowed, wide = repair_scope(state, [one_record], holders, widen=True)
    assert wide == ["sheets"] and allowed == {f"sh{i}" for i in range(6)}
    assert repair_scope(state, [], holders) == (set(), [])


def test_a_verdict_records_what_it_read_so_a_later_stage_can_refuse_it():
    """The review is the last step of seeding and two stages run after it: the last review call at
    03:47, the renaming at 03:50, the bulk layer at 03:53. The bulk layer adds 88-90% of every
    mailbox, so the verdict covered a tenth of what shipped and nothing on disk said so."""
    seeded = {"gmail_mock": {"emails": [{"id": "m1"}, {"id": "m2"}]}}
    before = review_inputs(WORLD, seeded, [])
    assert before["apps"]["gmail_mock"]["records"] == 2
    assert review_drift(before, review_inputs(WORLD, seeded, [])) == []
    renamed = {"gmail_mock": {"emails": [{"id": "m1", "to": "Dana"}, {"id": "m2"}]}}
    assert review_drift(before, review_inputs(WORLD, renamed, [])) == [
        "gmail_mock was rewritten after the verdict (the same 2 records, new content)"
    ]
    assert review_drift(before, review_inputs({"contracts": []}, seeded, [])) == [
        "the canonical world lost 2 of the 2 records the verdict read"
    ]
    # A reading from before any of this was recorded is unknown, not stale: refusing all sixty of
    # them would stop the fleet over a fact none of them can establish.
    bulked = {"gmail_mock": {"emails": [{"id": f"m{i}"} for i in range(20)]}}
    assert review_drift(None, review_inputs(WORLD, bulked, [])) == []
    assert review_drift({}, review_inputs(WORLD, bulked, [])) == []


def test_growth_after_a_verdict_is_coverage_and_only_a_rewrite_is_drift():
    """``review_drift`` counted the bulk layer's additions as staleness, so the flag fired on all 42
    of the 60 seeded worlds that have a bulk layer -- a flag always on is a flag nothing can act on.
    A verdict means exactly what it read: over the 60 folders the human layers the reviewers read
    hold 124,708 records against 331,577 on disk, 37.6%, and the worst world 8.8% of 8,669. That
    share is the number; a record the verdict read that is gone or rewritten is the refusal."""
    seeded = {"gmail_mock": {"emails": [{"id": "m1"}, {"id": "m2"}]}}
    before = review_inputs(WORLD, seeded, [])
    bulked = {"gmail_mock": {"emails": [{"id": "m1"}, {"id": "m2"}, *({"id": f"b{i}"} for i in range(18))]}}
    after = review_inputs(WORLD, bulked, [])
    assert review_drift(before, after) == [], "the bulk layer adds records, it does not unmake a reading"
    assert review_coverage(before, after) == 0.1, "the app records: the 2 it read of the 20 shipping"
    # The one case three marks cannot separate: a collection that grew while losing a record the
    # verdict read. No stage on disk does that -- ``dedupe-names`` renames people inside records it
    # keeps and ``add-bulk`` only appends, so every grown collection of the 60 folders kept its ids
    # -- and telling it apart would mean recording every id a verdict read, 24 KB an app a round.
    replaced = {"gmail_mock": {"emails": [{"id": f"b{i}"} for i in range(20)]}}
    assert review_drift(before, review_inputs(WORLD, replaced, [])) == []
    shrunk = {"gmail_mock": {"emails": [{"id": "m1"}]}}
    assert review_drift(before, review_inputs(WORLD, shrunk, [])) == [
        "gmail_mock lost 1 of the 2 records the verdict read"
    ]
    assert review_drift(before, review_inputs(WORLD, {}, [])) == [
        "gmail_mock is no longer in the world the verdict covered"
    ]
    assert review_coverage(None, after) is None and review_coverage({}, after) is None


def test_the_reviewer_packet_is_filled_errors_first():
    """The packet was filled from the checker's findings in list order and cut at a byte budget, so
    noise displaced real findings: measured over the 60 CHECKS.json as the checker then stood, 18
    worlds had a genuine error pushed out of the packet entirely (84 errors) and 8 worlds showed
    their reviewer none of their cross-app fact conflicts -- all 165 of which, over 12 worlds, are
    error severity. Today's checker collapses its warning classes 10,263 findings to 424 on
    eastman-chemical-company, so every packet fits and this is latent; the sort keeps it closed."""
    rows = [{"severity": "warning", "message": "w1"}, {"severity": "error", "message": "e1"}]
    assert [r["message"] for r in errors_first(rows)] == ["e1", "w1"]
    # Order inside each severity is the checker's own, so a packet stays readable.
    mixed = [{"severity": s, "message": f"{s}{i}"} for i in range(3) for s in ("warning", "error")]
    assert [r["message"] for r in errors_first(mixed)] == [
        "error0",
        "error1",
        "error2",
        "warning0",
        "warning1",
        "warning2",
    ]
    noise = [{"severity": "warning", "message": "x" * 400} for _ in range(20)]
    kept = _by_bytes(
        errors_first([*noise, {"severity": "error", "message": "the real one"}]), 900, "findings"
    )
    assert kept[0]["message"] == "the real one", "the error survives a budget the warnings would fill"


def panel(tmp_path, readings, *, review_rounds=2, votes=3):
    """``make_reviewer`` over a fixed list of readings; returns (review, the vote index of each call)."""
    skill = tmp_path / ".agents/skills/company-world-review"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: company-world-review\n---\nRead the world.\n")
    cast = []

    class Ballots:
        def call(self, job, prompt, response_type):
            cast.append(json.loads(prompt.rsplit("\n", 1)[1]).get("vote"))
            return readings.pop(0), {"model": "fake", "call_id": f"review-{len(cast)}"}

    review = make_reviewer(
        tmp_path,
        {"design": {"review_votes": votes}},
        Ballots(),
        review_rounds=review_rounds,
        reference_date="2026-09-08",
        operating_scope="one desk",
        company={"id": "acme", "name": "Acme"},
        tasks=[],
        world=WORLD,
        identities={},
        worker_apps={},
        apps=APPS,
        mechanical=lambda _materials: {"findings": []},
    )
    return review, cast


def test_the_panel_reads_the_rounds_it_can_act_on(tmp_path):
    """Ballot allocation was backwards: rounds 0 and 1 cast one ballot and the final round cast
    three, so the largest sample arrived with no round left to repair it -- the error count ended
    above where it started in 30 of 60 worlds, and 26% of three-ballot rounds had the panel
    disagree. Round 0 gets the panel, where a majority steers the first repair and a 2-of-3 accept
    saves the author calls a single dissenting reading would have ordered."""
    slip = finding("contractbook_mock", "Lease L003 rent differs from the signed copy", "L003")
    readings = [ballot("revise", [slip]) for _ in range(4)] + [ballot("accept", []) for _ in range(3)]
    review, cast = panel(tmp_path, readings)

    first, meta = review(0, STATES, [])
    assert meta["receipt"]["cast"] == 3 and sorted(cast) == [0, 1, 2]
    assert first.verdict == "revise" and meta["receipt"]["distinct_issues"] == 1
    assert meta["receipt"]["raw_findings"] == 3, "three readings of one slip"
    assert meta["receipt"]["new_issues"] == 1 and meta["receipt"]["persisting_issues"] == 0
    assert meta["inputs"]["apps"]["contractbook_mock"]["records"] == 2

    cast.clear()
    _middle, meta = review(1, STATES, [])
    assert meta["receipt"]["cast"] == 1 and cast == [0], "a revise with a round left spends one ballot"
    assert meta["receipt"]["persisting_issues"] == 1, "the loop remembers what it already reported"
    assert meta["receipt"]["new_issues"] == 0

    cast.clear()
    last, meta = review(2, STATES, [])
    assert meta["receipt"]["cast"] == 3 and sorted(cast) == [0, 1, 2]
    assert last.verdict == "accept" and meta["receipt"]["accepts"] == 3


def test_an_error_count_is_compared_only_with_a_round_read_by_the_same_panel(tmp_path):
    """The loop looked as if it diverged: 37 of the 38 worlds held at revise end with more errors
    than their best round (11-5-12, 7-3-5-6-12, 5-6-25, 2-2-7 ...). It does not. Over the 60
    REVIEW.json the last round is the only three-ballot round in 36 of those 38, and splitting the
    107 round-to-round transitions by ballot count settles which it is: with the ballot count
    unchanged the error count falls or holds in 46 of 56 transitions (29 better, 17 level, 10
    worse), and it rises in 36 of the 51 transitions from one reader to three. Errors per ballot
    fall monotonically across the round positions, 3.45 -> 2.55 -> 1.41."""
    slip = finding("contractbook_mock", "Lease L003 rent differs from the signed copy", "L003")
    other = finding("google_drive_mock", "The Drive copy of L003 carries a different rent", "L003")
    readings = [
        *(ballot("revise", [slip]) for _ in range(3)),  # round 0, three ballots, one issue
        ballot("revise", [slip, other]),  # round 1, one ballot, two issues
        *(ballot("revise", [slip]) for _ in range(3)),  # round 2, three ballots again
    ]
    review, _cast = panel(tmp_path, readings)

    _v, meta = review(0, STATES, [])
    assert meta["receipt"]["cast"] == 3 and meta["receipt"]["distinct_errors"] == 1
    assert meta["receipt"]["errors_per_ballot"] == 0.33
    assert meta["receipt"]["comparable_round"] is None
    assert meta["receipt"]["trend"] == "first round read by 3 ballot(s)"

    _v, meta = review(1, STATES, [])
    # Two errors against round 0's one, but one reader against three: not comparable, so not a trend.
    assert meta["receipt"]["cast"] == 1 and meta["receipt"]["distinct_errors"] == 2
    assert meta["receipt"]["comparable_round"] is None
    assert meta["receipt"]["trend"] == "first round read by 1 ballot(s)"

    _v, meta = review(2, STATES, [])
    assert meta["receipt"]["cast"] == 3 and meta["receipt"]["distinct_errors"] == 1
    assert meta["receipt"]["comparable_round"] == 0
    assert meta["receipt"]["trend"] == "level with 1 error(s) in round 0, the last round read by 3 ballot(s)"


def loop(tmp_path, readings, rewrite, *, review_rounds=2, votes=3):
    """``review_world`` over fixed readings and an author that applies ``rewrite`` to one app."""
    skill = tmp_path / ".agents/skills/company-world-review"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: company-world-review\n---\nRead the world.\n")
    states = deepcopy(STATES)
    calls = []

    class Ballots:
        def call(self, job, prompt, response_type):
            return readings.pop(0), {"model": "fake", "call_id": f"review-{len(readings)}"}

    def author(app, feedback, previous):
        calls.append(app["app_id"])
        return app["app_id"], rewrite(app["app_id"], len(calls), deepcopy(previous))

    verdict, history, _materials = review_world(
        tmp_path,
        {"design": {"review_votes": votes}},
        Ballots(),
        review_rounds=review_rounds,
        core=SimpleNamespace(reference_date="2026-09-08", operating_scope="one desk"),
        company={"id": "acme", "name": "Acme"},
        tasks=[],
        world=WORLD,
        identities={},
        worker_apps={},
        materials=[],
        states=states,
        apps=APPS,
        contract=[{"app_id": app_id} for app_id in sorted(STATES)],
        author=author,
        mechanical=lambda _materials: {"findings": []},
        instructions="seed",
    )
    return verdict, history, states, calls


def test_a_world_held_at_revise_ships_the_best_round_a_full_panel_read(tmp_path):
    """The last round's state was the one kept, so the pipeline kept the worst world it made: 37 of
    the 38 worlds held at revise end above their best round, 159 errors above it in total against
    253 in their last rounds. Keeping the fewest-error round outright is worse than keeping the last
    -- the last round is the only three-ballot round in 36 of those 38, so "fewest" picks a
    one-reader round and hands the next stage a state no panel ever read. The round kept is the best
    of the rounds the *same* panel read."""
    slip = finding("contractbook_mock", "Lease L003 rent differs from the signed copy", "L003")
    extra = finding("contractbook_mock", "Lease L004 now has no status at all", "L004")
    readings = [
        *(ballot("revise", [slip]) for _ in range(3)),  # round 0: three ballots, one error
        ballot("revise", [slip]),  # round 1: one ballot
        *(ballot("revise", [slip, extra]) for _ in range(3)),  # round 2: three ballots, two errors
    ]

    def rewrite(app_id, call, previous):
        if app_id == "contractbook_mock":
            previous["contracts"][0] = {**previous["contracts"][0], "rent": 900 + call}
        return previous

    verdict, history, states, _calls = loop(tmp_path, readings, rewrite)

    assert [h["round"] for h in history] == [0, 1, 2]
    assert history[2]["superseded_by_round"] == 0
    assert history[0]["kept"] == (
        "the world ships this round: 1 error(s) against 2 in round 2, both read by 3 ballot(s)"
    )
    # The state on disk is the one round 0's panel read, and the verdict is the one it cast on it.
    assert states["contractbook_mock"]["contracts"][0]["rent"] == 940
    assert [f.issue for f in verdict.findings] == [slip.issue]
    assert history[1].get("superseded_by_round") is None, "a one-reader round is never the comparison"


def test_a_round_records_which_of_the_apps_it_asked_came_back_changed(tmp_path):
    """A narrowing to zero is a failed author, not a repair: the author rewrote nothing the findings
    named, the reviewers duly reported the same defect again, and the driver read that as the loop
    diverging. ``repair_review`` refuses to pay a reading for a round of them -- measured over its 35
    recorded receipts, 25 of the 98 app repairs changed no record at all (26%, in 16 companies --
    {'contractbook_mock': 0}, {'google_sheets_mock': 0}, {'google_docs_mock': 0, 'salesforce_mock':
    0, 'slack_mock': 0}), 29 of the 52 reader passes were paid inside a receipt holding one, and 5 of
    the 25 repaired-and-rejected worlds are terminal on a round whose repair changed nothing in an
    app the next round's errors name. The seeding loop records the same fact and does not yet act on
    it: the evidence does not reach here. Over the 107 seeding rounds that were followed by a
    reading, not one had findings so unscoped that the narrowing had to put every rewrite back, and
    what the authors actually returned was never written down, so ``asked`` and ``unchanged`` are
    what the next measurement reads."""
    slip = finding("contractbook_mock", "Lease L003 rent differs from the signed copy", "L003")
    readings = [ballot("revise", [slip]) for _ in range(3)] + [ballot("accept", []) for _ in range(3)]

    verdict, history, _states, calls = loop(tmp_path, readings, lambda _a, _c, previous: previous)

    # Both holders of L003 are asked, once for the finding and once for the copy drift it exposes.
    assert calls == ["contractbook_mock", "google_drive_mock"] * 2
    assert verdict.verdict == "accept" and [h["round"] for h in history] == [0, 1]
    assert history[1]["asked"] == ["contractbook_mock", "google_drive_mock"]
    assert history[1]["unchanged"] == ["contractbook_mock", "google_drive_mock"]
    assert history[1]["repaired"] == [] and history[1]["materials"] == []


def test_a_sample_survives_the_collection_changing_so_a_finding_can_be_rechecked():
    """A verdict has to be checkable against the round that raised it.

    ``sample_collection`` used to seed its RNG on ``(seed, keep)`` and then draw indices out of
    ``range(len(rest))``, so the whole tail was re-drawn whenever the collection's length moved --
    and a collection's length moves every round while the world is still being authored and
    repaired. Measured on a 300-record collection at keep=80, records dropped from the 80 the
    reviewer had been shown: adding one record used to drop 7, adding five 11, and adding twenty or
    more 31 -- the entire tail. Ranking by record identity instead of position makes those 0, 1 and
    2. The sharpest case is removal: taking 50 records away used to cost the reviewer sight of 6
    records that were still in the collection, and now costs it none.
    """
    rows = [{"id": f"r{index}", "text": "x" * 40} for index in range(300)]
    shown = {row["id"] for row in sample_collection(rows, 80, "seed") if "_sampled" not in row}
    assert len(shown) == 80
    for added, worst in ((1, 0), (5, 1), (20, 2), (50, 8)):
        grown = rows + [{"id": f"new{index}", "text": "y" * 40} for index in range(added)]
        now = {row["id"] for row in sample_collection(grown, 80, "seed") if "_sampled" not in row}
        assert len(shown - now) <= worst, f"+{added} records dropped {len(shown - now)} of the sample"
    # Removing records may only cost the reviewer sight of the records that went away.
    gone = {row["id"] for row in rows[250:]}
    now = {row["id"] for row in sample_collection(rows[:250], 80, "seed") if "_sampled" not in row}
    assert not (shown - now) - gone


def test_a_record_a_finding_cites_is_never_sampled_out_of_the_next_round():
    """A round asked to confirm, repair or withdraw a finding needs the record it is about.

    The sample is chosen without regard to the findings, so a cited record was dropped at exactly
    the rate anything else was. ``pin`` carries the ids the mechanical findings and every earlier
    round reported, and a pinned record displaces an unpinned one rather than being added on top,
    so the packet stays the size the byte budget was computed against.
    """
    rows = [{"id": f"r{index}", "text": "x" * 40} for index in range(300)]
    plain = {row["id"] for row in sample_collection(rows, 80, "seed") if "_sampled" not in row}
    cited = sorted({f"r{index}" for index in range(200, 300)} - plain)[:6]
    assert cited, "need records the plain sample leaves out"
    pinned = [row for row in sample_collection(rows, 80, "seed", pin=cited) if "_sampled" not in row]
    assert set(cited) <= {row["id"] for row in pinned}
    assert len(pinned) == 80  # displaced, not added

    keyed = {f"k{index}": {"id": f"k{index}"} for index in range(300)}
    bounded = bound_states({"app_mock": {"rows": keyed}}, limit=20_000, pin=["k299"])["app_mock"]["rows"]
    assert "k299" in bounded
    assert "_sampled" in bounded


def test_a_second_round_is_shown_the_records_the_first_round_reported(tmp_path):
    """A reviewer asked to confirm, repair or withdraw a finding needs the record it is about.

    The sample was chosen without regard to the findings, so a cited record was dropped at exactly
    the rate anything else was -- and a collection long enough to be sampled is exactly the kind a
    finding gets raised about. Here the cited contract sits past the sample's reach in a 400-record
    collection: without the pin the second round's packet does not contain it, and the reviewer is
    asked about a record it cannot see.
    """
    big = {
        "contractbook_mock": {
            "contracts": [{"id": f"L{n:04d}", "rent": 900 + n} for n in range(400)],
        }
    }
    cited = "L0399"
    assert cited not in json.dumps(bound_states(big)), "the plain sample must not already show it"
    pinned = bound_states(big, pin=[cited])
    assert cited in json.dumps(pinned), "a cited record has to survive the sample"
    shown = [row for row in pinned["contractbook_mock"]["contracts"] if "_sampled" not in row]
    assert len(shown) == 80, "the pin displaces a record rather than growing the packet"
    assert any(row["id"] == cited for row in shown)
