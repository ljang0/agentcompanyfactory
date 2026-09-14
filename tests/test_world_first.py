"""World-first orchestration with model doubles; no live inference or app runtime."""

import json
import shutil
from types import SimpleNamespace

import pytest
from test_hub_world import SEEDED, USERS
from test_state_seed import FakeModels, app_state, core
from test_state_seed import exported as exported  # noqa: PLC0414 -- register the shared pytest fixture

from company_envs import workflows
from company_envs.storage import digest, read, write
from company_envs.world import state_seed, task_author


class GroupModels(FakeModels):
    def call(self, job, prompt, response_type, **kwargs):
        if response_type is state_seed.AppStateResult:
            payload = json.loads(prompt.rsplit("\n", 1)[1])
            self.app_value = app_state({k: SEEDED[k] for k in payload["named_collections"]})
        return super().call(job, prompt, response_type, **kwargs)


@pytest.mark.parametrize("large", [False, True])
def test_taskless_seed_groups_merge_and_preserve_identities(exported, large):
    root, folder = exported
    shutil.rmtree(folder / "tasks")
    company = read(folder / "company.json")
    company["outlines"] = [{"id": "later", "objective": "Resolve normal support work"}]
    write(folder / "company.json", company)
    apps = read(folder / "apps.json")
    apps["apps"][0]["role"] = "standard"
    write(folder / "apps.json", apps)
    with (root / "config.toml").open("a") as stream:
        stream.write("\n[design]\nseed_collections_per_call = 2\n")
    world = {
        "entities": [
            {"id": "7", "apps": ["demo_mock"], "note": "x" * (81000 if large else 1)},
            {"id": "unrelated", "apps": ["other_mock"]},
        ],
        "history": [],
    }
    models = GroupModels(core(entities_json=json.dumps(world)), app_state())
    report = state_seed.seed_world(root, folder, models=models, review_rounds=0)
    payloads = [json.loads(prompt.rsplit("\n", 1)[1]) for _, prompt, _ in models.calls]
    assert payloads[0]["tasks"] == []
    assert payloads[0]["upcoming_work_themes"] == company["outlines"]
    assert payloads[0]["apps"][0]["role"] == "standard"
    assert "outlines" not in payloads[0]["company"]
    assert len(payloads[1:]) == 2
    assert {k for p in payloads[1:] for k in p["named_collections"]} == set(SEEDED)
    assert all(p["canonical_world"]["entities"] == world["entities"][:1] for p in payloads[1:])
    assert all(not p["tasks_public"] for p in payloads[1:])
    assert read(folder / "world" / "demo_mock.state.json") == SEEDED
    assert read(folder / "world" / "identities.json")["w2"]["demo_mock"] == USERS[1]
    assert report["calls"] == len(payloads)


def test_projection_filters_nested_entities_and_explicit_references():
    world = {
        "reference_date": "2026-09-01",
        "entities": [
            {"id": "a", "apps": ["one"], "children": [{"id": "b", "apps": ["two"]}]},
            {"id": "c", "projection": {"app_id": "one"}},
            {"id": "d"},
            {"id": "e", "apps": ["two"], "child": {"id": "f", "app_id": "one"}},
            {"event": "unmapped history entry"},
        ],
    }
    projected = state_seed.project_world(world, "one")
    assert [e["id"] for e in projected["entities"]] == ["a", "c"]
    assert projected["entities"][0]["children"] == []
    assert projected["reference_date"] == "2026-09-01"


def test_group_output_cannot_return_unrequested_collections(exported):
    root, folder = exported
    models = FakeModels(core(), app_state())
    models.config = {"design": {"seed_collections_per_call": 1}}
    # A group slip is recorded, not raised: raising discarded every other app the seed had already
    # authored, which is how three companies died with nothing on disk for hundreds of model calls.
    report = state_seed.seed_world(root, folder, models=models)
    assert report["status"] == "seeded_review_failed"
    problems = read(folder / "world" / "SEED.json")["author_problems"]["demo_mock"]
    assert any("extra keys" in problem["issue"] for problem in problems)
    assert (folder / "world" / "demo_mock.state.json").exists()


@pytest.fixture
def authored_folder(root, company, workflow):
    folder = root / "export"
    company.workers.append(company.workers[0].model_copy(update={"id": "boss"}))
    company.teams[0].worker_ids.append("boss")
    company.outlines[0].worker_ids.append("boss")
    company.software[0].catalog_app_ids = ["google_docs_mock"]
    company.software[0].status = "catalog_candidate"
    write(folder / "company.json", company.model_dump())
    write(folder / "MANIFEST.json", {"company_id": company.id, "stages": {}, "tasks": [], "hashes": {}})
    write(
        folder / "apps.json",
        {
            "apps": [
                {
                    "app_id": "google_docs_mock",
                    "role": "standard",
                    "state_file": "world/google_docs_mock.state.json",
                    "top_level_keys": ["currentUser", "users", "documents", "comments", "ui"],
                }
            ]
        },
    )
    state = {
        "currentUser": {},
        "users": [],
        "ui": {},
        "documents": {
            "doc1": {
                "id": "doc1",
                "title": "Instrument policy",
                "status": "draft",
                "updatedAt": "2026-09-01",
                "body": "NEVER SEND FULL STATE",
            }
        },
        "comments": [{"id": 7, "subject": "Tolerance discrepancy", "text": "PRIVATE LONG BODY"}],
    }
    write(folder / "world" / "google_docs_mock.state.json", state)
    write(folder / "world" / "world.json", {"entities": [{"id": "doc1", "apps": ["google_docs_mock"]}]})
    raw = workflow.model_dump()
    raw.update(schema_version="2", execution_mode="digital", calendar_days=None, manager_id="boss", phases=[])
    raw["worker_ids"].append("boss")
    raw["contributions"].append({**raw["contributions"][0], "worker_id": "boss"})
    raw["completion"] = {
        "outcomes": [
            {
                "id": "complete",
                "when": "Evidence supports release",
                "required_phase_ids": [],
                "observable": "A justified disposition is recorded",
            }
        ],
        "feasible_path": {
            "outcome_id": "complete",
            "steps": [{"action_and_result": "Compare and decide"}],
            "constraint_checks": ["Policy supports the decision"],
            "numeric_checks": [],
        },
    }
    return folder, raw


class TaskModels:
    def __init__(self, raw, mutate=None, verdict="accept", on_review=None):
        self.config = {"design": {"runtime_adapter": "hub"}}
        self.raw, self.mutate, self.verdict, self.on_review = raw, mutate, verdict, on_review
        self.calls = []

    def call(self, job, prompt, response_type, **options):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        self.calls.append((job, prompt, payload, options))
        if job == "expand":
            raw = {
                **self.raw,
                "feature_cell": payload["feature_matrix"][0]["feature_cell"],
                "initial_materials": ["google_docs_mock.documents#doc1", "google_docs_mock.comments#7"],
            }
            if self.mutate:
                self.mutate(raw)
            value = {"workflows": [raw], "amendment": None, "selection_reason": "Existing unresolved work"}
        else:
            assert job == "review"
            if self.on_review:
                self.on_review()
            value = {
                "company_verdict": "accept",
                "company_reasons": [],
                "tasks": [
                    {
                        "workflow_id": self.raw["id"],
                        "verdict": self.verdict,
                        "quality": 4,
                        "novelty": "distinct",
                        "duplicate_of": "",
                        "reasons": [] if self.verdict == "accept" else ["Insufficient consequential work"],
                    }
                ],
            }
        return response_type.model_validate(value), {
            "model": "codex/double",
            "call_id": job,
            "session_id": job,
        }


@pytest.mark.parametrize("verdict", ["accept", "reject"])
def test_review_supplied_task_preserves_source_and_requires_acceptance(
    root, authored_folder, monkeypatch, verdict
):
    folder, raw = authored_folder
    raw.update(
        feature_cell={
            "collections": ["google_docs_mock.documents", "google_docs_mock.comments"],
            "decision_type": "investigate-root-cause",
        },
        initial_materials=["google_docs_mock.documents#doc1", "google_docs_mock.comments#7"],
    )
    for contribution in raw["contributions"]:
        contribution["apps"] = ["google_docs_mock"]
    workers = raw["worker_ids"]
    raw["assessment"] = {
        "criteria": [
            {
                "criterion": i + 1,
                "weight": 1,
                "must_pass": True,
                "evidence": [raw["initial_materials"][0]],
                "rubric": "The disposition follows the supplied policy.",
                "failure_cases": ["Unsupported disposition"],
            }
            for i in range(len(raw["success_criteria"]))
        ],
        "dependencies": [
            {
                "worker_id": w,
                "inputs": [raw["initial_materials"][0]],
                "produces": "A supported assessment",
                "consumed_by": [workers[(i + 1) % len(workers)]],
                "necessity": "The consumer needs this assessment",
                "authority_sources": [],
            }
            for i, w in enumerate(workers)
        ],
        "alternative_successes": [],
        "prohibited_shortcuts": ["Unsupported completion claims"],
    }
    source = folder / "tasks/_repairs/candidate.json"
    write(source, raw)
    before = source.read_bytes()
    write(
        folder / "tasks/author-original.json",
        {
            "rejected": [raw["id"]],
            "receipt": {
                "model": "codex/author",
                "call_id": "original-call",
                "session_id": "original-session",
            },
        },
    )
    snapshot = task_author.seeded_snapshot(folder)
    snapshot.update(
        accepted_world=True,
        identities={w: {"google_docs_mock": {"id": w}} for w in workers},
        worker_apps={w: ["google_docs_mock"] for w in workers},
    )
    monkeypatch.setattr(task_author, "seeded_snapshot", lambda *a, **kw: snapshot)
    write(folder / "world/FROZEN.json", {"status": "test fixture"})
    model = TaskModels(raw, verdict=verdict)
    report = task_author.review_candidate(root, folder, source, models=model)
    assert report["accepted"] == (verdict == "accept")
    assert [c[0] for c in model.calls] == ["review"]
    assert "NEVER SEND FULL STATE" in model.calls[0][1]  # exact supporting record reaches review
    assert source.read_bytes() == before
    assert read(folder / "MANIFEST.json")["tasks"] == ([raw["id"]] if verdict == "accept" else [])
    if verdict == "accept":
        with pytest.raises(ValueError, match="cannot be replaced"):
            task_author.review_candidate(root, folder, source, models=model)


def test_author_tasks_resolves_records_and_uses_same_digital_design_and_review(root, authored_folder):
    folder, raw = authored_folder
    frozen = digest((folder / "world" / "google_docs_mock.state.json").read_bytes())
    models = TaskModels(raw)
    report = task_author.author_tasks(root, folder, 1, models)
    assert report["accepted"] == [raw["id"]] and not report["rejected"]
    assert [c[0] for c in models.calls] == ["expand", "review"]
    _, prompt, payload, options = models.calls[0]
    assert "Design complete digital work cycles" in prompt
    assert "NEVER SEND FULL STATE" not in prompt and "PRIVATE LONG BODY" not in prompt
    assert payload["public_brief_rules"] and payload["standard_bundle_rule"]
    assert payload["seeded_world"]["standard_apps"] == ["google_docs_mock"]
    fields = options["schema"]["$defs"]["Workflow"]["properties"]
    assert fields["execution_mode"]["const"] == "digital"
    assert fields["manager_id"]["type"] == "string" and fields["events"]["maxItems"] == 0
    task = read(folder / "tasks" / raw["id"] / "workflow.json")
    assert task["feature_cell"] == payload["feature_matrix"][0]["feature_cell"]
    assert set(task["feature_cell"]["collections"]) == {
        "google_docs_mock.documents",
        "google_docs_mock.comments",
    }
    assignment = read(folder / "tasks" / raw["id"] / "assignment.json")
    assert set(assignment) == {"workflow_id", "company_id", "title", "brief"}
    assert digest((folder / "world" / "google_docs_mock.state.json").read_bytes()) == frozen
    assert read(folder / "MANIFEST.json")["tasks"] == [raw["id"]]
    assert "Frozen seeded workspace" in models.calls[1][1]


@pytest.mark.parametrize(
    "materials, message",
    [
        (["google_docs_mock.documents#missing", "google_docs_mock.comments#7"], "documents#missing"),
        (["google_docs_mock.documents#doc1"], "comments#<decisive record required>"),
        (["Customer requirements and nonexistent policy"], "Customer requirements"),
    ],
)
def test_author_rejects_missing_ids_before_review(root, authored_folder, materials, message):
    folder, raw = authored_folder
    models = TaskModels(raw, lambda draft: draft.update(initial_materials=materials))
    with pytest.raises(ValueError, match=message):
        task_author.author_tasks(root, folder, 1, models)
    assert len(models.calls) == 1
    assert not (folder / "tasks" / raw["id"]).exists()
    assert (folder / "tasks" / "_rejected" / raw["id"] / "rejection.json").is_file()


def test_author_rejects_feature_cell_without_decisive_input(root, authored_folder):
    folder, raw = authored_folder
    models = TaskModels(
        raw, lambda draft: draft["feature_cell"].update(collections=["google_docs_mock.users"])
    )
    with pytest.raises(ValueError, match="users#<decisive record required>"):
        task_author.author_tasks(root, folder, 1, models)
    assert len(models.calls) == 1
    assert (folder / "tasks" / "_rejected" / raw["id"] / "rejection.json").is_file()


@pytest.mark.parametrize("verdict", ["revise", "reject"])
def test_review_rejections_are_private_and_unpublished(root, authored_folder, verdict):
    folder, raw = authored_folder
    report = task_author.author_tasks(root, folder, 1, TaskModels(raw, verdict=verdict))
    assert report["rejected"] == [raw["id"]] and not report["accepted"]
    assert not (folder / "tasks" / raw["id"]).exists()
    rejected = folder / "tasks" / "_rejected" / raw["id"]
    assert read(rejected / "review.json")["review"]["tasks"][0]["verdict"] == verdict
    assert not (rejected / "assignment.json").exists()
    assert read(folder / "MANIFEST.json")["tasks"] == []


def test_frozen_world_drift_during_review_blocks_publication(root, authored_folder):
    folder, raw = authored_folder
    models = TaskModels(raw, on_review=lambda: write(folder / "world" / "world.json", {}))
    with pytest.raises(ValueError, match="changed during authoring"):
        task_author.author_tasks(root, folder, 1, models)
    assert not (folder / "tasks" / raw["id"]).exists()


def test_expand_accepts_seeded_world_and_preserves_feature_contract(root, authored_folder):
    folder, raw = authored_folder
    snapshot = task_author.seeded_snapshot(folder)
    company = task_author.Company.model_validate(read(folder / "company.json"))
    models = TaskModels(raw)
    tasks, _ = workflows.expand(
        models,
        "skill",
        company,
        [],
        ["release"],
        seeded_world=snapshot,
        catalogs=task_author.Catalogs(root / "catalogs"),
    )
    assert tasks[0].execution_mode == "digital" and tasks[0].feature_cell


def test_review_cannot_reuse_author_session(root, authored_folder):
    folder, raw = authored_folder

    class ReusedSession(TaskModels):
        def call(self, *args, **kwargs):
            result, receipt = super().call(*args, **kwargs)
            receipt["session_id"] = "shared"
            return result, receipt

    with pytest.raises(ValueError, match="reused an author session"):
        task_author.author_tasks(root, folder, 1, ReusedSession(raw))
    assert not (folder / "tasks" / raw["id"]).exists()


def test_group_merge_still_checks_full_schema(exported):
    root, folder = exported
    apps = read(folder / "apps.json")
    apps["apps"][0]["top_level_keys"].remove("tickets")
    write(folder / "apps.json", apps)
    models = GroupModels(core(), app_state())
    models.config = {"design": {"seed_collections_per_call": 2}}
    report = state_seed.seed_world(root, folder, models=models)
    assert report["status"] == "seeded_review_failed"
    problems = read(folder / "world" / "SEED.json")["author_problems"]["demo_mock"]
    assert any("missing documented keys" in p["issue"] and "tickets" in p["issue"] for p in problems)
    assert (folder / "world" / "demo_mock.state.json").exists()


def test_group_repair_receives_only_previous_requested_collections(exported):
    from test_state_seed import accept, revise

    root, folder = exported
    models = GroupModels(core(), app_state(), reviews=[revise(), accept()])
    models.config = {"design": {"seed_collections_per_call": 2}}
    report = state_seed.seed_world(root, folder, models=models)
    repairs = [
        json.loads(prompt.rsplit("\n", 1)[1])
        for _, prompt, kind in models.calls
        if kind is state_seed.AppStateResult and '"revision_feedback"' in prompt
    ]
    # The finding names ticket 7, so only the group holding tickets is redone; the other group
    # keeps its records (and any sharded volume) untouched.
    assert len(repairs) == 1 and "tickets" in repairs[0]["named_collections"]
    assert all(set(json.loads(p["previous_state_json"])) == set(p["named_collections"]) for p in repairs)
    assert report["review"] == "accept"


@pytest.mark.parametrize("count", [0, 2])
def test_bad_task_count_fails_without_model(root, authored_folder, count):
    folder, raw = authored_folder
    models = TaskModels(raw)
    with pytest.raises(ValueError, match="positive|unused outlines"):
        task_author.author_tasks(root, folder, count, models)
    assert models.calls == []


def test_duplicate_group_contract_cannot_overwrite_an_earlier_collection(exported):
    root, folder = exported
    apps = read(folder / "apps.json")
    apps["apps"][0]["top_level_keys"].append("tickets")
    write(folder / "apps.json", apps)
    models = GroupModels(core(), app_state())
    models.config = {"design": {"seed_collections_per_call": 2}}
    with pytest.raises(ValueError, match="duplicate.*collection"):
        state_seed.seed_world(root, folder, models=models)
    assert not (folder / "world/demo_mock.state.json").exists()


def test_decisive_record_only_in_canonical_world_is_rejected(root, authored_folder):
    folder, raw = authored_folder
    state = read(folder / "world/google_docs_mock.state.json")
    state["documents"] = {}
    write(folder / "world/google_docs_mock.state.json", state)
    models = TaskModels(raw)
    with pytest.raises(ValueError, match="documents#doc1"):
        task_author.author_tasks(root, folder, 1, models)
    assert len(models.calls) == 1


def test_native_app_id_case_is_preserved_when_resolving_decisive_records():
    task = SimpleNamespace(
        id="t1",
        initial_materials=["Zendesk_mock.tickets#1"],
        feature_cell={"collections": ["Zendesk_mock.tickets"]},
    )
    task_author.validate_seeded_records(
        task,
        {
            "app_index": {"Zendesk_mock": {"tickets": [{"id": 1}]}},
            "states": {"Zendesk_mock": {"tickets": [{"id": 1}]}},
        },
    )


def test_empty_single_key_group_is_retried_once_then_accepted(exported):
    """Selection state or an upload queue is legitimately empty; the author is asked once, then believed."""
    root, folder = exported

    class EmptyUiModels(GroupModels):
        ui_requests = 0

        def call(self, job, prompt, response_type, **kwargs):
            if response_type is state_seed.AppStateResult:
                payload = json.loads(prompt.rsplit("\n", 1)[1])
                if payload["named_collections"] == ["ui"]:
                    EmptyUiModels.ui_requests += 1
                    self.app_value = app_state({"ui": {}})
                    return super(GroupModels, self).call(job, prompt, response_type, **kwargs)
            return super().call(job, prompt, response_type, **kwargs)

    models = EmptyUiModels(core(), app_state())
    models.config = {"design": {"seed_collections_per_call": 1}}
    state_seed.seed_world(root, folder, models=models)
    assert EmptyUiModels.ui_requests == 2
    assert read(folder / "world" / "demo_mock.state.json")["ui"] == {}


def test_missing_directory_members_redo_only_the_user_groups(exported):
    """Identities absent from the app's users collection re-author that group once, keeping the rest."""
    root, folder = exported
    requested = []

    class DirectoryModels(GroupModels):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is state_seed.AppStateResult:
                payload = json.loads(prompt.rsplit("\n", 1)[1])
                requested.append(tuple(payload["named_collections"]))
                if payload["named_collections"] == ["users"] and "revision_feedback" not in payload:
                    self.app_value = app_state({"users": []})  # forgot the workers
                    return super(GroupModels, self).call(job, prompt, response_type, **kwargs)
            return super().call(job, prompt, response_type, **kwargs)

    models = DirectoryModels(core(), app_state())
    models.config = {"design": {"seed_collections_per_call": 1}}
    state_seed.seed_world(root, folder, models=models)
    assert requested.count(("users",)) == 2
    assert requested.count(("tickets",)) == 1
    assert read(folder / "world" / "demo_mock.state.json")["users"]


def test_a_shard_record_is_not_a_duplicate_of_the_channel_it_belongs_to():
    """Merging deduped on any id-shaped field, so foreign keys read as identity.

    A chat message whose only such field is channelId matched every other message in the channel,
    so whole shards were generated and discarded: 455 records in, 0 inserted, 1,715 rows lost in
    one sample. Records with no identifier of their own are now always kept, because a shard
    covers a window the collection does not hold yet.
    """
    from company_envs.world.state_seed import merge_records, primary_id

    assert primary_id({"channelId": "general", "senderId": "u1"}) is None
    assert primary_id({"messageId": "m1", "channelId": "general"}) == "m1"

    seeded = [{"channelId": "general", "content": "seed"}]
    fresh = [{"channelId": "general", "content": c} for c in ("a", "b")]
    assert len(merge_records(seeded, fresh)) == 3

    # A real identifier still wins: the incumbent is kept and the newcomer dropped.
    kept = merge_records([{"messageId": "m0", "content": "old"}], [{"messageId": "m0", "content": "new"}])
    assert kept == [{"messageId": "m0", "content": "old"}]


def test_merge_records_and_time_windows():
    from company_envs.world.state_seed import merge_records, record_count, time_windows

    assert merge_records([{"id": 1}, {"id": 2}], [{"id": 2}, {"id": 3}]) == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert merge_records(
        {"c1": [{"messageId": "m1"}]},
        {"c1": [{"messageId": "m1"}, {"messageId": "m2"}], "c2": [{"messageId": "m3"}]},
    ) == {"c1": [{"messageId": "m1"}, {"messageId": "m2"}], "c2": [{"messageId": "m3"}]}
    assert (
        merge_records(
            {"doc1": {"id": "doc1", "title": "a"}},
            {"doc1": {"id": "doc1", "title": "b"}, "doc2": {"id": "doc2"}},
        )["doc1"]["title"]
        == "a"
    )
    assert record_count({"c1": [1, 2], "c2": [3]}) == 3 and record_count([1]) == 1 and record_count("x") == 0
    windows = time_windows("2026-09-08", 3)
    assert len(windows) == 2 and windows[0][0] < windows[0][1] <= windows[1][0] < windows[1][1] < "2026-09-08"


def test_a_repair_reuses_the_history_it_already_paid_for(exported):
    """Shard windows are bought once per author and re-merged, not re-called on every repair.

    extend_collection ran after every author() call, repairs included, so one generation could
    regenerate the same (app, collection, window) many times: 5,179 duplicate shard calls cost
    442 model-hours. Those records sit weeks before the reference date, outside the recent layer
    a repair rewrites.
    """
    from test_state_seed import accept, revise

    root, folder = exported
    shard_prompts = []

    class ShardModels(GroupModels):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is state_seed.AppStateResult:
                payload = json.loads(prompt.rsplit("\n", 1)[1])
                if "shard" in payload:
                    shard_prompts.append(payload["shard"])
                    key = payload["shard"]["collection"]
                    older = [{"id": 100 + len(shard_prompts), "subject": "older"}] if key == "tickets" else []
                    self.app_value = app_state({key: older})
                    return super(GroupModels, self).call(job, prompt, response_type, **kwargs)
            return super().call(job, prompt, response_type, **kwargs)

    models = ShardModels(core(), app_state(), reviews=[revise(), accept()])
    models.config = {
        "design": {"seed_collections_per_call": 1, "seed_shards": 3, "seed_shard_min_records": 1}
    }
    report = state_seed.seed_world(root, folder, models=models)

    assert report["review"] == "accept"  # a repair round did run
    windows = [s["index"] for s in shard_prompts if s["collection"] == "tickets"]
    assert windows == [1, 2], f"each earlier window bought once, got {windows}"
    # The repaired app still carries the history: the records are merged back, not dropped.
    tickets = read(folder / "world" / "demo_mock.state.json")["tickets"]
    assert sum(1 for t in tickets if t.get("subject") == "older") == 2


def test_high_volume_collections_are_extended_by_shards(exported):
    """A collection at or above the shard threshold gets one call per earlier window, merged by id."""
    root, folder = exported
    shard_prompts = []

    class ShardModels(GroupModels):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is state_seed.AppStateResult:
                payload = json.loads(prompt.rsplit("\n", 1)[1])
                if "shard" in payload:
                    shard_prompts.append(payload["shard"])
                    key = payload["shard"]["collection"]
                    # Only the ticket queue has older months; a directory does not grow by shard.
                    older = (
                        [{"id": 100 + payload["shard"]["index"], "subject": "older"}]
                        if key == "tickets"
                        else []
                    )
                    self.app_value = app_state({key: older})
                    return super(GroupModels, self).call(job, prompt, response_type, **kwargs)
            return super().call(job, prompt, response_type, **kwargs)

    models = ShardModels(core(), app_state())
    models.config = {
        "design": {"seed_collections_per_call": 1, "seed_shards": 3, "seed_shard_min_records": 1}
    }
    state_seed.seed_world(root, folder, models=models)
    tickets = read(folder / "world" / "demo_mock.state.json")["tickets"]
    assert [t["id"] for t in tickets] == [7, 101, 102]
    assert {s["collection"] for s in shard_prompts} == {"tickets"}
    assert all(s["window"]["from"] < s["window"]["to"] and s["existing_ids"] for s in shard_prompts)


def test_the_seeded_designer_can_read_the_record_bodies_the_index_drops(root, authored_folder):
    """The app index keeps ids, titles and dates and drops every body, so the designer could not
    tell whether a collection carries a decision: 55 of 64 rejected drafts were empty batches whose
    selection_reason said the index omits the contents or that no read_seed tool was enabled."""
    folder, raw = authored_folder
    models = TaskModels(raw)
    task_author.author_tasks(root, folder, 1, models)
    _, _, payload, options = next(c for c in models.calls if c[0] == "expand")
    assert "read_seed" in payload["available_tools"]
    assert options["context"]["seed"] == task_author.seeded_snapshot(folder)["states"]
    # Whole states in the prompt is what put 234 of 316 mining calls over the provider's limit.
    assert "states" not in payload["seeded_world"] and "provenance" not in payload["seeded_world"]


def test_a_decisive_collection_the_world_cannot_carry_is_named_at_design_time(tmp_path):
    """feature_cell was never checked against the seeded world, so three defects that are terminal
    five stages later were legal at design time. Measured over the 108 accepted tasks in the 60
    seeded worlds: three decisive collections in three tasks hold no records -- both of one
    company's tasks turn on booking_com_mock.bookings, which is literally [], and it is retired.
    The interface-state and identity rules are the grader author's own refusals brought forward;
    both are latent today (0 of 216 decisive collections)."""
    states = {
        "docs": {
            "documents": {"doc1": {"id": "doc1"}},
            "drafts": [],
            "currentSortColumn": "name",
            "currentUser": {"id": "u1"},
            "messages": {"ch-a": [{"messageId": "m1"}]},
            "savedIds": ["p31"],
        }
    }
    problems = task_author.feature_cell_problems(
        ["docs.documents", "docs.messages", "docs.savedIds"], states, {"docs": "currentUser"}
    )
    assert problems == []  # a map of lists and a list of bare ids both hold records
    assert "holds no records" in task_author.feature_cell_problems(["docs.drafts"], states)[0]
    assert "interface state" in task_author.feature_cell_problems(["docs.currentSortColumn"], states)[0]
    assert "is not a seeded collection" in task_author.feature_cell_problems(["docs.absent"], states)[0]
    identity = task_author.feature_cell_problems(["docs.currentUser"], states, {"docs": "currentUser"})
    assert "signed-in account" in identity[0]


def test_native_ids_and_counts_both_see_the_user_collection(authored_folder):
    """The former literal-id index hid Slack-style users while counts saw them."""
    folder, _ = authored_folder
    state = read(folder / "world" / "google_docs_mock.state.json")
    state["users"] = [{"userId": "u1", "fullName": "Ada"}, {"userId": "u2", "fullName": "Bo"}]
    write(folder / "world" / "google_docs_mock.state.json", state)
    snapshot = task_author.seeded_snapshot(folder)
    assert snapshot["app_index"]["google_docs_mock"]["users"] == [{"id": "u1"}, {"id": "u2"}]
    assert snapshot["record_counts"]["google_docs_mock.users"] == 2
    assert snapshot["record_counts"]["google_docs_mock.ui"] == 0  # an empty keyed object is not one
    assert snapshot["identity_keys"] == {}  # this fixture declares no identity_key


def test_feature_cell_report_names_the_tasks_the_seeded_world_cannot_carry(authored_folder):
    """The check stage's one line. A task listed here cannot be graded, and saying so before its
    golden, grader and calibration calls are paid for is the whole point; for the company whose
    every task is listed it is the difference between a retirement and a rewrite."""
    folder, _ = authored_folder

    def task(name, collection):
        write(
            folder / "tasks" / name / "workflow.json",
            {"id": name.rsplit("/", 1)[-1], "feature_cell": {"collections": [collection]}},
        )

    task("t1", "google_docs_mock.documents")
    task("t2", "google_docs_mock.users")
    task("_rejected/t3", "google_docs_mock.users")  # a rejected draft is not the check stage's problem
    report = task_author.feature_cell_report(folder)
    assert list(report) == ["t2"] and "holds no records" in report["t2"][0]
