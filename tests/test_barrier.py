"""Use local company fixtures to check records and the agreement gate."""

import pytest

from company_envs.storage import read, write
from company_envs.world.agreement import agreement
from company_envs.world.barrier import check_barrier


@pytest.fixture
def company(tmp_path):
    folder = tmp_path / "company"
    write(
        folder / "tasks/order/workflow.json",
        {
            "id": "order",
            "manager_id": "boss",
            "brief": "Please resolve the order for Harbor Supply by September 12, 2026.",
            "feature_cell": {"collections": ["gmail_mock.emails", "jira_mock.issues"]},
            "contributions": [
                {"worker_id": "boss", "apps": []},
                {"worker_id": "sales", "apps": []},
                {"worker_id": "ops", "apps": ["jira_mock"]},
            ],
        },
    )
    write(
        folder / "world/identities.json",
        {worker: {"gmail_mock": {"email": f"{worker}@harbor.test"}} for worker in ("boss", "sales", "ops")},
    )
    write(
        folder / "world/gmail_mock.state.json",
        {
            "emails": [
                {
                    "id": "shared",
                    "from": {"name": "Harbor Supply", "email": "client@harbor.test"},
                    "to": [{"email": "boss@harbor.test"}, {"email": "sales@harbor.test"}],
                    "subject": "Order confirmation",
                    "body": "Please confirm the quantity.",
                },
                {
                    "id": "private",
                    "from": {"email": "client@harbor.test"},
                    "to": [{"email": "sales@harbor.test"}],
                    "subject": "Harbor Supply order",
                    "body": "We need the revised quantities.",
                },
                {
                    "id": "background",
                    "to": [{"email": "ops@harbor.test"}],
                    "subject": "Lunch with Birch Works",
                    "body": "September 12 works for me.",
                },
            ],
            "drafts": [],
        },
    )
    write(folder / "world/world.json", {"reference_date": "2026-09-08"})
    write(
        folder / "world/worker_apps.json",
        {
            "boss": ["gmail_mock"],
            "sales": ["gmail_mock"],
            "ops": ["gmail_mock", "jira_mock"],
        },
    )
    write(folder / "apps.json", {"apps": [{"app_id": app} for app in ("gmail_mock", "jira_mock")]})
    for worker in ("boss", "sales", "ops"):
        path = folder / "world/materials" / worker / "notes.txt"
        path.parent.mkdir(parents=True)
        path.write_text(f"Notes for {worker}")
    return folder


def finding(report, kind):
    return next(f["detail"] for f in report["findings"] if f["kind"] == kind)


def change_workflow(folder, change):
    path = folder / "tasks/order/workflow.json"
    workflow = read(path)
    change(workflow)
    write(path, workflow)


def test_pass_and_report_evidence(company):
    report = check_barrier(company)
    assert report["ok"] is True
    assert read(company / "world/BARRIER.json") == report
    assert {f["task"] for f in report["findings"]} == {"order"}
    mail = finding(report, "mailbox")
    assert mail["names_from_brief"] == ["Harbor Supply"]
    assert mail["relevant_emails"] == ["gmail_mock.emails#shared", "gmail_mock.emails#private"]
    assert mail["manager_visible_emails"] == ["gmail_mock.emails#shared"]
    assert mail["specialist_only_emails"]["sales"] == ["gmail_mock.emails#private"]
    apps = finding(report, "exclusive_apps")
    assert apps["workers_by_collection"] == {"jira_mock.issues": ["ops"]}
    assert apps["gating_collections"] == ["jira_mock.issues"]
    assert apps["spreadsheet_is_the_only_gate"] is False and apps["systems_of_record_unused"] == []
    assert agreement(company)["conditions"]["barrier"]["ok"] is True


@pytest.mark.parametrize("recipient_field", ["to", "cc"])
def test_all_relevant_mail_visible_fails_despite_unrelated_private_mail(company, recipient_field):
    path = company / "world/gmail_mock.state.json"
    state = read(path)
    state["emails"][1].setdefault(recipient_field, []).append({"email": "BOSS@HARBOR.TEST"})
    write(path, state)
    report = check_barrier(company)
    assert report["ok"] is False
    mail = finding(report, "mailbox")
    assert mail["manager_could_finish_alone"] is True
    assert mail["specialist_only_emails"]["sales"] == []
    assert mail["specialist_only_emails"]["ops"] == ["gmail_mock.emails#background"]
    assert agreement(company)["conditions"]["barrier"]["ok"] is False


def test_cc_strings_and_sender_do_not_make_manager_a_recipient(company):
    path = company / "world/gmail_mock.state.json"
    state = read(path)
    state["emails"][1].update(
        {
            "from": "Boss <boss@harbor.test>",
            "to": [],
            "cc": "Sales <SALES@harbor.test>",
            "subject": "harbor supply order",
        }
    )
    write(path, state)
    report = check_barrier(company)
    assert report["ok"] is True
    assert finding(report, "mailbox")["specialist_only_emails"]["sales"] == ["gmail_mock.emails#private"]


@pytest.mark.parametrize(
    "brief",
    [
        "Resolve Harbor Supply orders.",
        "Harbor Supply needs a revised order.",
        "Please check Harbor orders.",
        "Harbor needs a revised order.",
    ],
)
def test_names_at_sentence_start_and_after_requests(company, brief):
    change_workflow(company, lambda w: w.update(brief=brief))
    assert check_barrier(company)["ok"] is True


def test_name_substrings_do_not_count_as_counterparties(company):
    change_workflow(company, lambda w: w.update(brief="Please check Birch orders."))
    path = company / "world/gmail_mock.state.json"
    state = read(path)
    state["emails"][2]["subject"] = "Lunch with Birchwood Works"
    write(path, state)
    report = check_barrier(company)
    assert report["ok"] is False
    assert finding(report, "mailbox")["relevant_emails"] == []


@pytest.mark.parametrize("collection", ["emails", "drafts"])
def test_decisive_drafts_read_both_native_locations(company, collection):
    change_workflow(
        company, lambda w: w["feature_cell"].update(collections=["gmail_mock.drafts", "jira_mock.issues"])
    )
    path = company / "world/gmail_mock.state.json"
    state = read(path)
    private = state["emails"].pop(1)
    private["folder"] = "drafts"
    state[collection].append(private)
    write(path, state)
    report = check_barrier(company)
    assert report["ok"] is True
    assert finding(report, "mailbox")["specialist_only_emails"]["sales"] == [
        f"gmail_mock.{collection}#private"
    ]


def test_duplicate_desktop_bytes_report_all_paths_and_fail_agreement(company):
    manager = company / "world/materials/boss/copied/renamed.bin"
    manager.parent.mkdir()
    original = company / "world/materials/sales/notes.txt"
    manager.write_bytes(original.read_bytes())
    second = company / "world/materials/boss/another.txt"
    second.write_bytes(original.read_bytes())
    report = check_barrier(company)
    assert report["ok"] is False
    assert finding(report, "desktop") == {
        # ``outcome`` is the shared vocabulary from company_envs.receipt: a reader that holds one
        # finding out of a dozen stages' receipts can tell a verdict from a fault from "there was
        # nothing to measure" without knowing which stage wrote it.
        "outcome": "refused",
        "ok": False,
        "reason": "sales and the manager hold the same bytes",
        "specialist": "sales",
        "manager_paths": ["world/materials/boss/another.txt", "world/materials/boss/copied/renamed.bin"],
        "specialist_paths": ["world/materials/sales/notes.txt"],
    }
    assert agreement(company)["conditions"]["barrier"]["ok"] is False


def test_manager_only_domain_collection_fails_even_with_another_specialist_app(company):
    def change(workflow):
        workflow["contributions"][0]["apps"] = ["billing_mock"]
        workflow["feature_cell"]["collections"] = ["billing_mock.invoices", "jira_mock.issues"]

    change_workflow(company, change)
    report = check_barrier(company)
    apps = finding(report, "exclusive_apps")
    assert report["ok"] is False
    assert apps["manager_could_finish_alone"] is False
    assert apps["manager_only_collections"] == ["billing_mock.invoices"]
    assert apps["workers_by_collection"] == {"billing_mock.invoices": ["boss"], "jira_mock.issues": ["ops"]}


def test_manager_holding_all_apps_still_fails_when_specialists_share_them(company):
    change_workflow(company, lambda w: w["contributions"][0].update(apps=["jira_mock"]))
    report = check_barrier(company)
    assert report["ok"] is False
    assert finding(report, "exclusive_apps")["manager_could_finish_alone"] is True
    assert agreement(company)["conditions"]["barrier"]["evidence"]["manager_could_finish_alone"] == ["order"]


def test_a_spreadsheet_gate_is_reported_as_weak_without_failing_the_company(company):
    """52 of 178 bound tasks keep the manager out with google_sheets_mock and nothing else, 32 of
    them while a real system of record sits unused. Failing them here would spin the driver: its
    only repair for this condition, sync-worker-apps, rewrites worker_apps.json from the task
    contract and cannot move a decisive collection. So it is reported, not failed."""
    write(
        company / "apps.json",
        {"apps": [{"app_id": a} for a in ("gmail_mock", "jira_mock", "google_sheets_mock")]},
    )

    def change(workflow):
        workflow["feature_cell"]["collections"] = ["gmail_mock.emails", "google_sheets_mock.sheets"]
        workflow["contributions"][2]["apps"] = ["google_sheets_mock"]

    change_workflow(company, change)
    report = check_barrier(company)
    apps = finding(report, "exclusive_apps")
    assert report["ok"] is True and apps["ok"] is True
    assert apps["manager_could_finish_alone"] is False
    assert apps["gating_collections"] == ["google_sheets_mock.sheets"]
    assert apps["spreadsheet_is_the_only_gate"] is True
    assert apps["systems_of_record_unused"] == ["jira_mock"]


def test_a_desktop_hashed_once_per_company_still_reports_every_task(company):
    """Hashing each desktop once instead of once per task saved rereading 2026 seeded files, and must
    not cost the second task its finding: both tasks share the roster and the copied bytes."""
    copy = company / "world/materials/boss/copied.txt"
    copy.write_bytes((company / "world/materials/sales/notes.txt").read_bytes())
    write(company / "tasks/other/workflow.json", read(company / "tasks/order/workflow.json"))
    report = check_barrier(company)
    desktops = [f for f in report["findings"] if f["kind"] == "desktop"]
    assert report["ok"] is False
    assert [f["task"] for f in desktops] == ["order", "other"]
    assert {f["detail"]["specialist"] for f in desktops} == {"sales"}


@pytest.mark.parametrize("problem", ["invalid_state", "missing_email", "no_match"])
def test_mailbox_evidence_that_is_wrong_is_a_failure(company, problem):
    """A file that is there and will not parse is a half-written state; a roster member with no
    address and mail that matches no name in the brief are answers about the world. All three are
    verdicts the world earned, and they stay failures."""
    state_path = company / "world/gmail_mock.state.json"
    identities_path = company / "world/identities.json"
    if problem == "invalid_state":
        state_path.write_text("{")
    elif problem == "missing_email":
        identities = read(identities_path)
        identities["sales"]["gmail_mock"] = {}
        write(identities_path, identities)
    else:
        write(state_path, {"emails": [], "drafts": []})
    report = check_barrier(company)
    assert report["ok"] is False
    assert finding(report, "mailbox")["ok"] is False
    assert report["unmeasured"] == [] and "unmeasured" not in finding(report, "mailbox")


@pytest.mark.parametrize(
    ("problem", "missing"),
    [
        ("missing_state", ["world/gmail_mock.state.json"]),
        ("missing_identities", ["world/identities.json"]),
        ("no_world", ["world/identities.json", "world/gmail_mock.state.json"]),
    ],
)
def test_a_mailbox_the_seed_has_not_written_is_unmeasured_and_not_a_failure(company, problem, missing):
    """The receipt a real mailbox defect writes used to be the receipt an absent world wrote: `{ok:
    false, reason: "[Errno 2] No such file or directory: .../identities.json"}`. Measured over the 99
    companies on 2026-09-10, of 35 mailbox findings 25 passed and all 10 failures were this -- eight
    companies with no world yet, and not one true mailbox defect in the cohort. It is still not a pass:
    a world that does not exist has earned nothing, so the report goes on saying ok false."""
    if problem in ("missing_state", "no_world"):
        (company / "world/gmail_mock.state.json").unlink()
    if problem in ("missing_identities", "no_world"):
        (company / "world/identities.json").unlink()
    report = check_barrier(company)
    detail = finding(report, "mailbox")
    assert detail["ok"] is None and detail["unmeasured"] is True
    assert detail["missing"] == missing
    assert "Errno" not in detail["reason"] and "the seed has not written" in detail["reason"]
    assert report["unmeasured"] == sorted(missing)
    assert report["ok"] is False, "unmeasured is not a pass"
    # The findings that read no world at all are unaffected and still decide what they always did.
    assert finding(report, "exclusive_apps")["ok"] is True


@pytest.mark.parametrize("problem", ["missing", "empty", "missing_worker", "extra_app", "no_contract"])
def test_agreement_requires_complete_synced_contract(company, problem):
    path = company / "world/worker_apps.json"
    if problem == "missing":
        path.unlink()
    elif problem == "empty":
        write(path, {})
    elif problem == "no_contract":
        change_workflow(company, lambda w: w["contributions"][1].pop("apps"))
    else:
        apps = read(path)
        if problem == "missing_worker":
            del apps["sales"]
        else:
            apps["boss"].append("jira_mock")
        write(path, apps)
    assert agreement(company)["conditions"]["barrier"]["ok"] is False


def test_no_tasks_and_incomplete_task_fail(tmp_path, company):
    assert check_barrier(tmp_path / "empty")["ok"] is False
    change_workflow(company, lambda w: w.pop("manager_id"))
    assert finding(check_barrier(company), "input")["ok"] is False


def test_mail_and_desktop_checks_stay_with_the_task_roster(company):
    workflow = read(company / "tasks/order/workflow.json")
    workflow["brief"] = "Please resolve the order for Birch Works."
    write(company / "tasks/other/workflow.json", workflow)
    write(company / "tasks/_internal/workflow.json", {})
    path = company / "world/materials/outsider/copy.txt"
    path.parent.mkdir()
    path.write_bytes((company / "world/materials/boss/notes.txt").read_bytes())
    report = check_barrier(company)
    assert report["ok"] is True
    assert {f["task"] for f in report["findings"]} == {"order", "other"}
    other = next(f["detail"] for f in report["findings"] if f["task"] == "other" and f["kind"] == "mailbox")
    assert other["relevant_emails"] == ["gmail_mock.emails#background"]


def test_the_report_can_be_read_without_writing_into_the_company(tmp_path, company):
    """Reading a company must not write into it: inspecting one by writing REVIEW-SHEET.md advanced
    its apparent position, because that file is a driver marker. Before a seed there is no world/ at
    all, and writing BARRIER.json there would create the directory to hold a half-unmeasured
    artifact -- so the pre-seed entry point is the one that computes and returns."""
    from company_envs.world.barrier import barrier_report, missing_inputs

    assert barrier_report(company) == check_barrier(company)
    unseeded = tmp_path / "unseeded"
    write(unseeded / "tasks/order/workflow.json", read(company / "tasks/order/workflow.json"))
    report = barrier_report(unseeded)
    assert not (unseeded / "world").exists(), "computing a report creates nothing"
    assert report["unmeasured"] == sorted([*missing_inputs(unseeded), "world/materials", "world/SEED.json"])
    # The two findings that read no world are decided all the same, which is what makes them movable
    # before a seed: 0 of the 98 companies with an ASSIGN.json fail either (2026-09-10), so nothing
    # calls it that way yet.
    assert finding(report, "exclusive_apps")["manager_could_finish_alone"] is False
    assert finding(report, "mailbox")["ok"] is None


def test_a_world_nobody_has_looked_at_does_not_report_a_barrier_that_holds(tmp_path, company):
    """A desktop collision is reported when it is found, so finding nothing meant two things: the
    desktops differ, or there are none. Measured 2026-09-10, **30 of the 39 companies with no world
    reported barrier ok true** -- each on exclusive_apps alone, because the desktop comparison
    compared nothing and no decisive collection of theirs lives in gmail. The agreement asked for
    worker_apps.json too and so was not fooled, but the artifact claimed a division of work that
    nobody had looked at, and a pre-seed caller reading that ok would be."""
    from company_envs.world.barrier import barrier_report

    unseeded = tmp_path / "unseeded"
    workflow = read(company / "tasks/order/workflow.json")
    # No decisive collection in gmail, so the mailbox half is not asked for either: this is the shape
    # all 30 companies had, and the only finding left was one that passes.
    workflow["feature_cell"] = {"collections": ["jira_mock.issues"]}
    write(unseeded / "tasks/order/workflow.json", workflow)
    report = barrier_report(unseeded)
    assert [f["kind"] for f in report["findings"]] == ["exclusive_apps", "desktop"]
    assert finding(report, "exclusive_apps")["ok"] is True
    assert finding(report, "desktop") == {
        "outcome": "unmeasured",
        "ok": None,
        "reason": "the desktop barrier was not measured: the seed has not written "
        "world/materials, world/SEED.json",
        "missing": ["world/materials", "world/SEED.json"],
        "unmeasured": True,
    }
    # The report's own word for the run, and why it is not the same question as ``ok``: the only
    # finding that could be taken passed, so ``all()`` over the details would have said yes, while
    # the run as a whole measured nothing a desktop barrier could be judged on.
    assert report["outcome"] == "unmeasured"
    assert report["ok"] is False, "a pass has to be earned by a world that exists"
    # A seeded world whose workers carry no desktop files has been compared and had nothing to
    # compare. That is a verdict, and it must not turn into "unmeasured" -- no stage writes materials
    # after the seed, so blocking there would be a defect the pipeline cannot repair.
    write(unseeded / "world/SEED.json", {"status": "seeded_reviewed"})
    report = barrier_report(unseeded)
    assert [f["kind"] for f in report["findings"]] == ["exclusive_apps"]
    assert report["ok"] is True and report["unmeasured"] == []
