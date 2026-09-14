import json

import pytest

from company_envs.models import ModelOutputInvalid
from company_envs.world import worker_apps as wa
from company_envs.world.state_seed import contract_worker_apps, sync_worker_apps

APPS = {
    "jira_mock": ["issues", "projects"],
    "gmail_mock": ["emails", "labels"],
    "google_docs_mock": ["documents"],
}
COLLECTIONS = {**APPS, "billing_mock": ["invoices"], "google_sheets_mock": ["sheets"]}


def make(tmp_path, apps=None):
    root = tmp_path
    APPS = apps if apps is not None else globals()["APPS"]
    (root / ".agents/skills/company-worker-apps").mkdir(parents=True)
    (root / ".agents/skills/company-worker-apps/SKILL.md").write_text(
        "---\nname: company-worker-apps\n---\nBind.\n"
    )
    (root / "config.toml").write_text("[models]\n")
    (root / "schemas").mkdir()
    for app, keys in APPS.items():
        rows = "".join(f"| `{k}` | array | {k} |\n" for k in keys)
        (root / "schemas" / f"{app}.md").write_text(
            "## State Schema\n| Key | Type | Description |\n|---|---|---|\n" + rows
        )
    folder = root / "companies" / "acme"
    (folder / "tasks" / "acme_o1").mkdir(parents=True)
    (folder / "company.json").write_text(
        json.dumps(
            {
                "id": "acme",
                "name": "Acme",
                "workers": [
                    {
                        "id": "boss",
                        "title": "Manager",
                        "information_access": ["Everything the unit reports"],
                    },
                    {"id": "dev", "title": "Engineer", "information_access": ["Build logs"]},
                ],
            }
        )
    )
    (folder / "apps.json").write_text(
        json.dumps(
            {
                "apps": [
                    {"app_id": a, "schema": f"schemas/{a}.md", "top_level_keys": k} for a, k in APPS.items()
                ]
            }
        )
    )
    (folder / "MANIFEST.json").write_text(json.dumps({"hashes": {}}))
    (folder / "tasks" / "acme_o1" / "workflow.json").write_text(
        json.dumps(
            {
                "id": "acme_o1",
                "title": "Fix the build",
                "brief": "The nightly build broke. Find the cause and fix it by Friday.",
                "feature_cell": {
                    "collections": ["gmail_mock.labels", "google_docs_mock.documents"],
                    "decision_type": "investigate-root-cause",
                },
                "contributions": [
                    {"worker_id": "boss", "work": "Decides"},
                    {"worker_id": "dev", "work": "Fixes"},
                ],
            }
        )
    )
    return root, folder


class Models:
    def __init__(self, answers):
        self.answers = list(answers)

    def call(self, job, prompt, response_type):
        return self.answers.pop(0), {"model": "fake"}


class Recording(Models):
    """Keep the prompts so a test can read the payload the skill is handed."""

    def __init__(self, answers):
        super().__init__(answers)
        self.prompts = []

    def call(self, job, prompt, response_type):
        self.prompts.append(prompt)
        return super().call(job, prompt, response_type)


def payload(models):
    """The JSON the instructions are sent with; json.dumps writes it on one line."""
    return json.loads(models.prompts[-1].rsplit("\n", 1)[-1])


def set_manager(folder, worker="boss", task="acme_o1"):
    path = folder / "tasks" / task / "workflow.json"
    workflow = json.loads(path.read_text())
    workflow["manager_id"] = worker
    path.write_text(json.dumps(workflow))


def binding(boss_apps, dev_apps, cells=("jira_mock.issues", "google_docs_mock.documents"), wid="acme_o1"):
    return wa.WorkerAppsResult(
        tasks=[
            wa.TaskBinding(
                workflow_id=wid,
                decisive_collections=list(cells),
                contributions=[
                    wa.WorkerBinding(worker_id="boss", apps=boss_apps),
                    wa.WorkerBinding(worker_id="dev", apps=dev_apps),
                ],
            )
        ]
    )


def test_assign_writes_contribution_apps_and_replaces_ui_cells(tmp_path):
    root, folder = make(tmp_path)
    result = wa.assign_worker_apps(root, folder, models=Models([binding([], ["jira_mock"])]))
    assert result["tasks"] == 1
    wf = json.loads((folder / "tasks/acme_o1/workflow.json").read_text())
    assert [c["apps"] for c in wf["contributions"]] == [[], ["jira_mock"]]
    assert wf["feature_cell"]["collections"] == ["google_docs_mock.documents", "jira_mock.issues"]
    assert wf["feature_cell_before"]["collections"] == ["gmail_mock.labels", "google_docs_mock.documents"]
    assert contract_worker_apps(folder) == {"boss": [], "dev": ["jira_mock"]}


def test_assign_rejects_everyone_holding_everything_then_accepts_a_fix(tmp_path):
    root, folder = make(tmp_path)
    everyone = binding(["jira_mock"], ["jira_mock"])
    assert (
        wa.assign_worker_apps(root, folder, models=Models([everyone, binding([], ["jira_mock"])]))["attempts"]
        == 2
    )
    root2, folder2 = make(tmp_path / "b")
    with pytest.raises(ModelOutputInvalid):
        wa.assign_worker_apps(root2, folder2, models=Models([everyone] * 3))


def test_only_binds_the_named_tasks_and_leaves_the_company_marker_alone(tmp_path):
    """The mined-task path: ASSIGN.json is the seed stage's input, and re-dating it re-seeds."""
    root, folder = make(tmp_path)
    wa.assign_worker_apps(root, folder, models=Models([binding([], ["jira_mock"])]))
    marker = folder / "tasks/_worker_apps/ASSIGN.json"
    stamped = marker.stat().st_mtime_ns
    fresh = json.loads((folder / "tasks/acme_o1/workflow.json").read_text())
    fresh["id"] = "acme_o2"
    fresh.pop("worker_apps")
    for contribution in fresh["contributions"]:
        contribution.pop("apps")
    (folder / "tasks/acme_o2").mkdir()
    (folder / "tasks/acme_o2/workflow.json").write_text(json.dumps(fresh))
    models = Models([binding(["jira_mock"], [], wid="acme_o2")])
    assert wa.assign_worker_apps(root, folder, models=models, only=["acme_o2"])["tasks"] == 1
    mined = json.loads((folder / "tasks/acme_o2/workflow.json").read_text())
    assert [c["apps"] for c in mined["contributions"]] == [["jira_mock"], []]
    assert marker.stat().st_mtime_ns == stamped
    # The world's binding now has to cover both tasks: that is what sync-worker-apps re-derives.
    assert contract_worker_apps(folder) == {"boss": ["jira_mock"], "dev": ["jira_mock"]}
    assert wa.assign_worker_apps(root, folder, models=Models([]), only=["acme_o2"])["skipped"]
    with pytest.raises(ValueError, match="no such tasks"):
        wa.assign_worker_apps(root, folder, models=Models([]), only=["acme_o9"])


def test_sync_worker_apps_rewrites_a_seeded_world_from_the_contract(tmp_path):
    root, folder = make(tmp_path)
    wa.assign_worker_apps(root, folder, models=Models([binding([], ["jira_mock"])]))
    (folder / "world").mkdir()
    (folder / "world/worker_apps.json").write_text(
        json.dumps({"boss": ["jira_mock", "gmail_mock"], "dev": ["jira_mock", "gmail_mock"]})
    )
    synced = sync_worker_apps(folder)
    assert synced == {
        "boss": ["gmail_mock", "google_docs_mock"],
        "dev": ["gmail_mock", "google_docs_mock", "jira_mock"],
    }


def test_manager_holding_every_decisive_app_is_rejected(tmp_path):
    root, folder = make(tmp_path)
    set_manager(folder)
    solo = binding(["jira_mock"], [])  # boss holds Jira, the decisive app; dev holds nothing
    with pytest.raises(ModelOutputInvalid, match="could do the task alone"):
        wa.assign_worker_apps(root, folder, models=Models([solo] * 3))
    root2, folder2 = make(tmp_path / "b")
    set_manager(folder2)
    assert wa.assign_worker_apps(root2, folder2, models=Models([binding([], ["jira_mock"])]))["tasks"] == 1


def test_the_spreadsheet_alone_cannot_gate_a_company_that_owns_a_system_of_record(tmp_path):
    """52 of 178 bound tasks keep the manager out with google_sheets_mock and nothing else, and 32
    of those belong to companies whose real system of record the binding never touched. Everyone in
    an office has a spreadsheet, so that gate proves nothing about how the work divides."""
    apps = {k: COLLECTIONS[k] for k in ("jira_mock", "google_sheets_mock", "gmail_mock")}
    cells = ("google_sheets_mock.sheets", "jira_mock.issues")
    root, folder = make(tmp_path, apps)
    set_manager(folder)
    sheets_gate = binding(["jira_mock"], ["google_sheets_mock", "jira_mock"], cells=cells)
    with pytest.raises(ModelOutputInvalid, match="not a system of record"):
        wa.assign_worker_apps(root, folder, models=Models([sheets_gate] * 3))
    root2, folder2 = make(tmp_path / "b", apps)
    set_manager(folder2)
    real_gate = binding(["google_sheets_mock"], ["google_sheets_mock", "jira_mock"], cells=cells)
    assert wa.assign_worker_apps(root2, folder2, models=Models([real_gate]))["attempts"] == 1


def test_a_company_whose_only_domain_app_is_the_spreadsheet_still_binds(tmp_path):
    """Ten seeded companies own no domain app but Sheets. Refusing them would block on a defect this
    stage cannot repair: it can move a decisive collection, never buy the company a new system."""
    apps = {k: COLLECTIONS[k] for k in ("google_sheets_mock", "gmail_mock", "google_docs_mock")}
    root, folder = make(tmp_path, apps)
    set_manager(folder)
    cells = ("google_sheets_mock.sheets", "google_docs_mock.documents")
    assert (
        wa.assign_worker_apps(
            root, folder, models=Models([binding([], ["google_sheets_mock"], cells=cells)])
        )["attempts"]
        == 1
    )


def test_a_domain_collection_only_the_manager_holds_is_refused_while_the_model_can_still_fix_it(tmp_path):
    """check_barrier fails a task whose domain collection nobody but the manager can open, and the
    driver's one repair for that condition -- sync-worker-apps -- only rewrites worker_apps.json
    from this contract, so it can never move the collection. The binding has to refuse it here."""
    apps = {k: COLLECTIONS[k] for k in ("jira_mock", "billing_mock", "gmail_mock")}
    cells = ("billing_mock.invoices", "jira_mock.issues")
    root, folder = make(tmp_path, apps)
    set_manager(folder)
    hoarded = binding(["billing_mock"], ["jira_mock"], cells=cells)
    with pytest.raises(ModelOutputInvalid, match="held by the manager"):
        wa.assign_worker_apps(root, folder, models=Models([hoarded] * 3))
    root2, folder2 = make(tmp_path / "b", apps)
    set_manager(folder2)
    shared = binding(["billing_mock"], ["billing_mock", "jira_mock"], cells=cells)
    assert wa.assign_worker_apps(root2, folder2, models=Models([shared]))["attempts"] == 1


def test_the_prompt_carries_the_information_each_worker_can_reach(tmp_path):
    """All 476 workers in the portfolio carry title, responsibility and information_access, and none
    carries name or team_id: the payload asked for two fields that were null 476 times and dropped
    the one that says which systems a person already reaches."""
    root, folder = make(tmp_path)
    models = Recording([binding([], ["jira_mock"])])
    wa.assign_worker_apps(root, folder, models=models)
    assert payload(models)["workers"] == [
        {"id": "boss", "title": "Manager", "information_access": ["Everything the unit reports"]},
        {"id": "dev", "title": "Engineer", "information_access": ["Build logs"]},
    ]


def test_ui_state_is_dropped_before_the_model_is_asked_which_cells_to_keep(tmp_path):
    """92 of the 178 pairs handed to this stage named at least one key that is no record collection
    at all, 33 of them both, because the feature matrix draws a cell from every top-level key.
    Offering those back as collections to keep invites the model to copy one."""
    root, folder = make(tmp_path)
    path = folder / "tasks/acme_o1/workflow.json"
    workflow = json.loads(path.read_text())
    workflow["feature_cell"]["collections"] = ["google_docs_mock.zoom", "gmail_mock.labels"]
    path.write_text(json.dumps(workflow))
    models = Recording([binding([], ["jira_mock"])])
    wa.assign_worker_apps(root, folder, models=models)
    assert payload(models)["tasks"][0]["current_decisive_collections"] == ["gmail_mock.labels"]


def test_contract_apps_fill_worker_apps_the_model_left_out():
    from company_envs.world.state_seed import STANDARD_APPS, WorldCore, validate_core

    core = WorldCore.model_validate(
        {
            "rationale": "r",
            "reference_date": "2026-09-08",
            "operating_scope": "s",
            "assumptions": [],
            "entities_json": '{"staff": [{"id": "s1", "name": "Ana Cruz"}]}',
            "identities": [
                {
                    "worker_id": w,
                    "app_id": "jira_mock",
                    "user_json": json.dumps({"id": w, "name": w.title(), "email": f"{w}@acme.test"}),
                }
                for w in ("boss", "dev")
            ],
            "worker_apps": [{"worker_id": "boss", "app_ids": ["jira_mock"]}],  # forgot dev
            "materials": [],
        }
    )
    apps = {"jira_mock": {"identity_key": "currentUser"}, "gmail_mock": {"identity_key": None}}
    _world, _identities, _materials, worker_apps = validate_core(
        core, apps, ["boss", "dev"], contract_apps={"boss": [], "dev": ["jira_mock"]}
    )
    assert worker_apps == {
        "boss": sorted(STANDARD_APPS & apps.keys()),
        "dev": sorted({"jira_mock"} | (STANDARD_APPS & apps.keys())),
    }


def test_a_decisive_collection_nobody_holds_is_refused(tmp_path):
    """The gate rejected a manager who could finish alone but not a cell held by no one.

    Nobody holding it looks like the access barrier -- the manager is kept out of it -- while the
    specialist cannot reach the records either, so the delegation the task is built on cannot
    happen and nothing downstream notices: the world seeds the app, the grader reads the
    collection, and the episode has no one who can write it. None of the 178 bindings on disk do
    this; the check is here so the latent case stays latent.
    """
    root, folder = make(tmp_path)
    set_manager(folder)
    workflow = json.loads((folder / "tasks/acme_o1/workflow.json").read_text())
    apps = wa.company_apps(root, folder)
    nobody = binding([], [], cells=("jira_mock.issues",))
    assert wa.check_binding(workflow, nobody.tasks[0], apps) == [
        (
            "jira_mock.issues is held by nobody; give jira_mock to the worker whose contribution "
            "produces that finding"
        )
    ]
    held = binding([], ["jira_mock"], cells=("jira_mock.issues",))
    assert wa.check_binding(workflow, held.tasks[0], apps) == []
    # An app the company does not have is already named by its own rule; it is not also unheld.
    unknown = binding([], [], cells=("billing_mock.invoices",))
    problems = wa.check_binding(workflow, unknown.tasks[0], apps)
    assert problems and not any("held by nobody" in p for p in problems)
    assert wa.assign_worker_apps(root, folder, models=Models([nobody, held]))["attempts"] == 2


def test_company_grants_are_settled_before_the_world_is_written(tmp_path):
    """The grant list is one rule in one place, and the binding stage is where it is decided.

    149 (worker, app) identities in 40 of the 60 seeded worlds name an app the worker has no login
    for, because the world was authored for every worker and narrowed afterwards. The narrowing in
    sync_worker_apps only touches workers the world already listed, so a worker no task uses -- who
    still has a mailbox -- can be missing from it entirely; this map covers the whole roster.
    """
    root, folder = make(tmp_path)
    path = folder / "company.json"
    company = json.loads(path.read_text())
    company["workers"].append({"id": "clerk", "title": "Clerk", "information_access": ["Invoices"]})
    path.write_text(json.dumps(company))
    assert wa.company_grants(folder) is None  # no task names an app: the binding was never written
    result = wa.assign_worker_apps(root, folder, models=Models([binding([], ["jira_mock"])]))
    grants = {
        "boss": ["gmail_mock", "google_docs_mock"],
        "dev": ["gmail_mock", "google_docs_mock", "jira_mock"],
        "clerk": ["gmail_mock", "google_docs_mock"],
    }
    assert wa.company_grants(folder) == grants == result["grants"]
    # The standard bundle is only what this company declares: no login for an app it does not have.
    assert "slack_mock" not in grants["boss"]
    # Nothing is left for the check stage to narrow: syncing a world seeded against this map is a
    # no-op, which is what makes the grant final before the world is written rather than after.
    (folder / "world").mkdir()
    (folder / "world/worker_apps.json").write_text(json.dumps({w: sorted(APPS) for w in grants}))
    assert sync_worker_apps(folder) == grants


def test_the_assign_marker_cannot_record_a_pass_for_a_bind_that_did_nothing(tmp_path):
    """ASSIGN.json was scoped as the clearest remaining instance of "an empty run writes the receipt a
    real verdict writes", and measuring says it is not one -- an inverted conclusion, which is the
    commonest kind of finding this sweep produced (about one in four).

    The marker cannot hold `tasks: []`: a company with no tasks raises, and a binding that does not
    cover every task raises ModelOutputInvalid after three attempts. 0 of the 98 ASSIGN.json on disk
    hold `tasks: []` or `attempts: 0` (2026-09-10). So nothing here is a fix.

    What this test is for is that the guarantee was two raises thirty lines apart, enforced by
    nothing. It is now stated as the marker's only reachable outcome and checked here, so removing
    either raise fails a test instead of quietly letting an empty bind wear a pass.
    """
    root, folder = make(tmp_path)
    for path in folder.glob("tasks/*/workflow.json"):
        path.unlink()
    # An empty answer list raises IndexError if anything reaches the model, so the raise below is
    # reached before any call: there is nothing to bind and nothing is filed.
    with pytest.raises(ValueError, match="has no tasks"):
        wa.assign_worker_apps(root, folder, models=Models([]))
    assert not (folder / "tasks" / "_worker_apps" / "ASSIGN.json").exists(), "no marker, so no verdict"


def test_a_bind_already_done_is_a_pass_and_says_so_rather_than_tasks_zero(tmp_path):
    """`{"tasks": 0, "skipped": "already assigned"}` is the shape a bind that achieved nothing would
    also have had, and a reader needed to know this stage to tell them apart. It is a pass: the
    binding the marker names is on disk."""
    root, folder = make(tmp_path)
    wa.assign_worker_apps(root, folder, models=Models([binding([], ["jira_mock"])]))
    again = wa.assign_worker_apps(root, folder, models=Models([]))
    assert again["outcome"] == "passed" and again["ok"] is True
    assert again["tasks"] == 0 and again["skipped"] == "already assigned"
    marker = json.loads((folder / "tasks" / "_worker_apps" / "ASSIGN.json").read_text())
    assert marker["outcome"] == "passed" and marker["tasks"] == ["acme_o1"]
