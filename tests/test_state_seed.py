"""seed-world writes validated app states, identities and materials from one model result (model stubbed)."""

import json
from copy import deepcopy

import pytest
from test_hub_world import SCHEMA_WITH_USERS, SEEDED, USERS

from company_envs.models import PromptTooLarge
from company_envs.storage import read, write
from company_envs.world import state_seed


@pytest.fixture
def exported(tmp_path):
    root = tmp_path
    (root / "catalogs" / "app_schemas").mkdir(parents=True)
    (root / "catalogs" / "app_schemas" / "demo_mock.md").write_text(SCHEMA_WITH_USERS)
    (root / ".agents" / "skills" / "company-world-states").mkdir(parents=True)
    (root / ".agents" / "skills" / "company-world-states" / "SKILL.md").write_text(
        "---\nname: company-world-states\ndescription: test\n---\nSeed the world.\n"
    )
    (root / ".agents" / "skills" / "company-world-review").mkdir(parents=True)
    (root / ".agents" / "skills" / "company-world-review" / "SKILL.md").write_text(
        "---\nname: company-world-review\ndescription: test\n---\nReview the world.\n"
    )
    (root / "config.toml").write_text(
        "[generation]\ncompanies = 1\ntasks = 1\nworkers = 1\ncompletion_timeout_seconds = 5\n"
        '[models]\nexpand = ["codex/gpt-test"]\nreasoning = "high"\n'
    )
    folder = root / "companies" / "acme"
    (folder / "tasks" / "acme_o1").mkdir(parents=True)
    (folder / "world").mkdir()
    write(
        folder / "MANIFEST.json", {"company_id": "acme", "stages": {"stage2_world": "pending"}, "hashes": {}}
    )
    write(folder / "company.json", {"id": "acme", "workers": [{"id": "w1"}, {"id": "w2"}], "software": []})
    write(folder / "tasks" / "acme_o1" / "workflow.json", {"id": "acme_o1", "brief": "Do the thing."})
    write(
        folder / "tasks" / "acme_o1" / "assignment.json", {"workflow_id": "acme_o1", "brief": "Do the thing."}
    )
    write(
        folder / "apps.json",
        {
            "identity": "per_worker_proxy",
            "workers": ["w1", "w2"],
            "identities_file": "world/identities.json",
            "apps": [
                {
                    "app_id": "demo_mock",
                    "hub_seedable": True,
                    "schema": "catalogs/app_schemas/demo_mock.md",
                    "top_level_keys": ["currentUser", "tickets", "ui", "users"],
                    "identity_key": "currentUser",
                    "state_file": "world/demo_mock.state.json",
                }
            ],
        },
    )
    return root, folder


def core(**overrides):
    base = {
        "rationale": "small support desk",
        "reference_date": "2026-09-08",
        "operating_scope": "one district desk",
        "assumptions": ["synthetic names"],
        "entities_json": json.dumps({"entities": [{"id": "t7", "kind": "ticket"}], "history": []}),
        "identities": [
            {"worker_id": "w1", "app_id": "demo_mock", "user_json": json.dumps(USERS[0])},
            {"worker_id": "w2", "app_id": "demo_mock", "user_json": json.dumps(USERS[1])},
        ],
        "worker_apps": [
            {"worker_id": "w1", "app_ids": ["demo_mock"]},
            {"worker_id": "w2", "app_ids": ["demo_mock"]},
        ],
        "materials": [{"worker_id": "w1", "path": "notes/desk.md", "content": "Desk policy v3."}],
    }
    base.update(overrides)
    return state_seed.WorldCore.model_validate(base)


def app_state(state=SEEDED, app_id="demo_mock"):
    """The model's answer for one collection group; ``state`` may be any JSON value it writes."""
    return state_seed.AppStateResult.model_validate(
        {"app_id": app_id, "rationale": "desk queue", "state_json": json.dumps(state)}
    )


def accept():
    return state_seed.WorldReview.model_validate({"verdict": "accept", "summary": "coherent", "findings": []})


def revise(target="demo_mock"):
    return state_seed.WorldReview.model_validate(
        {
            "verdict": "revise",
            "summary": "one drift",
            "findings": [
                {
                    "target": target,
                    "severity": "error",
                    "issue": "ticket 7 status differs from world",
                    "evidence": "tickets/0",
                }
            ],
        }
    )


class FakeModels:
    def __init__(self, core_value, app_value, reviews=None, app_values=None):
        self.core_value, self.app_value, self.calls = core_value, app_value, []
        self.reviews = list(reviews or [accept()])
        self.app_values = list(app_values or [])

    def call(self, job, prompt, response_type, **kwargs):
        self.calls.append((job, prompt, response_type))
        if response_type is state_seed.WorldCore:
            value = self.core_value
        elif response_type is state_seed.WorldReview:
            value = self.reviews.pop(0)
        elif response_type is state_seed.IdentitiesOnly:
            value = state_seed.IdentitiesOnly(identities=self.core_value.identities)  # repair changes nothing
        else:
            value = self.app_values.pop(0) if self.app_values else self.app_value
        return value, {"model": "codex/gpt-test", "call_id": f"c{len(self.calls)}", "status": "complete"}


def test_seed_world_writes_states_identities_materials_and_manifest(exported):
    root, folder = exported
    models = FakeModels(core(), app_state())
    report = state_seed.seed_world(root, folder, models=models)
    assert [c[2] for c in models.calls] == [
        state_seed.WorldCore,
        state_seed.AppStateResult,
        state_seed.WorldReview,
    ]
    core_prompt, app_prompt = models.calls[0][1], models.calls[1][1]
    assert (
        '"call": "world_core"' in core_prompt
        and "Do the thing." in core_prompt
        and "Seed the world." in core_prompt
    )
    assert (
        '"call": "app_state"' in app_prompt
        and "## State Schema" in app_prompt
        and '"canonical_world"' in app_prompt
    )
    assert read(folder / "world" / "demo_mock.state.json") == SEEDED
    assert read(folder / "world" / "identities.json") == {
        "w1": {"demo_mock": USERS[0]},
        "w2": {"demo_mock": USERS[1]},
    }
    assert read(folder / "world" / "worker_apps.json") == {"w1": ["demo_mock"], "w2": ["demo_mock"]}
    assert (folder / "world" / "materials" / "w1" / "notes" / "desk.md").read_text() == "Desk policy v3."
    assert read(folder / "MANIFEST.json")["stages"]["stage2_world"] == "seeded_reviewed"
    assert read(folder / "world" / "SEED.json")["receipts"]["world_core"]["call_id"] == "c1"
    assert read(folder / "world" / "CHECKS.json")["ok"] is True
    assert read(folder / "world" / "REVIEW.json")["final_verdict"] == "accept"
    assert report["apps"] == ["demo_mock"] and report["materials"] == 1 and report["calls"] == 3
    assert '"call": "world_review"' in models.calls[2][1] and '"mechanical_findings"' in models.calls[2][1]


def test_revise_verdict_repairs_only_the_named_app_once_then_re_reviews(exported):
    root, folder = exported
    repaired = {**SEEDED, "ui": {"activeView": 2}}
    models = FakeModels(
        core(), app_state(), reviews=[revise(), accept()], app_values=[app_state(), app_state(repaired)]
    )
    report = state_seed.seed_world(root, folder, models=models)
    kinds = [c[2].__name__ for c in models.calls]
    assert kinds == ["WorldCore", "AppStateResult", "WorldReview", "AppStateResult", "WorldReview"]
    repair_prompt = models.calls[3][1]
    assert (
        '"revision_feedback"' in repair_prompt
        and "ticket 7 status differs" in repair_prompt
        and '"previous_state_json"' in repair_prompt
    )
    assert read(folder / "world" / "demo_mock.state.json") == repaired
    review = read(folder / "world" / "REVIEW.json")
    assert review["final_verdict"] == "accept" and review["rounds"][1]["repaired"] == ["demo_mock"]
    assert report["status"] == "seeded_reviewed" and report["calls"] == 5


def test_unresolved_revise_is_recorded_as_failed_not_hidden(exported):
    root, folder = exported
    models = FakeModels(core(), app_state(), reviews=[revise(), revise()])
    report = state_seed.seed_world(root, folder, models=models)
    assert report["status"] == "seeded_review_failed"
    assert read(folder / "MANIFEST.json")["stages"]["stage2_world"] == "seeded_review_failed"
    assert read(folder / "world" / "REVIEW.json")["final_verdict"] == "revise"


def test_review_can_be_disabled(exported):
    root, folder = exported
    report = state_seed.seed_world(root, folder, models=FakeModels(core(), app_state()), review_rounds=0)
    assert (
        report["status"] == "seeded_not_verified"
        and report["review"] is None
        and not (folder / "world" / "REVIEW.json").exists()
    )


@pytest.mark.parametrize(
    ("core_override", "app_value", "message"),
    [
        (
            {"identities": [{"worker_id": "w1", "app_id": "demo_mock", "user_json": json.dumps(USERS[0])}]},
            app_state(),
            "missing identity for worker w2",
        ),
        (
            {"materials": [{"worker_id": "w1", "path": "../etc/passwd", "content": "x"}]},
            app_state(),
            "invalid material",
        ),
        (
            {"worker_apps": [{"worker_id": "w1", "app_ids": ["demo_mock"]}]},
            app_state(),
            "worker_apps must cover",
        ),
        # An answer for an app nobody asked about: never seen in the 99 companies on disk, and the
        # repair stages treat it as a reason to stop rather than spend a round.
        ({}, app_state(app_id="other_mock"), "returned other_mock"),
    ],
)
def test_seed_world_rejects_incoherent_results_and_writes_nothing(
    exported, core_override, app_value, message
):
    """A core the world cannot be built from still stops the seed: these are the core's own faults.

    The app-state faults that used to be here now come back as findings instead -- see
    ``test_one_app_group_slip_no_longer_discards_every_other_app``.
    """
    root, folder = exported
    with pytest.raises((ValueError, TypeError), match=message):
        state_seed.seed_world(root, folder, models=FakeModels(core(**core_override), app_value))
    assert not (folder / "world" / "demo_mock.state.json").exists()
    assert read(folder / "MANIFEST.json")["stages"]["stage2_world"] == "pending"


@pytest.mark.parametrize(
    ("app_value", "message"),
    [
        # The measured killers, in the words the author used: a group that came back with the wrong
        # keys (cresa, where the repair model's own label was the key) and a group that is not an
        # object at all (piper-sandler, eaglebrook, ultimus-fund-solutions, and four more worlds
        # whose review repair died on it).
        (app_state({"tickets": []}), "missing documented keys"),
        (app_state("not an object at all"), "must be a JSON object"),
    ],
)
def test_one_app_group_slip_no_longer_discards_every_other_app(exported, app_value, message):
    """One collection group the author cannot write is a finding, not a world thrown away.

    ``author`` gave a group one bounded retry and then raised; the raise escaped
    ``pool.map(author, contract)``, ``seed_world`` died and no SEED.json was written, so every
    other app's finished calls were lost -- a 20-minute core call and hours of app calls for one
    mis-keyed group. Measured on disk: piper-sandler, eaglebrook and ultimus-fund-solutions on
    ``collection group must be a JSON object`` (a TypeError that the retry handler did not even
    catch), cresa on ``group missing documented keys``, and four more worlds whose review-repair
    round died the same way. The fault is now carried where the repair loop and CHECKS.json
    already look.
    """
    root, folder = exported
    report = state_seed.seed_world(root, folder, models=FakeModels(core(), app_value))
    assert report["status"] == "seeded_review_failed", "the fault is not hidden"
    seed = read(folder / "world" / "SEED.json")
    problems = seed["author_problems"]["demo_mock"]
    assert any(message in p["issue"] for p in problems)
    # Written, so the rest of the world survives and a repair stage can reach this app.
    state = read(folder / "world" / "demo_mock.state.json")
    assert set(state) == set(read(folder / "apps.json")["apps"][0]["top_level_keys"])
    assert read(folder / "MANIFEST.json")["stages"]["stage2_world"] == "seeded_review_failed"
    checks = read(folder / "world" / "CHECKS.json")
    assert not checks["ok"] and any(message in f["message"] for f in checks["findings"])


def test_a_merged_state_that_fails_its_own_schema_is_recorded_not_raised(exported):
    """The same reasoning one level up: a state the validator faults is still every other app's work.

    ``SEED_FAILED.json`` records only ``{at, count, rc}``, so a raise here lost the diagnosis as
    well as the world; the finding names it in CHECKS.json and the world reaches disk for the
    repair stage. The world is not accepted either way.
    """
    root, folder = exported
    apps = read(folder / "apps.json")
    apps["apps"][0]["top_level_keys"].remove("tickets")  # the schema still documents it
    write(folder / "apps.json", apps)

    class Groups(FakeModels):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is state_seed.AppStateResult:
                packet = json.loads(prompt.split("\n")[-1])
                self.calls.append((job, prompt, response_type))
                return app_state({k: SEEDED[k] for k in packet["named_collections"]}), {"call_id": "g"}
            return super().call(job, prompt, response_type, **kwargs)

    report = state_seed.seed_world(root, folder, models=Groups(core(), app_state()))
    assert report["status"] == "seeded_review_failed"
    problems = read(folder / "world" / "SEED.json")["author_problems"]["demo_mock"]
    assert any("missing documented keys" in p["issue"] and "tickets" in p["issue"] for p in problems)
    assert "tickets" not in read(folder / "world" / "demo_mock.state.json")


@pytest.mark.parametrize("field", ["name", "email"])
def test_identity_drift_survives_as_a_finding_after_the_retry(exported, field):
    """An identity the app's directory disagrees with is repaired once, then recorded.

    Raising after that retry is what killed eastman-chemical, median-technologies and
    sungrow-power-supply's repair rounds outright. The drift is still blocking -- the world is
    never accepted with it -- but the world is on disk and the mechanical check names it.
    """
    root, folder = exported
    value = core()
    value.identities[0] = value.identities[0].model_copy(
        update={"user_json": json.dumps({**USERS[0], field: "someone else"})}
    )
    report = state_seed.seed_world(root, folder, models=FakeModels(value, app_state()))
    assert report["status"] == "seeded_review_failed" and not report["mechanical_ok"]
    findings = read(folder / "world" / "CHECKS.json")["findings"]
    assert any("identity" in f["message"] and field in f["message"] for f in findings)
    assert (folder / "world/demo_mock.state.json").exists()


def test_a_username_disagreement_is_named_in_the_retry(exported):
    """The retry has to name every field the check reads, or it asks for the wrong repair.

    The complaint used to stop at name and email while the identity check also compared
    username, so a username disagreement came back with instructions that never mentioned it
    and failed again the same way. Usernames were the field that disagreed most often across
    the fleet: 1,150 disagreements against 131 on name and 128 on email.
    """
    root, folder = exported
    value = core()
    handles = [{**user, "username": user["name"].split()[0].casefold()} for user in USERS]
    for index, handle in enumerate(handles):
        value.identities[index] = value.identities[index].model_copy(update={"user_json": json.dumps(handle)})
    drifted = [{**handles[0], "username": "someone.else"}, handles[1]]
    wrong = {**SEEDED, "currentUser": drifted[0], "users": drifted}
    right = {**SEEDED, "currentUser": handles[0], "users": handles}

    models = FakeModels(value, app_state(right), app_values=[app_state(wrong), app_state(right)])
    state_seed.seed_world(root, folder, models=models)
    retries = [prompt for _job, prompt, kind in models.calls if kind is state_seed.AppStateResult]
    assert len(retries) == 2, "the drifted state should be sent back once"
    # The complaint quotes the error, which names the field on its own, so the assertion has to
    # be on the instruction the author is given rather than on the word appearing anywhere.
    assert "name, email and username" in retries[1]


def test_seed_accepts_identity_id_as_string(exported):
    root, folder = exported
    value = core()
    value.identities[0] = value.identities[0].model_copy(
        update={"user_json": json.dumps({**USERS[0], "id": "1"})}
    )
    assert (
        state_seed.seed_world(root, folder, models=FakeModels(value, app_state()))["status"]
        == "seeded_reviewed"
    )
    assert read(folder / "world/identities.json")["w1"]["demo_mock"]["id"] == 1
    from company_envs.world.hub_world import load_world

    assert load_world(folder, root)


def test_a_worker_missing_from_the_directory_is_spliced_in_not_raised_over(exported):
    """Raising discarded every other app the seed had already authored, for a one-record insert.

    Measured: around ten companies are terminal on this with nothing on disk for hundreds of model
    calls each, and six of the sixty seeded worlds could not be served for it. The protection this
    replaces is structural rather than a raise: identity_members searches the named user collections
    when the app has them, and elsewhere requires a matching name, email or username, so a ticket
    that happens to share an id can never stand in for a person.
    """
    root, folder = exported
    state = deepcopy(SEEDED)
    missing = state["users"][0]
    state["users"] = state["users"][1:]
    state["tickets"].append({"id": missing["id"]})
    report = state_seed.seed_world(root, folder, models=FakeModels(core(), app_state(state)))
    assert report["status"] == "seeded_reviewed"
    seeded = read(folder / "world" / "demo_mock.state.json")
    assert any(str(u.get("id")) == str(missing["id"]) for u in seeded["users"])
    assert {"id": missing["id"]} in seeded["tickets"], "the unrelated record is left alone"
    # An identity naming nobody is a malformed identity, not a missing directory entry.
    assert state_seed.splice_identities({"users": []}, {"w1": {"id": 42}}) == 0


def test_duplicate_json_collection_members_cannot_silently_drop_records():
    with pytest.raises(ValueError, match="duplicate.*tickets"):
        state_seed.parse_json('{"tickets": [{"id": 1}], "tickets": [{"id": 2}]}', "state")


@pytest.mark.parametrize("failure", ["repair", "review"])
def test_failed_repair_or_rereview_preserves_published_world(exported, failure):
    root, folder = exported
    state_seed.seed_world(root, folder, models=FakeModels(core(), app_state()))
    before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}

    class FailingModels(FakeModels):
        def call(self, job, prompt, response_type, **kwargs):
            if (failure == "repair" and '"revision_feedback"' in prompt) or (
                failure == "review" and response_type is state_seed.WorldReview and not self.reviews
            ):
                raise RuntimeError("injected model failure")
            return super().call(job, prompt, response_type, **kwargs)

    models = FailingModels(core(), app_state({**SEEDED, "ui": {"activeView": 2}}), reviews=[revise()])
    with pytest.raises(RuntimeError, match="injected"):
        state_seed.seed_world(root, folder, models=models)
    assert before == {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_malformed_model_json_is_repaired_once_instead_of_discarding_the_call(exported):
    """A syntax slip inside a 20-minute authoring call costs one short repair call, not a rerun."""
    root, folder = exported
    broken = json.dumps({"entities": [{"id": "t7", "kind": "ticket"}], "history": []})[:-1] + ",}"
    models = FakeModels(core(entities_json=broken), app_state())

    original_call = models.call

    def call(job, prompt, response_type, **kwargs):
        if response_type is state_seed.JsonRepair:
            assert "[entities_json]" in prompt and broken in prompt
            return state_seed.JsonRepair(fixed_json=broken[:-2] + "}"), {"call_id": "repair"}
        return original_call(job, prompt, response_type, **kwargs)

    models.call = call
    report = state_seed.seed_world(root, folder, models=models)
    assert report["status"] == "seeded_reviewed"
    assert read(folder / "world" / "world.json")["entities"][0]["id"] == "t7"


def test_unrepairable_json_still_fails_closed(exported):
    root, folder = exported
    models = FakeModels(core(entities_json="{not json at all"), app_state())
    original_call = models.call

    def call(job, prompt, response_type, **kwargs):
        if response_type is state_seed.JsonRepair:
            return state_seed.JsonRepair(fixed_json="{still broken"), {"call_id": "repair"}
        return original_call(job, prompt, response_type, **kwargs)

    models.call = call
    with pytest.raises(ValueError, match="even after repair"):
        state_seed.seed_world(root, folder, models=models)


def test_a_repair_that_answers_with_its_own_label_as_the_key_is_unwrapped():
    """The repair call is given "[<what>]" as a heading, and the model made it the top-level key.

    ``repair_json`` heads the broken text with the label; the repair model answered
    ``{"google_drive_mock collection group": {...}}`` and ``parse_json`` accepted it, so the
    envelope reached the group check as a collection nobody asked for -- "expected ['items'], got
    ['google_drive_mock collection group']" -- and cresa was retired at three strikes on it.
    """
    payload = {"items": [{"id": "d1"}]}

    class Wrapping:
        def __init__(self):
            self.prompts = []

        def call(self, job, prompt, response_type, **kwargs):
            self.prompts.append(prompt)
            wrapped = {"google_drive_mock collection group": payload}
            return state_seed.JsonRepair(fixed_json=json.dumps(wrapped)), {"call_id": "repair"}

    models = Wrapping()
    value = state_seed.parse_json(
        '{"items": [{"id": "d1"},]}', "google_drive_mock collection group", models=models, expected=["items"]
    )
    assert value == payload
    assert "['items']" in models.prompts[0], "the repair is told which keys the object has"
    # A single-key object whose key is a real collection is a state, not an envelope.
    assert state_seed.unwrap_label({"items": payload}, "x collection group", ["items"]) == {"items": payload}


def test_collection_groups_author_a_record_collection_one_call_at_a_time():
    """A call's wall time is what it has to write, so the unit of a call is one record collection.

    Over the 2,208 group calls on disk the correlation between output tokens and seconds is 0.975,
    and a four-collection call wrote a mean of 30,885 output tokens: 400 of the 601 world_states
    timeouts were four-collection calls, 193 core-hours at a mean of 1,736 s, each losing its whole
    group. The app's own record-field catalogue separates the expensive collections from the
    trivial ones almost exactly -- of the 817 one-collection calls on disk, the 268 for a
    catalogued collection ran a median 493 s (48% over 600 s) and the 549 for an uncatalogued one a
    median 7 s, p90 10 s, none over 600 s -- so a catalogued collection gets a call and the rest
    are still batched. An app the catalogue does not cover keeps the fixed grouping.
    """
    keys = ["users", "tickets", "ui", "selection", "filters", "drafts"]
    fields = {"users": ["id", "name"], "tickets": ["id", "status"]}
    assert state_seed.collection_groups(keys, fields, 4) == [
        ["users"],
        ["tickets"],
        ["ui", "selection", "filters", "drafts"],
    ]
    assert state_seed.collection_groups(keys, {}, 4) == [keys[:4], keys[4:]]
    assert state_seed.collection_groups(keys, fields, 1) == [[k] for k in keys]


def test_the_identity_book_stays_complete_while_the_grant_map_narrows():
    """Every worker keeps an identity in every app; the grant map decides who is a user of one.

    The book is insurance: a task mined after the world was seeded can widen a grant without
    re-seeding, and a granted app with no identity makes ``hub_vm._plan`` raise "Missing proxy
    endpoint". It is 1,671 records across the 60 seeded worlds where the grants need 1,522, and the
    8.9% surplus is exactly the 149 (worker, app) pairs in 40 worlds with no login -- which is a
    defect only where it reaches an app author, and that is narrowed in ``make_author``.
    """
    apps = {"demo_mock": {"identity_key": "currentUser"}, "other_mock": {"identity_key": "currentUser"}}
    both = ["demo_mock", "other_mock"]
    value = core(
        identities=[
            {"worker_id": w, "app_id": a, "user_json": json.dumps(USERS[i])}
            for i, w in enumerate(("w1", "w2"))
            for a in both
        ],
        worker_apps=[{"worker_id": "w1", "app_ids": both}, {"worker_id": "w2", "app_ids": ["demo_mock"]}],
    )
    _, identities, _, worker_apps = state_seed.validate_core(value, apps, ["w1", "w2"])
    assert sorted(identities["w1"]) == both and sorted(identities["w2"]) == both
    assert worker_apps == {"w1": both, "w2": ["demo_mock"]}
    # The company's binding replaces the model's guess, and the standard bundle is unioned in, so a
    # raw contributions map and ``worker_apps.company_grants`` give the same answer here.
    _, identities, _, worker_apps = state_seed.validate_core(
        value, apps, ["w1", "w2"], contract_apps={"w1": ["other_mock"], "w2": ["other_mock"]}
    )
    assert sorted(identities["w1"]) == both, "the book is not narrowed"
    assert worker_apps == {"w1": ["other_mock"], "w2": ["other_mock"]}


def test_shared_desk_identities_trigger_one_identities_repair(exported):
    """Every worker must be a distinct person; a shared account is repaired, not seeded."""
    root, folder = exported
    shared = [
        {"worker_id": "w1", "app_id": "demo_mock", "user_json": json.dumps(USERS[0])},
        {"worker_id": "w2", "app_id": "demo_mock", "user_json": json.dumps(USERS[0])},
    ]
    models = FakeModels(core(identities=shared), app_state())
    original_call = models.call
    seen = {}

    def call(job, prompt, response_type, **kwargs):
        if response_type is state_seed.IdentitiesOnly:
            seen["repair"] = prompt
            return state_seed.IdentitiesOnly(
                identities=[
                    state_seed.Identity(worker_id="w1", app_id="demo_mock", user_json=json.dumps(USERS[0])),
                    state_seed.Identity(worker_id="w2", app_id="demo_mock", user_json=json.dumps(USERS[1])),
                ]
            ), {"call_id": "idrepair"}
        return original_call(job, prompt, response_type, **kwargs)

    models.call = call
    report = state_seed.seed_world(root, folder, models=models)
    assert "identities_repair" in seen["repair"] and "share id" in seen["repair"]
    assert read(folder / "world" / "identities.json")["w2"]["demo_mock"] == USERS[1]
    assert report["status"] == "seeded_reviewed"


@pytest.mark.parametrize("suffixes", [("1", "2"), ("w1", "w2")])
def test_role_accounts_are_not_distinct_people_by_suffix(suffixes):
    identities = {
        worker: {
            "app": {
                "id": f"desk-{suffix}",
                "name": f"Dispatch Desk {suffix}",
                "email": f"dispatch+{suffix}@example.test",
            }
        }
        for worker, suffix in zip(("w1", "w2"), suffixes, strict=True)
    }
    assert state_seed.distinct_identity_errors(identities, {"app"})


def test_distinct_people_can_have_sequential_user_ids():
    identities = {
        "w1": {"app": {"id": "user-1", "name": "Ann Lee", "email": "ann@example.test"}},
        "w2": {"app": {"id": "user-2", "name": "Bob Lee", "email": "bob@example.test"}},
    }
    assert state_seed.distinct_identity_errors(identities, {"app"}) == []


def test_large_malformed_json_never_enters_model_repair():
    calls = []

    class Model:
        def call(self, *args):
            calls.append(args)
            return state_seed.JsonRepair(fixed_json='{"silently_dropped": true}'), {}

    # Too large for the model repair: never sent there; the truncated text is salvaged locally.
    value = state_seed.parse_json('{"records": "' + "x" * 300_000, "state", models=Model())
    assert calls == []
    assert set(value) == {"records"}


def test_json_repair_has_one_attempt_and_valid_large_json_needs_none():
    calls = []

    class Model:
        def call(self, *args):
            calls.append(args)
            return state_seed.JsonRepair(fixed_json="{broken again"), {}

    value = {"records": "x" * 100_000}
    assert state_seed.parse_json(json.dumps(value), "state", models=Model()) == value
    assert not calls
    with pytest.raises(ValueError, match="even after repair"):
        state_seed.parse_json("{broken", "state", models=Model())
    assert len(calls) == 1


def test_collection_groups_merge_without_repairing_the_full_state(exported):
    root, folder = exported
    state = deepcopy(SEEDED)
    state["tickets"][0]["description"] = "x" * 100_000

    class Groups(FakeModels):
        def call(self, job, prompt, response_type, **kwargs):
            assert response_type is not state_seed.JsonRepair
            if response_type is state_seed.AppStateResult:
                packet = json.loads(prompt.split("\n")[-1])
                self.calls.append((job, prompt, response_type))
                keys = packet["named_collections"]
                assert len(keys) == 1
                return app_state({k: state[k] for k in keys}), {"call_id": keys[0]}
            return super().call(job, prompt, response_type, **kwargs)

    models = Groups(core(), app_state())
    models.config = {"design": {"seed_collections_per_call": 1}}
    state_seed.seed_world(root, folder, models=models, review_rounds=0)
    assert read(folder / "world/demo_mock.state.json") == state


def test_the_app_prompt_carries_the_access_barrier_and_the_apps_own_field_names(exported):
    """Two things the author had to guess at, and a call it no longer has to make in one piece.

    The seeder was never told who cannot log into the app it is writing, so it assigned and
    reported records as workers with no account there: 218 jira records at greenbrier-companies
    (the task's own manager among them), 236 salesforce at federal-home-loan-bank-des-moines, 165
    Expensify at treehouse-foods. And ``catalogs/app_record_fields.json`` -- the field names each
    app's own records carry, 87 apps and 767 collections -- existed without ever reaching a prompt,
    while microsoft_teams was seeded id/name where the app reads userId/displayName and salesforce
    posts carried createdAt where the app reads createdDate. The catalogue also decides the shape
    of the call: a collection it lists is authored on its own.
    """
    root, folder = exported
    write(
        root / "catalogs" / "app_record_fields.json",
        {"apps": {"demo_mock": {"tickets": ["id", "status", "requesterId"], "users": ["id", "name"]}}},
    )
    state_seed.record_field_catalogue.cache_clear()
    apps = read(folder / "apps.json")
    apps["apps"][0]["top_level_keys"] = ["currentUser", "tickets", "ui", "users"]
    write(folder / "apps.json", apps)
    # w2 holds nothing but the standard bundle, which this company does not have: no login here.
    write(
        folder / "tasks" / "acme_o1" / "workflow.json",
        {
            "id": "acme_o1",
            "contributions": [{"worker_id": "w1", "apps": ["demo_mock"]}, {"worker_id": "w2", "apps": []}],
        },
    )
    value = core()  # the identity book covers both workers, as insurance for a widened grant

    class Groups(FakeModels):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is state_seed.AppStateResult:
                packet = json.loads(prompt.split("\n")[-1])
                self.calls.append((job, prompt, response_type))
                state = {k: SEEDED[k] for k in packet["named_collections"]}
                if "users" in state:
                    state["users"] = [USERS[0]]
                return app_state(state), {"call_id": ",".join(packet["named_collections"])}
            return super().call(job, prompt, response_type, **kwargs)

    models = Groups(value, app_state())
    state_seed.seed_world(root, folder, models=models, review_rounds=0)
    packets = [
        json.loads(prompt.split("\n")[-1])
        for _job, prompt, kind in models.calls
        if kind is state_seed.AppStateResult
    ]
    # One call per catalogued collection; the rest still share one. Only the first pass is asserted:
    # a further repair round follows while check_folder still hands check_state_identities every
    # worker who has an identity instead of the app's holders (world_check.py, another agent's
    # file), which asks this author to put a worker with no login back into the directory.
    assert [p["named_collections"] for p in packets[:3]] == [["tickets"], ["users"], ["currentUser", "ui"]]
    assert packets[0]["record_fields"] == {"tickets": ["id", "status", "requesterId"]}
    assert packets[0]["workers_using_this_app"] == ["w1"]
    assert packets[0]["workers_without_access"] == ["w2"]
    assert "cannot log in" in packets[0]["access_rule"]
    # The collection written first is context for the ones written after it.
    assert packets[1]["already_authored_collections"]["tickets"] == SEEDED["tickets"]
    # Only the holder is a person this app is written for, and the rule agrees with the code that
    # follows it: w2 is not in identities_for_this_app, so splice_identities does not put w2 back
    # into the directory and check_state_identities does not demand w2 be there.
    assert list(packets[1]["identities_for_this_app"]) == ["w1"]
    assert read(folder / "world" / "demo_mock.state.json")["users"] == [USERS[0]]
    # The book itself keeps both: a mined task can widen w2's grant without re-seeding the world.
    assert read(folder / "world" / "identities.json") == {
        "w1": {"demo_mock": USERS[0]},
        "w2": {"demo_mock": USERS[1]},
    }
    state_seed.record_field_catalogue.cache_clear()


def test_identity_repair_is_bounded_and_does_not_receive_world(exported):
    root, folder = exported
    shared = [core().identities[0], core().identities[0].model_copy(update={"worker_id": "w2"})]
    value = core(identities=shared, entities_json=json.dumps({"secret": "WORLD-ONLY" * 20_000}))
    repairs = []

    class Model(FakeModels):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is state_seed.IdentitiesOnly:
                repairs.append(prompt)
                assert "WORLD-ONLY" not in prompt
                return state_seed.IdentitiesOnly(identities=shared), {}
            return super().call(job, prompt, response_type, **kwargs)

    with pytest.raises(ValueError, match="not distinct people"):
        state_seed.seed_world(root, folder, models=Model(value, app_state()))
    assert len(repairs) == 1


def test_seed_prompt_lists_names_already_used_by_other_companies(exported):
    root, folder = exported
    other = folder.parent / "other-co" / "world"
    other.mkdir(parents=True)
    other.joinpath("identities.json").write_text(
        json.dumps({"w1": {"demo_mock": {"id": 9, "name": "Taken Person", "email": "taken@x.example"}}})
    )
    models = FakeModels(core(), app_state())
    state_seed.seed_world(root, folder, models=models)
    core_prompt = models.calls[0][1]
    assert "Taken Person" in core_prompt and "taken@x.example" in core_prompt


def test_single_account_app_state_seeds_without_directory_lookup(exported):
    """Gmail-like apps have one user object and no directory; seeding must not index an empty match list."""
    root, folder = exported
    schema = (
        (root / "catalogs" / "app_schemas" / "demo_mock.md")
        .read_text()
        .replace("| `users` | array | Users |", "")
    )
    (root / "catalogs" / "app_schemas" / "demo_mock.md").write_text(schema)
    apps = read(folder / "apps.json")
    apps["apps"][0]["top_level_keys"] = ["currentUser", "tickets", "ui"]
    write(folder / "apps.json", apps)
    single = {"currentUser": USERS[0], "tickets": [{"id": 7}], "ui": {"activeView": 1}}
    report = state_seed.seed_world(root, folder, models=FakeModels(core(), app_state(single)))
    assert report["status"] == "seeded_reviewed"


def test_missing_identity_is_repaired_with_the_identities_only_call(exported):
    root, folder = exported
    full = core()
    broken = full.model_copy(
        update={"identities": [i for i in full.identities if i.worker_id != full.identities[0].worker_id]}
    )

    class Repairing(FakeModels):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is state_seed.IdentitiesOnly:
                self.calls.append((job, prompt, response_type))
                return state_seed.IdentitiesOnly(identities=core().identities), {"model": "codex/gpt-test"}
            return super().call(job, prompt, response_type, **kwargs)

    models = Repairing(broken, app_state())
    report = state_seed.seed_world(root, folder, models=models)
    assert state_seed.IdentitiesOnly in [c[2] for c in models.calls]
    assert report["workers"]


def test_truncated_world_json_is_salvaged_to_its_complete_records():
    full = {
        "staff": [{"id": "s1", "name": "Ana"}, {"id": "s2", "name": "Bo"}],
        "history": [{"at": "2026-09-01", "text": 'Opened the office, "quoted" text'}],
    }
    text = json.dumps(full)
    cut = text[: text.index('"quoted') + 3]  # stops inside a string in the last record
    value = state_seed.salvage_truncated_json(cut)
    assert value["staff"] == full["staff"] and "history" in value
    assert state_seed.salvage_truncated_json("not json at all") is None

    class RepairRefuses:
        def call(self, *args, **kwargs):
            raise ValueError("entities_json: JSON repair input exceeds the 250000-character limit")

    big = json.dumps({"rows": [{"i": i, "note": "x" * 100} for i in range(300)]})
    salvaged = state_seed.parse_json(big[:-40], "entities_json", strict=False, models=RepairRefuses())
    assert len(salvaged["rows"]) == 299


def test_world_review_is_a_majority_of_independent_readings(exported):
    root, folder = exported
    # The panel reads round 0, where its findings can still be repaired, and reads the last round,
    # where it decides the world; a middle round casts one ballot. Three readings say revise, the
    # repair round runs, and the last round's panel accepts.
    models = FakeModels(
        core(), app_state(), reviews=[revise(), revise(), revise(), accept(), accept(), accept()]
    )
    models.config = {"design": {"review_votes": 3}}
    report = state_seed.seed_world(root, folder, models=models, review_rounds=1)
    assert report["review"] == "accept"
    review = read(folder / "world" / "REVIEW.json")
    assert (
        review["rounds"][0]["receipt"]["cast"] == 3 and review["rounds"][0]["verdict"]["verdict"] == "revise"
    )
    assert review["rounds"][0]["receipt"]["distinct_issues"] == 1, "three readings of one slip"
    assert review["rounds"][1]["receipt"]["cast"] == 3 and review["rounds"][1]["receipt"]["accepts"] == 3
    assert len([c for c in models.calls if c[2] is state_seed.WorldReview]) == 6


def test_review_packet_is_bounded_to_the_provider_input_limit():
    big = {
        "gmail_mock": {
            "emails": [{"id": f"e{i}", "body": "x" * 2000} for i in range(600)],
            "labels": [{"id": "l1"}],
        },
        "slack_mock": {
            "messages": {"general": [{"messageId": f"m{i}", "content": "y" * 500} for i in range(400)]}
        },
    }
    bounded = state_seed.bound_states(big, limit=300_000)
    assert len(json.dumps(bounded)) <= 300_000
    assert bounded["gmail_mock"]["labels"] == [{"id": "l1"}]
    tail = bounded["gmail_mock"]["emails"][-1]
    assert "_sampled" in tail and "of 600" in tail["_sampled"]
    assert bounded == state_seed.bound_states(big, limit=300_000)  # deterministic


def test_the_review_packet_never_exceeds_its_budget(exported):
    """A packet over the provider's ceiling gets no verdict, and takes the world with it.

    Review is the last step before any state reaches disk, so a refused packet discards the whole
    generation. Sixty-five went out at or above 1,048,576 characters and every one was refused,
    the largest at 2.7 MB, because the shrink loop gave up at a record count rather than a size.
    """
    from company_envs.world.world_review import _by_bytes

    huge = {f"app{n}": {"rows": [{"id": f"r{i}", "text": "z" * 900} for i in range(500)]} for n in range(6)}
    for limit in (300_000, 50_000, 8_000):
        bounded = state_seed.bound_states(huge, limit=limit)
        assert len(json.dumps(bounded)) <= limit, f"packet still over budget at {limit}"
        assert bounded == state_seed.bound_states(huge, limit=limit)  # deterministic

    # Findings are trimmed by bytes: they average kilobytes each, so a count is not a budget.
    findings = [{"message": "w" * 3_000, "path": f"/p/{i}"} for i in range(200)]
    kept = _by_bytes(findings, 20_000, "findings")
    assert len(json.dumps(kept)) <= 20_000 + 200
    assert "_omitted" in kept[-1] and "of 200" in kept[-1]["_omitted"]
    assert _by_bytes(findings[:2], 1_000_000, "findings") == findings[:2]  # nothing to omit


def test_avoid_list_is_frozen_on_the_first_attempt(exported):
    root, folder = exported
    (folder / "world").mkdir(exist_ok=True)
    (folder / "world" / "avoid.json").write_text(json.dumps({"names": ["Frozen Person"], "emails": []}))
    payload = state_seed.core_payload(root, folder)
    assert payload["avoid_person_names"] == ["Frozen Person"]


FAULTY = {**SEEDED, "tickets": [{"id": 7, "description": "expand these literally for i=1..600"}]}


def test_seed_time_verification_is_the_check_stage_check(exported):
    """What seeding accepts, world-check accepts: one check_folder call, finding for finding."""
    from company_envs.world.world_check import check_folder

    root, folder = exported
    state_seed.seed_world(root, folder, models=FakeModels(core(), app_state()))
    checks = read(folder / "world" / "CHECKS.json")
    assert check_folder(root, folder) == checks
    seed = read(folder / "world" / "SEED.json")
    assert seed["status"] == "seeded_reviewed"
    assert seed["review_verdict"] == "accept" and seed["mechanical_ok"] is True
    assert seed["mechanical_repair_rounds"] == 0
    assert seed["mechanical_checks"] == {"errors": 0, "warnings": checks["warnings"]}


def test_mechanical_faults_after_acceptance_go_back_to_the_author(exported):
    root, folder = exported
    # First authoring and the pre-review repair keep the fault; the post-acceptance round fixes it.
    models = FakeModels(
        core(),
        app_state(FAULTY),
        reviews=[accept()],
        app_values=[app_state(FAULTY), app_state(FAULTY), app_state()],
    )
    report = state_seed.seed_world(root, folder, models=models)
    kinds = [c[2].__name__ for c in models.calls]
    assert kinds == ["WorldCore", "AppStateResult", "AppStateResult", "WorldReview", "AppStateResult"]
    assert '"revision_feedback"' in models.calls[4][1] and "generator rule" in models.calls[4][1]
    seed = read(folder / "world" / "SEED.json")
    assert seed["status"] == "seeded_reviewed" and report["status"] == "seeded_reviewed"
    assert seed["review_verdict"] == "accept" and seed["mechanical_ok"] is True
    assert seed["mechanical_repair_rounds"] == 1
    assert read(folder / "world" / "demo_mock.state.json") == SEEDED


def test_an_unfixed_mechanical_fault_keeps_the_accept_verdict_visible(exported):
    """status folds both results into one word; the two fields say which held."""
    root, folder = exported
    models = FakeModels(core(), app_state(FAULTY), reviews=[accept()])
    report = state_seed.seed_world(root, folder, models=models)
    kinds = [c[2].__name__ for c in models.calls]
    assert kinds == ["WorldCore", "AppStateResult", "AppStateResult", "WorldReview"] + ["AppStateResult"] * 2
    seed = read(folder / "world" / "SEED.json")
    assert seed["status"] == "seeded_review_failed" and seed["review"] == "accept"
    assert seed["review_verdict"] == "accept" and seed["mechanical_ok"] is False
    assert seed["mechanical_repair_rounds"] == 2 and seed["mechanical_checks"]["errors"] >= 1
    assert report["review_verdict"] == "accept" and report["mechanical_ok"] is False
    assert read(folder / "world" / "CHECKS.json")["ok"] is False


def test_mechanical_feedback_routes_pair_findings_to_both_apps_and_skips_world_findings():
    checks = {
        "findings": [
            {"severity": "error", "source": "a~b", "path": "/rows/1", "message": "drift"},
            {"severity": "error", "source": "world", "path": "/x", "message": "world only"},
            {"severity": "warning", "source": "a", "path": "/y", "message": "not an error"},
            {"severity": "error", "source": "c", "path": "/z", "message": "unknown app"},
        ]
    }
    feedback = state_seed.mechanical_feedback(checks, {"a": {}, "b": {}})
    assert set(feedback) == {"a", "b"}
    assert feedback["a"] == [{"target": "a", "severity": "error", "issue": "drift", "evidence": "/rows/1"}]
    assert state_seed.mechanical_feedback({"findings": checks["findings"][1:]}, {"a": {}}) == {}


STAFF_WORLD = {
    "staff": [
        {"id": "w1", "name": "Dana Ortiz", "email": "dana.ortiz@acme.test", "title": "Dispatcher"},
        {"id": "w2", "name": "Luis Prado", "email": "luis.prado@acme.test", "title": "Accountant"},
        {"id": "s3", "name": "Nerissa Pike", "email": "nerissa.pike@acme.test"},
    ],
    "history": [{"id": "h1", "at": "2026-08-01T10:00:00Z", "by": "w1"}],
}


def test_canonical_people_come_from_the_world_and_reconcile_every_identity_shape():
    people = state_seed.canonical_people(STAFF_WORLD, ["w1", "w2", "w3"])
    assert people == {
        "w1": {"name": "Dana Ortiz", "email": "dana.ortiz@acme.test"},
        "w2": {"name": "Luis Prado", "email": "luis.prado@acme.test"},
    }
    nested = {
        "people": [{"worker_id": "w1", "firstName": "Dana", "lastName": "Ortiz", "email": "d@acme.test"}]
    }
    assert state_seed.canonical_people(nested, ["w1"]) == {
        "w1": {"name": "Dana Ortiz", "email": "d@acme.test"}
    }
    # Two workers pointing at one person is ambiguous: neither is reconciled.
    twins = {
        "staff": [{"id": "w1", "name": "Same One", "email": "s@acme.test"}, {"id": "w2", "name": "Same One"}]
    }
    assert state_seed.canonical_people(twins, ["w1", "w2"]) == {}

    identities = {
        "w1": {
            "gmail_mock": {"name": "Elena Marquez", "email": "elena.marquez@acme.test", "avatar": None},
            "slack_mock": {"userId": "U01", "displayName": "Elena", "email": "elena.marquez@acme.test"},
            "contractbook_mock": {
                "id": "cb-elena",
                "firstName": "Elena",
                "lastName": "Marquez",
                "email": "e@x",
            },
            "klaviyo_mock": {
                "id": "acct-w1",
                "user": {"name": "Elena Marquez", "email": "e@x", "role": "Manager"},
            },
        },
        "w2": {"gmail_mock": {"name": "Luis Prado", "email": "luis.prado@acme.test"}},
        "w3": {"gmail_mock": {"name": "Nobody Canonical", "email": "n@acme.test"}},
    }
    assert state_seed.reconcile_identities(identities, people) == {"w1"}
    w1 = identities["w1"]
    assert w1["gmail_mock"] == {"name": "Dana Ortiz", "email": "dana.ortiz@acme.test", "avatar": None}
    assert w1["slack_mock"] == {"userId": "U01", "displayName": "Dana Ortiz", "email": "dana.ortiz@acme.test"}
    assert w1["contractbook_mock"] == {
        "id": "cb-elena",
        "firstName": "Dana",
        "lastName": "Ortiz",
        "email": "dana.ortiz@acme.test",
    }
    assert w1["klaviyo_mock"]["user"] == {
        "name": "Dana Ortiz",
        "email": "dana.ortiz@acme.test",
        "role": "Manager",
    }
    assert identities["w3"]["gmail_mock"]["name"] == "Nobody Canonical"


def test_identities_are_derived_from_the_canonical_staff(exported):
    """jabil / Pennsylvania DGS: the core named one cast in the world's staff and another in the
    identities; app authors copied the staff, the identity gate then failed on every worker."""
    root, folder = exported
    invented = [
        {
            "worker_id": "w1",
            "app_id": "demo_mock",
            "user_json": json.dumps(
                {**USERS[0], "name": "Elena Marquez", "email": "elena.marquez@acme.test"}
            ),
        },
        {
            "worker_id": "w2",
            "app_id": "demo_mock",
            "user_json": json.dumps({**USERS[1], "name": "Daniel Cho", "email": "daniel.cho@acme.test"}),
        },
    ]
    models = FakeModels(core(identities=invented, entities_json=json.dumps(STAFF_WORLD)), app_state())
    report = state_seed.seed_world(root, folder, models=models)
    assert report["status"] == "seeded_reviewed"
    written = read(folder / "world" / "identities.json")
    assert written["w1"]["demo_mock"] == USERS[0] and written["w2"]["demo_mock"] == USERS[1]
    app_prompt = next(p for job, p, t in models.calls if t is state_seed.AppStateResult)
    assert "Dana Ortiz" in app_prompt and "Elena Marquez" not in app_prompt
    assert state_seed.IdentitiesOnly not in [c[2] for c in models.calls]


def test_identities_repair_is_told_the_canonical_people_and_its_answer_is_reconciled(exported):
    root, folder = exported
    shared = [
        {"worker_id": "w1", "app_id": "demo_mock", "user_json": json.dumps(USERS[0])},
        {"worker_id": "w2", "app_id": "demo_mock", "user_json": json.dumps(USERS[0])},
    ]
    models = FakeModels(core(identities=shared, entities_json=json.dumps(STAFF_WORLD)), app_state())
    original_call = models.call
    seen = {}

    def call(job, prompt, response_type, **kwargs):
        if response_type is state_seed.IdentitiesOnly:
            seen["repair"] = json.loads(prompt.split("\n", 1)[1])
            # The repair makes the accounts distinct but, as it did for jabil, invents new people.
            return state_seed.IdentitiesOnly(
                identities=[
                    state_seed.Identity(
                        worker_id="w1",
                        app_id="demo_mock",
                        user_json=json.dumps(
                            {**USERS[0], "name": "Elena Marquez", "email": "elena@acme.test"}
                        ),
                    ),
                    state_seed.Identity(
                        worker_id="w2",
                        app_id="demo_mock",
                        user_json=json.dumps({**USERS[1], "name": "Daniel Cho", "email": "daniel@acme.test"}),
                    ),
                ]
            ), {"call_id": "idrepair"}
        return original_call(job, prompt, response_type, **kwargs)

    models.call = call
    report = state_seed.seed_world(root, folder, models=models)
    assert seen["repair"]["canonical_people"] == state_seed.canonical_people(STAFF_WORLD, ["w1", "w2"])
    assert "canonical_people names" in seen["repair"]["rule"]
    assert "history" not in seen["repair"]  # the people, never the world
    written = read(folder / "world" / "identities.json")
    assert written["w1"]["demo_mock"] == USERS[0] and written["w2"]["demo_mock"] == USERS[1]
    assert report["status"] == "seeded_reviewed"


def test_a_repair_prompt_over_the_ceiling_is_authored_fresh_rather_than_retried_unchanged(exported):
    """A refusal the caller retries unchanged is an infinite loop, not a failure.

    The state being repaired is nearly all of an oversized prompt -- 3.7 MB of 3.9 MB in the worst
    case on disk. PromptTooLarge is raised before dispatch, and because it was caught nowhere 13
    call ids reached 396 attempts that each failed in under a second and none could have
    succeeded. The group is now authored fresh once, and only a prompt that will not fit even
    without the previous state is terminal.
    """
    root, folder = exported
    repaired = {**SEEDED, "ui": {"activeView": 2}}

    class Refuses(FakeModels):
        refused = False

        def call(self, job, prompt, response_type, **kwargs):
            if (
                response_type is state_seed.AppStateResult
                and '"revision_feedback"' in prompt
                and not self.refused
            ):
                self.refused = True
                self.calls.append((job, prompt, response_type))
                raise PromptTooLarge(job, len(prompt))
            return super().call(job, prompt, response_type, **kwargs)

    models = Refuses(
        core(),
        app_state(),
        reviews=[revise(), accept()],
        app_values=[app_state(), app_state(repaired)],
    )
    state_seed.seed_world(root, folder, models=models)
    authored = [p for _job, p, kind in models.calls if kind is state_seed.AppStateResult]
    assert len(authored) == 3, "the first pass, the refused repair, then the group authored fresh"
    assert '"revision_feedback"' in authored[1] and '"previous_state_json": "{' in authored[1]
    # The retry keeps the feedback and drops the state it was repairing.
    assert '"revision_feedback"' in authored[2] and '"previous_state_json": "null"' in authored[2]
    assert len(authored[2]) < len(authored[1])
    assert read(folder / "world" / "REVIEW.json")["final_verdict"] == "accept"


def test_a_patch_replaces_by_id_adds_removes_and_ignores_the_sampling_markers():
    """``apply_patch`` is the merge the repair contract needs, and it is not ``merge_records``.

    A repair used to be handed the collection whole and had to answer with it whole, which is why a
    repair call emits more output than first-pass authoring -- measured over the 31,886 world_states
    attempts on disk, a mean of 12,878 output tokens against 8,447 and a p90 of 42,756 against
    21,945, 72.6 M output tokens and 861 core-hours. Under a patch, the records the answer leaves
    out are the records that are kept, so the merge has to let the patch win (``merge_records`` keeps
    the existing record, because a shard covers a window the collection does not hold yet).
    """
    rows = [{"id": "a", "v": 1}, {"id": "b", "v": 2}, {"id": "c", "v": 3}]
    patched = state_seed.apply_patch(
        rows,
        [
            {"id": "b", "v": 20},
            {"id": "d", "v": 4},
            {"id": "c", "_remove": True},
            {"_sampled": "300 more records not shown of 303"},
        ],
    )
    assert patched == [{"id": "a", "v": 1}, {"id": "b", "v": 20}, {"id": "d", "v": 4}]
    # An answer that echoes back only the sample it was shown corrects those records and keeps the
    # rest. Under the whole-collection contract the same answer deleted them.
    assert state_seed.apply_patch(rows, [rows[0]]) == rows
    # A dict of lists patches per channel; a dict of records patches per key; one record, a view or
    # a scalar is replaced as the answer gives it, so a repair can still drop a key from `ui`.
    assert state_seed.apply_patch({"ch": rows}, {"ch": [{"id": "a", "v": 9}]})["ch"][0]["v"] == 9
    assert state_seed.apply_patch({"a": {"id": "a"}, "b": {"id": "b"}}, {"b": {"id": "b", "v": 1}}) == {
        "a": {"id": "a"},
        "b": {"id": "b", "v": 1},
    }
    assert state_seed.apply_patch({"activeView": 1, "filter": "all"}, {"activeView": 2}) == {"activeView": 2}
    assert state_seed.apply_patch({"a": {"id": "a"}, "b": {"id": "b"}}, {"b": {"_remove": True}}) == {
        "a": {"id": "a"}
    }


def test_a_sampled_repair_state_fits_the_budget_and_still_carries_every_cited_record():
    """Sampling a collection and then asking for cited records back is asking for a guess.

    Measured on the 60 seeded folders: the largest collection of each of the 406 app states totals
    355,872,398 characters, and 84 of them are alone beyond the 1,048,576 the provider accepts. The
    sample brings the 406 to 25,484,614 characters (7.2%) and the count over the ceiling to 0.
    """
    rows = [{"id": f"r-{n}", "body": "x" * 400} for n in range(900)]
    held = {"tickets": rows}
    finding = [
        {
            "target": "demo_mock",
            "severity": "error",
            "issue": "r-880 contradicts the world",
            "evidence": "/tickets: r-5, r-880",
        }
    ]
    cited = state_seed.patch_cited_ids(finding, held)
    assert cited == {"r-5", "r-880"}
    bounded = state_seed.bound_previous("demo_mock", held, cited, limit=40_000)
    assert len(json.dumps(bounded)) <= 40_000 < len(json.dumps(held))
    kept = {r["id"] for r in bounded["tickets"] if "id" in r}
    assert cited <= kept, "a record the finding names must never be sampled away"
    # Deterministic, so a retry of the same repair is the same prompt and hits the call cache.
    assert bounded == state_seed.bound_previous("demo_mock", held, cited, limit=40_000)


def test_a_repair_is_sent_a_sample_and_the_records_its_patch_leaves_out_survive(exported):
    """The repair contract, which is where ``world_states`` output goes: 2,441 repair call ids
    emitted 72.6 M output tokens because re-sending a collection means re-writing it.

    Measured on the worst case on disk, macerich gmail_mock /emails (3,089 emails): the repair
    prompt goes from 4,363,037 characters to 226,958 and the answer the contract demands from
    3,985,945 to 2,943, with all three cited records still in the sample and all 3,089 records, in
    order, still in the merged state.
    """
    root, folder = exported
    many = {**SEEDED, "tickets": [{"id": n} for n in range(1, 11)]}
    patch = {"currentUser": USERS[0], "ui": {"activeView": 2}, "users": USERS, "tickets": [{"id": 4}]}
    models = FakeModels(
        core(),
        app_state(many),
        reviews=[revise(), accept()],
        app_values=[app_state(many), app_state(patch)],
    )
    state_seed.seed_world(root, folder, models=models)
    repair_prompt = models.calls[3][1]
    assert '"previous_state_is_a_sample": true' in repair_prompt
    assert "ONLY the records you change or add" in repair_prompt
    assert "including empty collections" not in repair_prompt  # never both contracts at once
    written = read(folder / "world" / "demo_mock.state.json")
    # One ticket was returned; the other nine were never sent back and are still there.
    assert [t["id"] for t in written["tickets"]] == list(range(1, 11))
    assert written["ui"] == {"activeView": 2}
    # The first pass is not a patch: it has nothing to patch onto.
    assert '"previous_state_is_a_sample"' not in models.calls[1][1]
    assert "including empty collections" in models.calls[1][1]


def test_an_over_ceiling_group_is_refused_once_with_a_receipt_instead_of_retried_for_ever(
    exported, monkeypatch
):
    """A refusal raised past the caller is a refusal the caller retries unchanged.

    ``PromptTooLarge`` is raised before dispatch and was caught nowhere on this path: macerich's
    gmail_mock /emails repair reached 162 attempts that each failed in 0.4 s, and over the whole
    tree 15 world_states call ids ran 213 such attempts, every one of them certain to fail. The
    group is now refused terminally -- one receipt the driver can read, a warning rather than an
    error because no stage can shrink the prompt, and never dispatched again.
    """
    root, folder = exported
    monkeypatch.setattr(state_seed, "PROMPT_CEILING", 10)
    models = FakeModels(core(), app_state())
    report = state_seed.seed_world(root, folder, models=models)
    assert not [c for c in models.calls if c[2] is state_seed.AppStateResult], "nothing was dispatched"
    seed = read(folder / "world" / "SEED.json")
    refusals = [r for r in seed["receipts"]["app_states"]["demo_mock"] if r["status"] == "refused"]
    assert len(refusals) == 1, "one refusal for the group, however many passes ask for it"
    assert "beyond the 10 the provider accepts in one call" in refusals[0]["reason"]
    assert refusals[0]["collections"] == ["currentUser", "tickets", "ui", "users"]
    # A refusal is not a call: the world_core call and the review are, the refused group is not.
    assert report["calls"] == 2
    findings = read(folder / "world" / "CHECKS.json")["findings"]
    refused = [f for f in findings if "provider accepts in one call" in f["message"]]
    assert [f["severity"] for f in refused] == ["warning"], "a defect no stage can repair cannot block"
    # The world still reaches disk, with the documented keys present and empty.
    assert sorted(read(folder / "world" / "demo_mock.state.json")) == [
        "currentUser",
        "tickets",
        "ui",
        "users",
    ]


def test_a_provider_refusal_shrinks_the_repair_once_and_then_stops(exported):
    """Shrink once, then refuse: the two halves of the rule, measured as one sequence.

    The provider's own ceiling can sit below this module's, so the raise still has to be caught.
    The first refusal drops the state being repaired and authors the group fresh (the 3.7 MB of a
    3.9 MB prompt that state was); a second refusal of the same group is terminal, where the old
    code re-raised it into a caller that retried it unchanged.
    """
    root, folder = exported

    class AlwaysRefuses(FakeModels):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is state_seed.AppStateResult and '"revision_feedback"' in prompt:
                self.calls.append((job, prompt, response_type))
                raise PromptTooLarge(job, 2_000_000)
            return super().call(job, prompt, response_type, **kwargs)

    models = AlwaysRefuses(core(), app_state(), reviews=[revise(), accept()])
    state_seed.seed_world(root, folder, models=models)
    authored = [p for _job, p, kind in models.calls if kind is state_seed.AppStateResult]
    assert len(authored) == 3, "the first pass, the refused repair, the refused fresh retry"
    assert '"previous_state_json": "{' in authored[1] and '"previous_state_json": "null"' in authored[2]
    seed = read(folder / "world" / "SEED.json")
    assert [r.get("status") for r in seed["receipts"]["app_states"]["demo_mock"]] == [
        "complete",
        "refused",
    ]
    assert read(folder / "world" / "demo_mock.state.json") == SEEDED, "the first pass is kept"


def test_a_problem_no_stage_can_repair_is_counted_as_a_warning_not_an_error():
    """``with_problems`` used to stamp every author problem an error, and an error on a prompt over
    the provider's ceiling is a gate blocking on a defect the pipeline cannot repair -- a permanent
    stall. Only errors move ``ok``, and ``mechanical_feedback`` sends only errors back, which is
    what stops the refused group from being dispatched again."""
    checks = {"ok": True, "errors": 0, "warnings": 1, "findings": []}
    problems = {
        "demo_mock": [
            state_seed.group_finding("demo_mock", "over the ceiling", "named_collections=['a']", "warning")
        ]
    }
    out = state_seed.with_problems(checks, problems)
    assert out["ok"] is True and out["errors"] == 0 and out["warnings"] == 2
    assert state_seed.mechanical_feedback(out, {"demo_mock": {}}) == {}
    blocking = state_seed.with_problems(
        checks, {"demo_mock": [state_seed.group_finding("demo_mock", "x", "y")]}
    )
    assert blocking["ok"] is False and blocking["errors"] == 1 and blocking["warnings"] == 1


def test_first_pass_authoring_prompts_are_byte_for_byte_what_they_were(exported):
    """The prompt is the call cache's key, so changing it for no reason throws the cache away.

    24,752 first-pass ``world_states`` attempts are cached on disk against 6,529 repairs. The patch
    contract has to change the repair prompt -- that is the point -- but a first-pass prompt must
    stay exactly as it was, or a resumed run re-authors every app it already paid for.
    """
    assert state_seed.WHOLE_CONTRACT == (
        "state_json contains exactly named_collections, including empty collections; "
        "omit all other top-level keys, and never wrap the object in a label of your own. Groups are "
        "merged and validated as one native state, so already_authored_collections is context to "
        "reference and never output. Use canonical IDs for cross-collection references; compact JSON."
    )
    root, folder = exported
    models = FakeModels(core(), app_state())
    state_seed.seed_world(root, folder, models=models)
    first_pass = next(p for _job, p, kind in models.calls if kind is state_seed.AppStateResult)
    assert state_seed.WHOLE_CONTRACT in first_pass
    assert "previous_state_is_a_sample" not in first_pass and "_remove" not in first_pass


def test_a_counted_finding_is_repaired_over_its_population_and_not_its_three_exemplars(exported):
    """The sample-versus-population trap, which the patch contract made worse before it fixed it.

    ``world_check.MAX_EXAMPLES`` is 3 and its comment says the citations exist "so a repair can be
    narrowed": a finding reads "3,350 of 4,960 threads name a root message that does not exist
    (e.g. a, b, c)". Narrowed to the citations, that repair fixes 3 of 3,350 and the count it is
    measured against does not move. Measured over the 66 reachability findings on disk, 62 name a
    population larger than their own exemplars -- 27,649 records against 186 printed ones -- and 11
    of those 62 (22,815 records) sit in layers the old whole-collection contract refused to send at
    all, so narrowing was not the only thing failing them.

    A finding carries ``records`` now: the whole population. One call per batch of it puts every
    named record in front of the author once.
    """
    root, folder = exported
    # No shard calls: this test is about the repair, and a 60-record collection would buy history.
    with (root / "config.toml").open("a") as stream:
        stream.write("\n[design]\nseed_shards = 1\n")
    rows = [{"id": n, "status": "open"} for n in range(1, 61)]
    many = {**SEEDED, "tickets": rows}
    state_seed.seed_world(root, folder, models=FakeModels(core(), app_state(many)), review_rounds=0)

    class Batched(FakeModels):
        """Closes every ticket it is actually shown, which is what a population batch tests."""

        def call(self, job, prompt, response_type, **kwargs):
            payload = json.loads(prompt.rsplit("\n", 1)[1])
            shown = json.loads(payload["previous_state_json"])
            self.calls.append((job, prompt, response_type))
            closed = [
                {**r, "status": "closed"} for r in shown.get("tickets", []) if isinstance(r.get("id"), int)
            ]
            answer = {k: shown.get(k, []) for k in payload["named_collections"]} | {"tickets": closed}
            return app_state(answer), {"model": "codex/gpt-test", "call_id": "repair", "status": "complete"}

    # The finding prints three ids in its prose and names all sixty in ``records``.
    findings = [
        {
            "target": "demo_mock",
            "severity": "error",
            "issue": "60 of 60 tickets are left open (e.g. 1, 2, 3)",
            "evidence": "/tickets",
            "records": [str(n) for n in range(1, 61)],
        }
    ]
    models = Batched(core(), app_state(many))
    instructions, _ = state_seed.load_skill(root)
    # A budget that holds only part of the population, so the batching itself is under test: batch
    # two must add to what batch one repaired rather than replace it.
    state_seed.REPAIR_STATE_BUDGET, budget = 900, state_seed.REPAIR_STATE_BUDGET
    receipts, problems = {}, {}
    author, contract = state_seed.folder_author(
        root, folder, models, instructions, receipts=receipts, problems=problems
    )
    app = next(a for a in contract if a["app_id"] == "demo_mock")
    try:
        _, repaired = author(app, findings, read(folder / "world" / "demo_mock.state.json"))
    finally:
        state_seed.REPAIR_STATE_BUDGET = budget

    payloads = [json.loads(p.rsplit("\n", 1)[1]) for _job, p, _kind in models.calls]
    assert len(payloads) > 1, "the population did not fit one call, so it was sent in batches"
    # Every batch is told that what it holds IS the population, not an example of it.
    assert all(p["records_to_repair"] > 0 for p in payloads)
    assert all("never the extent of it" in p["records_to_repair_rule"] for p in payloads)
    shown = {
        r["id"]
        for p in payloads
        for r in json.loads(p["previous_state_json"]).get("tickets", [])
        if isinstance(r.get("id"), int)
    }
    assert shown == set(range(1, 61)), "every record the finding names was sent, not just three"
    assert [t["status"] for t in repaired["tickets"]] == ["closed"] * 60
    assert len(repaired["tickets"]) == 60, "the patches accumulate; no batch reverts the last"
    assert not problems, "a population the batches covered leaves no shortfall"
    assert all(r.get("population") == 60 for r in receipts["demo_mock"])


def test_a_population_bigger_than_the_batch_cap_says_how_much_it_repaired():
    """A partial population repair must not write the receipt a whole one writes.

    Eight batches cover 53 of the 62 populations on disk outright; the other nine need up to 14, and
    the repair loop's second round takes those because the checker recomputes the population each
    round. What must never happen is the first round reporting completion: that is the "an empty run
    writes the same receipt a real failure writes" mistake with the signs reversed.
    """
    rows = [{"id": f"r-{n}", "body": "x" * 900} for n in range(400)]
    held = {"tickets": rows}
    ids = {r["id"] for r in rows}
    batches, uncovered = state_seed.population_batches(held, ids, budget=20_000, cap=3)
    assert len(batches) == 3 and uncovered > 0
    assert sum(len(b) for b in batches) + uncovered == len(ids), "every named record is accounted for"
    # Deterministic and in collection order, so a re-run sends the same batches and hits the cache.
    assert state_seed.population_batches(held, ids, budget=20_000, cap=3) == (batches, uncovered)
    assert batches[0] <= ids
    # A batch fits what it promises to fit.
    for batch in batches:
        sent = state_seed.bound_previous("demo_mock", held, batch, only_cited=True)
        assert len(json.dumps(sent)) <= 20_000 * 1.1
        assert {r["id"] for r in sent["tickets"] if "id" in r} == batch
        assert any("_sampled" in r for r in sent["tickets"] if isinstance(r, dict))


def test_the_population_field_is_carried_from_the_checker_to_the_author():
    """``records`` is the one field that separates "repair these three" from "repair these 3,350",
    and a checker that does not set it behaves exactly as before."""
    checks = {
        "findings": [
            {
                "severity": "error",
                "source": "demo_mock",
                "path": "/tickets",
                "message": "3 of 9 (e.g. a)",
                "records": ["a", "b", "c"],
            },
            {"severity": "error", "source": "demo_mock", "path": "/ui", "message": "stale view"},
            {
                "severity": "warning",
                "source": "demo_mock",
                "path": "/x",
                "message": "ignored",
                "records": ["z"],
            },
        ]
    }
    feedback = state_seed.mechanical_feedback(checks, {"demo_mock": {}})["demo_mock"]
    assert [f.get("records") for f in feedback] == [["a", "b", "c"], None]
    # Only ids this group actually holds are a population; the rest belong to another group.
    held = {"tickets": [{"id": "a"}, {"id": "b"}]}
    assert state_seed.population_ids(feedback, held) == {"a", "b"}
    assert state_seed.population_ids([{"issue": "x", "evidence": "y"}], held) == set()


def test_a_patch_that_fails_its_checks_is_never_written_as_the_collection(exported):
    """Under the whole-collection contract a faulted answer still *was* the collection, so salvaging
    it kept the best content there was. A patch is a handful of records: salvaging one as the
    collection would write 3 records over the 3,089 it was laid against -- the same data loss the
    ``too_large_to_send`` guard was protecting against, arriving by a different door."""
    root, folder = exported
    with (root / "config.toml").open("a") as stream:
        stream.write("\n[design]\nseed_shards = 1\n")
    rows = [{"id": n} for n in range(1, 31)]
    many = {**SEEDED, "tickets": rows}
    state_seed.seed_world(root, folder, models=FakeModels(core(), app_state(many)), review_rounds=0)

    class WrongKeys(FakeModels):
        """Answers a repair with a patch under a key the group does not have, twice."""

        def call(self, job, prompt, response_type, **kwargs):
            self.calls.append((job, prompt, response_type))
            return app_state({"not_a_collection": [{"id": 1}]}), {
                "model": "codex/gpt-test",
                "call_id": "bad",
                "status": "complete",
            }

    models = WrongKeys(core(), app_state(many))
    instructions, _ = state_seed.load_skill(root)
    problems = {}
    author, contract = state_seed.folder_author(
        root, folder, models, instructions, receipts={}, problems=problems
    )
    app = next(a for a in contract if a["app_id"] == "demo_mock")
    before = read(folder / "world" / "demo_mock.state.json")
    _, after = author(
        app,
        [{"target": "demo_mock", "severity": "error", "issue": "ticket 4 is wrong", "evidence": "/tickets"}],
        before,
    )
    assert "not_a_collection" not in after
    assert after["tickets"] == before["tickets"], "the collection is what it was, not the failed patch"
    assert problems["demo_mock"], "and the failure is recorded, not swallowed"


def test_the_population_id_list_never_reaches_the_prompt():
    """``records`` is for the repair, not for the model, and it is large.

    Measured over the 60 worlds now that four checkers declare populations: the id lists are a mean
    of 35.9 KB per world, a median of 17.8 KB and 162.2 KB at hammond-power-solutions, 2.11 MB
    fleet-wide -- six times the estimate the handover carried. On hammond's own gmail /emails repair
    that is 137,286 characters in every one of 17 batch prompts, 2,333,862 in all, of input the model
    never reads: the batch it is working on is already in previous_state_json. It was not yet a
    refusal there (463,836 against the 1,048,576 ceiling) but it was more than half the margin the
    pre-dispatch check exists to protect.
    """
    finding = {
        "target": "demo_mock",
        "severity": "error",
        "issue": "4,040 of 4,040 emails name a thread that does not exist",
        "evidence": "/emails/0/threadId",
        "records": [f"thr-{n}" for n in range(4_040)],
    }
    plain = {"target": "demo_mock", "severity": "error", "issue": "x", "evidence": "/ui"}
    out = state_seed.feedback_for_prompt([finding, plain])
    assert "records" not in out[0] and out[0]["population_records"] == 4_040
    assert len(json.dumps(out)) < len(json.dumps([finding, plain])) / 10
    # A finding that declares no population serialises byte for byte as it did, so the 24,752 cached
    # first-pass attempts and every cached repair of such a finding stay cached.
    assert out[1] is plain and json.dumps(out[1]) == json.dumps(plain)


def test_a_reference_finding_names_absent_ids_and_resolves_to_the_records_pointing_at_them():
    """``check_references`` declares "the ids that resolve to nothing", which are by definition not
    records: 28,608 of them on disk, the largest declared population in the tree, and under a
    consumer that kept only ids it could find they resolved to **zero** records -- the repair changed
    nothing and recorded that it had.

    The other direction is the trap's mirror image. 2,175 cruise-america drive items under one folder
    that was never written is a missing *container*, not 2,175 broken records, and rewriting them all
    to supply one parent is the expensive repair of a cheap defect. Measured over the 185 reference
    findings whose absent ids have referrers, the ratio is bimodal -- median 1.00, p75 3.00, p90
    286.75, max 2,175 -- so the cut sits in an empty gap: every threshold from 4 to 8 keeps the same
    148 findings and 19,775 records, and drops 37 that would have rewritten 49,416.
    """
    held = {
        "emails": [
            {"id": "m1", "threadId": "gone-1"},
            {"id": "m2", "threadId": "gone-2"},
            {"id": "m3", "threadId": "here"},
        ],
        "threads": [{"id": "here"}],
    }
    dangling = [
        {
            "issue": "2 of 3 threadId values name no record",
            "evidence": "/emails/0/threadId",
            "records": ["gone-1", "gone-2"],
        }
    ]
    assert state_seed.population_ids(dangling, held) == {"m1", "m2"}
    # One absent container that a crowd points at is the container's defect, not the crowd's.
    crowd = {"items": [{"id": f"f{n}", "parentId": "never-written"} for n in range(50)]}
    assert (
        state_seed.population_ids([{"issue": "x", "evidence": "/items", "records": ["never-written"]}], crowd)
        == set()
    )
    # And an id that is neither a record here nor pointed at from here belongs to another group.
    assert (
        state_seed.population_ids([{"issue": "x", "evidence": "/x", "records": ["elsewhere"]}], held) == set()
    )
    # Lists of bare ids count: a container names its children that way.
    lists = {"lists": [{"id": "l1", "cardIds": ["c1", "missing"]}]}
    assert state_seed.population_ids(
        [{"issue": "x", "evidence": "/lists", "records": ["missing"]}], lists
    ) == {"l1"}


def test_a_skipped_shard_writes_a_receipt_instead_of_only_a_warning(exported):
    """A collection that lost its earlier history read exactly like one that never had any: the skip
    was a line on stderr and nothing else. The split it relies on is already the model exception
    hierarchy -- PromptTooLarge and ModelOutputInvalid are ValueErrors and say something about this
    work; ModelUnavailable and CallBudgetExhausted are RuntimeErrors and say something about the
    environment, so they stop the step rather than being recorded against the world."""
    root, folder = exported
    rows = [{"id": n} for n in range(1, 21)]
    many = {**SEEDED, "tickets": rows}

    class ShardFails(FakeModels):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is state_seed.AppStateResult and '"shard"' in prompt:
                self.calls.append((job, prompt, response_type))
                raise PromptTooLarge(job, 2_000_000)
            return super().call(job, prompt, response_type, **kwargs)

    models = ShardFails(core(), app_state(many))
    state_seed.seed_world(root, folder, models=models, review_rounds=0)
    receipts = read(folder / "world" / "SEED.json")["receipts"]["app_states"]["demo_mock"]
    skipped = [r for r in receipts if r.get("status") == "skipped"]
    assert skipped, "the shard that was not bought is in the receipt"
    assert skipped[0]["collections"] == ["tickets"] and "PromptTooLarge" in skipped[0]["reason"]
    # The recent layer is untouched: a shard is extra history, not the collection.
    assert len(read(folder / "world" / "demo_mock.state.json")["tickets"]) == 20


def test_both_writers_of_seed_json_get_their_outcome_from_one_place():
    """SEED.json has two writers -- seed_world and world_repair.review_repair -- and each decided for
    itself what the file said. One mapping serves both, so a status rewritten in one cannot leave the
    other's word behind: a stale marker and a lying receipt are the same defect in different hats.

    The three statuses are `seeded_reviewed`, `seeded_not_verified` (no review was asked for, which is
    a pass *of the seed*; `review_verdict` and `mechanical_ok` are where the review is answered) and
    `seeded_review_failed`, which is a verdict about this world and so a refusal.
    """
    from company_envs.world import world_repair
    from company_envs.world.state_seed import seed_outcome

    assert seed_outcome("seeded_reviewed").outcome == "passed"
    assert seed_outcome("seeded_not_verified").outcome == "passed"
    refused = seed_outcome("seeded_review_failed")
    assert refused.outcome == "refused" and refused.ok is False and "not accepted" in refused.reason
    assert world_repair.seed_outcome is seed_outcome, "one mapping, not a copy in each writer"


def test_a_repair_that_answers_short_has_its_records_put_back(exported):
    """Traced to the call that did it: cort-business-services-corporation's contractbook_mock repair
    was given 540 activities and three findings about ``contracts`` -- a collection outside its group
    -- and answered ``{}``, saying so in its own rationale ("the reported defects require editing
    contracts, which this collection group excludes and previous_state_json does not contain"). The
    author read that as a keys mismatch, retried with the previous state **dropped**, and wrote the 28
    activities the retry invented over the 540 it had. Zero id overlap.

    That is one of 45 isolated collapses across 26 companies and 10,581 records, and it was not the
    ``salvage_group`` path already fixed: replayed over every repair answer on disk, the guard below
    refuses **94** answers and puts back **27,599** records, and the retry that drops the previous
    state authored a group fresh over a collection that already had records **180** times in 56
    companies, replacing 15,408. No output contract can stop a model answering short, so the check is
    at the moment of writing.
    """
    root, folder = exported
    with (root / "config.toml").open("a") as stream:
        stream.write("\n[design]\nseed_shards = 1\n")
    rows = [{"id": n} for n in range(1, 41)]
    many = {**SEEDED, "tickets": rows}
    state_seed.seed_world(root, folder, models=FakeModels(core(), app_state(many)), review_rounds=0)

    class AnswersShort(FakeModels):
        """First the empty object cort's model returned, then a freshly invented short collection."""

        def call(self, job, prompt, response_type, **kwargs):
            self.calls.append((job, prompt, response_type))
            payload = json.loads(prompt.rsplit("\n", 1)[1])
            if len(self.calls) == 1:
                return app_state({}), {"model": "m", "call_id": "empty", "status": "complete"}
            fresh = {k: [] for k in payload["named_collections"]}
            fresh["tickets"] = [{"id": f"invented-{n}"} for n in range(3)]
            return app_state(fresh), {"model": "m", "call_id": "fresh", "status": "complete"}

    models = AnswersShort(core(), app_state(many))
    instructions, _ = state_seed.load_skill(root)
    problems = {}
    author, contract = state_seed.folder_author(
        root, folder, models, instructions, receipts={}, problems=problems
    )
    app = next(a for a in contract if a["app_id"] == "demo_mock")
    before = read(folder / "world" / "demo_mock.state.json")
    _, after = author(
        app,
        [
            {
                "target": "demo_mock",
                "severity": "error",
                "issue": "ticket 7 is wrong",
                "evidence": "/tickets: 7",
            }
        ],
        before,
    )
    # The retry is still shown what it is repairing. Dropping it is what destroyed cort.
    retry = json.loads(models.calls[1][1].rsplit("\n", 1)[1])
    assert retry["previous_state_json"] != "null", "a repair never retries with a clean slate"
    # Every record the collection had is still there. The retry's own invented rows are merged in
    # rather than substituted for the collection, which is the patch contract doing its job: the
    # destructive outcome was 40 records replaced by 3, and what happens now is 40 kept and 3 added
    # for the texture and uniformity checks to judge.
    kept = {t["id"] for t in after["tickets"]}
    assert {t["id"] for t in before["tickets"]} <= kept
    assert len(after["tickets"]) >= len(before["tickets"])
    assert not problems.get("demo_mock"), "nothing collapsed, so nothing to record"


def test_a_collection_is_only_called_collapsed_when_it_was_not_asked_to_shrink():
    """The floor and the allowance together. Measured over the 8,972 collection answers on disk, only
    207 (2.3%) made a collection smaller at all, and the losses are wipes rather than trims: the same
    110 answers breach every floor from 90% down to 50%, and the worst are 761 -> 0, 755 -> 0 and
    592 -> 0 with zero surviving ids. So the floor is not delicate, and the allowance is what keeps a
    genuine narrowed repair from tripping it."""
    before = {"emails": [{"id": f"m{n}"} for n in range(100)], "ui": {"activeView": 1}}
    # A repair that removed the three records it was asked about is not a collapse.
    assert not state_seed.collapsed_collections(before, {"emails": before["emails"][:97]}, 3)
    # Nor is an ordinary edit that keeps most of the collection.
    assert not state_seed.collapsed_collections(before, {"emails": before["emails"][:60]}, 3)
    # A wipe is.
    assert state_seed.collapsed_collections(before, {"emails": []}, 3) == {"emails": (100, 0)}
    # A collection the findings really do name in bulk may shrink that far.
    assert not state_seed.collapsed_collections(before, {"emails": before["emails"][:10]}, 90)
    # Small collections are ordinary editing; a view is not a collection of records at all.
    small = {"tags": [{"id": f"t{n}"} for n in range(8)]}
    assert not state_seed.collapsed_collections(small, {"tags": []}, 0)
    assert not state_seed.collapsed_collections(before, {"ui": {}}, 0)
    # A collection the answer omits entirely is the keys check's business, not this one's.
    assert not state_seed.collapsed_collections(before, {}, 0)


def test_a_group_is_asked_only_what_it_can_answer(exported):
    """cort's group held ``activities, notifications, comments, savedViews`` and was sent three
    findings about ``contracts``. The model answered, correctly, that it could not act on them. Asking
    a group only what it can answer removes the wrong question instead of coping with the right answer
    to it -- and a group with nothing left to answer costs no call at all."""
    keys = ["activities", "contracts", "comments"]
    about_contracts = {"issue": "Most agreements are placeholders", "evidence": "/contracts: L003, L004"}
    about_comments = {"issue": "comments repeat", "evidence": "/comments: c1"}
    unplaced = {"issue": "the world disagrees with itself", "evidence": ""}
    assert state_seed.findings_for_group([about_contracts], ["activities", "comments"], keys) == []
    assert state_seed.findings_for_group([about_contracts], ["contracts"], keys) == [about_contracts]
    # A finding naming no collection could be about anything, so every group still gets it.
    assert state_seed.findings_for_group([unplaced], ["activities"], keys) == [unplaced]
    assert state_seed.findings_for_group([about_contracts, about_comments], ["comments"], keys) == [
        about_comments
    ]

    root, folder = exported
    with (root / "config.toml").open("a") as stream:
        stream.write("\n[design]\nseed_shards = 1\nseed_collections_per_call = 1\n")
    state_seed.seed_world(root, folder, models=FakeModels(core(), app_state()), review_rounds=0)
    models = FakeModels(core(), app_state())
    instructions, _ = state_seed.load_skill(root)
    author, contract = state_seed.folder_author(root, folder, models, instructions, receipts={})
    app = next(a for a in contract if a["app_id"] == "demo_mock")
    state = read(folder / "world" / "demo_mock.state.json")
    _, after = author(
        app,
        [{"target": "demo_mock", "severity": "error", "issue": "tickets drift", "evidence": "/tickets"}],
        state,
    )
    asked = [json.loads(p.rsplit("\n", 1)[1])["named_collections"] for _j, p, _k in models.calls]
    assert asked and all(a == ["tickets"] for a in asked), (
        "only the collection the finding names is called; the other three groups cost no call"
    )
    assert set(after) == set(state)


def test_a_patch_that_deletes_most_of_a_collection_is_refused_at_the_write(exported):
    """With a repair's previous state no longer dropped on retry, the only way an answer can still
    shrink a collection is by asking for it: ``_remove`` on record after record. That is the path the
    write-time guard is left holding, and it is the one a uniformity finding would take -- so it is
    refused once, with the records kept and the refusal recorded, rather than written."""
    root, folder = exported
    with (root / "config.toml").open("a") as stream:
        stream.write("\n[design]\nseed_shards = 1\nseed_collections_per_call = 1\n")
    rows = [{"id": n} for n in range(1, 41)]
    many = {**SEEDED, "tickets": rows}
    state_seed.seed_world(root, folder, models=FakeModels(core(), app_state(many)), review_rounds=0)

    class DeletesEverything(FakeModels):
        def call(self, job, prompt, response_type, **kwargs):
            self.calls.append((job, prompt, response_type))
            payload = json.loads(prompt.rsplit("\n", 1)[1])
            patch = {k: [] for k in payload["named_collections"]}
            if "tickets" in patch:
                patch["tickets"] = [{"id": n, "_remove": True} for n in range(1, 41)]
            return app_state(patch), {"model": "m", "call_id": "wipe", "status": "complete"}

    models = DeletesEverything(core(), app_state(many))
    instructions, _ = state_seed.load_skill(root)
    problems = {}
    author, contract = state_seed.folder_author(
        root, folder, models, instructions, receipts={}, problems=problems
    )
    app = next(a for a in contract if a["app_id"] == "demo_mock")
    before = read(folder / "world" / "demo_mock.state.json")
    _, after = author(
        app,
        [{"target": "demo_mock", "severity": "error", "issue": "tickets repeat", "evidence": "/tickets: 7"}],
        before,
    )
    assert len(after["tickets"]) == 40, "every record it was not asked about is put back"
    assert [t["id"] for t in after["tickets"]] == [t["id"] for t in before["tickets"]]
    recorded = " ".join(p["issue"] for p in problems["demo_mock"])
    assert "kept 0 of 40 tickets" in recorded and "put back" in recorded
    assert [p["severity"] for p in problems["demo_mock"]] == ["error"], "the round that follows asks again"
    # Refused once, retried once: the guard does not spend the whole budget arguing.
    assert len(models.calls) == 2
