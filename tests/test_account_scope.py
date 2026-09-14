import json

from company_envs.world.hub_identity import WorkerAppProxy, visible_to

ME = {"id": "u1", "name": "Jo Park", "email": "jo@acme.test"}
STATE = {
    "user": {"id": "u0", "name": "Desk", "email": "desk@acme.test"},
    "emails": [
        {
            "id": "e1",
            "threadId": "t1",
            "from": {"email": "ana@x.test"},
            "to": [{"email": "jo@acme.test"}],
            "subject": "for Jo",
        },
        {
            "id": "e2",
            "threadId": "t2",
            "from": {"email": "ana@x.test"},
            "to": [{"email": "sam@acme.test"}],
            "subject": "for Sam",
        },
        {
            "id": "e3",
            "from": {"email": "jo@acme.test"},
            "to": [{"email": "sam@acme.test"}],
            "subject": "from Jo",
        },
    ],
    "threads": [{"id": "t1"}, {"id": "t2"}],
    "labels": [{"id": "inbox"}],
    "events": [
        {"id": "ev1", "attendees": [{"email": "jo@acme.test"}]},
        {"id": "ev2", "attendees": [{"email": "sam@acme.test"}]},
        {"id": "ev3", "title": "Office closed"},
    ],
}


def test_visible_to_keeps_only_the_accounts_records_and_shared_ones():
    view = visible_to(STATE, ME)
    assert [e["id"] for e in view["emails"]] == ["e1", "e3"]
    assert [t["id"] for t in view["threads"]] == ["t1"]
    assert [e["id"] for e in view["events"]] == ["ev1", "ev3"]  # no attendee list means shared
    assert view["labels"] == STATE["labels"]


def test_proxy_read_is_scoped_and_write_leaves_hidden_records_untouched(tmp_path):
    proxy = WorkerAppProxy(
        "http://127.0.0.1:1",
        worker_id="jo",
        sid="s",
        identity_key="user",
        user_record=ME,
        canonical_user=STATE["user"],
    )
    served = json.loads(proxy.rewrite_state(json.dumps({"stored_state": STATE}).encode()))["stored_state"]
    assert [e["id"] for e in served["emails"]] == ["e1", "e3"]
    # The served copy carries a picture the browser can draw; everything else is the user record.
    assert {k: v for k, v in served["user"].items() if k != "avatar"} == {
        k: v for k, v in ME.items() if k != "avatar"
    }
    # Jo archives e1 (removes it) and sends a new one; Sam's e2 must survive the write.
    edited = json.loads(json.dumps(served))
    edited["emails"] = [e for e in edited["emails"] if e["id"] != "e1"] + [
        {"id": "e4", "from": {"email": "jo@acme.test"}, "to": [{"email": "ana@x.test"}], "subject": "reply"}
    ]
    written = {}

    def upstream_call(method, path, raw=None, headers=None):
        if method == "GET":
            return 200, json.dumps({"stored_state": STATE}).encode(), {}
        written["state"] = json.loads(raw)["state"]
        return 200, b"{}", {}

    proxy.merge_writes = True
    status, _, _ = proxy.write_state({"state": edited}, "/post?sid=s", {}, upstream_call)
    assert status == 200
    ids = [e["id"] for e in written["state"]["emails"]]
    assert "e2" in ids and "e4" in ids and "e1" not in ids and "e3" in ids
    assert written["state"]["user"] == STATE["user"]  # the canonical user goes back upstream


def test_sys_id_records_merge_per_record_in_replay_and_proxy():
    from company_envs.world.golden import merge_patch
    from company_envs.world.hub_identity import _rebase

    state = {"problems": [{"sys_id": "p1", "state": "open"}, {"sys_id": "p2", "state": "open"}]}
    merged = merge_patch(
        state, {"problems": [{"sys_id": "p2", "state": "closed"}, {"sys_id": "p3", "state": "new"}]}
    )
    assert [(r["sys_id"], r["state"]) for r in merged["problems"]] == [
        ("p1", "open"),
        ("p2", "closed"),
        ("p3", "new"),
    ]
    # Two workers edit different ServiceNow problems; neither write clobbers the other.
    base = state
    current = merge_patch(state, {"problems": [{"sys_id": "p1", "state": "closed"}]})
    incoming = merge_patch(state, {"problems": [{"sys_id": "p2", "state": "closed"}]})
    result = _rebase(base, incoming, current, [])
    assert [(r["sys_id"], r["state"]) for r in result["problems"]] == [("p1", "closed"), ("p2", "closed")]


def test_scalar_settings_can_be_patched_in_replay():
    from company_envs.world.golden import merge_patch

    state = {"activeModule": "incidents", "problems": []}
    assert merge_patch(state, {"activeModule": "problems"})["activeModule"] == "problems"


def test_golden_trajectory_must_fit_the_task_contract():
    from company_envs.world.golden import GoldenStep, contract_problems

    workflow = {
        "contributions": [
            {"worker_id": "boss", "apps": ["slack_mock"]},
            {"worker_id": "dev", "apps": ["github_mock"]},
        ],
        "feature_cell": {"collections": ["github_mock.issues"]},
    }
    boss_in_github = GoldenStep(
        worker_id="boss", app_id="github_mock", action="set_current", state_patch={"issues": [{"id": 1}]}
    )
    dev_in_github = GoldenStep(
        worker_id="dev", app_id="github_mock", action="set_current", state_patch={"issues": [{"id": 1}]}
    )
    boss_in_slack = GoldenStep(
        worker_id="boss", app_id="slack_mock", action="set_current", state_patch={"messages": [{"id": "m1"}]}
    )
    ui_only = GoldenStep(
        worker_id="boss", app_id="slack_mock", action="set_current", state_patch={"view": "inbox"}
    )
    problems = contract_problems([boss_in_github], workflow)
    assert any("does not hold github_mock" in p for p in problems)
    assert any("dev makes no consequential write" in p for p in problems)
    assert contract_problems([dev_in_github, boss_in_slack], workflow) == []
    assert any("boss makes no" in p for p in contract_problems([dev_in_github, ui_only], workflow))
    assert any(
        "decisive collection github_mock.issues" in p
        for p in contract_problems(
            [
                boss_in_slack,
                GoldenStep(
                    worker_id="dev",
                    app_id="github_mock",
                    action="set_current",
                    state_patch={"pulls": [{"id": 2}]},
                ),
            ],
            workflow,
        )
    )
    assert (
        contract_problems([boss_in_github], {"contributions": [{"worker_id": "boss"}]}) == []
    )  # no contract, no rule


def test_a_worker_can_read_back_a_record_it_wrote_for_someone_else():
    """Scoping answers "may I read my colleague's mail", not "may I see what I just did".

    The secretary booked seven visits for the nurse and the physician. She is neither organiser
    nor attendee on any of them, so her own reads came back without them. She spent the end of her
    budget reporting a calendar persistence failure that had not happened, and the delivered run's
    handoff asserts a defect its own grade contradicts.
    """
    from company_envs.world.hub_identity import WorkerAppProxy, visible_to

    tessa = {"id": "hlh-tessa", "email": "tessa@harborlight.test", "name": "Tessa Whitfield"}
    state = {
        "events": [
            {"id": "svc-S201", "title": "Nursing visit", "attendees": [{"email": "maribel@h.test"}]},
            {"id": "own-1", "title": "Her own review", "attendees": [{"email": "tessa@harborlight.test"}]},
        ]
    }
    hidden = [e["id"] for e in visible_to(state, tessa)["events"]]
    assert hidden == ["own-1"], "a colleague's booking is not hers to read by default"

    seen = [e["id"] for e in visible_to(state, tessa, keep={"svc-S201"})["events"]]
    assert seen == ["svc-S201", "own-1"], "but a record she wrote is"

    # The proxy collects those ids from its own successful saves.
    proxy = WorkerAppProxy.__new__(WorkerAppProxy)
    proxy.authored = set()
    proxy.remember_authored(state, {"events": [state["events"][1]]})
    assert proxy.authored == {"svc-S201"}


def test_records_with_an_empty_party_list_stay_visible_to_everyone():
    from company_envs.world.hub_identity import visible_to

    state = {
        "events": [
            {"id": "svc-1", "title": "Shared reservation", "guests": [], "status": "confirmed"},
            {"id": "mine", "title": "Mine", "guests": [{"email": "ann@x.test"}]},
            {"id": "theirs", "title": "Theirs", "guests": [{"email": "bob@x.test"}]},
        ]
    }
    view = visible_to(state, {"email": "ann@x.test", "id": "u-ann"})
    assert [e["id"] for e in view["events"]] == ["svc-1", "mine"]


def test_the_served_state_gets_pictures_the_browser_can_draw():
    """The unit that fills them is useless unless the proxy actually calls it on the way out."""
    from company_envs.world.hub_identity import WorkerAppProxy

    state = {
        "users": [{"id": "u1", "name": "Tessa Whitfield", "avatar": ""}],
        "currentUser": {"id": "u1", "name": "Tessa Whitfield", "avatar": ""},
    }
    body = json.dumps({"has_custom_state": True, "stored_state": state}).encode()
    proxy = WorkerAppProxy.__new__(WorkerAppProxy)
    proxy.identity_key = "currentUser"
    proxy.user_record = {"id": "u1", "name": "Tessa Whitfield", "avatar": ""}
    proxy.placeholder_images = True
    proxy.base = None
    served = json.loads(proxy.rewrite_state(body))["stored_state"]
    assert served["currentUser"]["avatar"].startswith("/__image/")
    assert served["users"][0]["avatar"].startswith("/__image/")
    assert proxy.base["currentUser"]["avatar"] == "", "the stored shape is what a save is judged on"

    proxy.placeholder_images = False
    plain = json.loads(proxy.rewrite_state(body))["stored_state"]
    assert plain["currentUser"]["avatar"] == ""


def test_a_save_never_writes_a_placeholder_picture_into_the_shared_store():
    """The app is served pictures it can draw; the store keeps what the world actually says."""
    from company_envs.world.hub_identity import WorkerAppProxy

    original = {"users": [{"id": "u1", "name": "Tessa Whitfield", "avatar": ""}]}
    proxy = WorkerAppProxy.__new__(WorkerAppProxy)
    proxy.identity_key = None
    proxy.placeholder_images = True
    proxy.base = None
    proxy.merge_writes = False
    proxy.attribution_log = None
    served = json.loads(proxy.rewrite_state(json.dumps({"stored_state": original}).encode()))["stored_state"]
    assert served["users"][0]["avatar"].startswith("/__image/")

    sent = {}

    def upstream_call(method, path, raw=None, headers=None):
        sent["state"] = json.loads(raw)["state"]
        return 200, b"{}", {}

    proxy.write_state({"state": served}, "/post?sid=s", {}, upstream_call)
    assert sent["state"] == original, "the seeded value comes back untouched"


POINTER_STATE = {
    "currentUserId": "user-1",
    "users": [
        {"id": "user-1", "name": "Sarah Chen", "email": "sarah@firm.test"},
        {"id": "user-7", "name": "Godfrey Delacroix", "email": "godfrey@firm.test"},
    ],
    "tasks": [{"id": "t1", "name": "File the motion"}],
}


def test_an_identity_that_is_a_pointer_signs_each_worker_in_as_itself():
    """clio, linear, monday and xiaohongshu name the signed-in person by id, not by record.

    ``identity_key`` listed object keys only, so it returned None for those four apps,
    ``hub_world`` built every worker's proxy with ``user_record=None`` and no injection at all, and
    all five workers of a company shared the seed's one signed-in person -- the direct cause of one
    company's blocking visual verdict ("shows Audrey instead of Godfrey Delacroix").
    """
    from company_envs.world.hub_identity import identity_key, pointer_identity

    schema = "## State Schema\n| `currentUserId` | string | id |\n| `users` | array | Staff |\n"
    assert identity_key(schema) == "currentUserId"
    godfrey = POINTER_STATE["users"][1]
    assert pointer_identity(POINTER_STATE, "currentUserId", godfrey) == "user-7"

    proxy = WorkerAppProxy(
        "http://127.0.0.1:1",
        worker_id="godfrey",
        sid="s",
        identity_key="currentUserId",
        user_record=godfrey,
        user_pointer="user-7",
        canonical_user="user-1",
    )
    served = json.loads(proxy.rewrite_state(json.dumps({"stored_state": POINTER_STATE}).encode()))
    # A string where the app reads a string, and this worker's own id rather than the seed's.
    assert served["stored_state"]["currentUserId"] == "user-7"


def test_a_pointer_identity_refuses_to_serve_a_record_where_the_app_reads_an_id():
    import pytest

    with pytest.raises(ValueError, match="pointer identity key"):
        WorkerAppProxy(
            "http://127.0.0.1:1",
            worker_id="godfrey",
            sid="s",
            identity_key="currentUserId",
            user_record=POINTER_STATE["users"][1],
            canonical_user="user-1",
        )


def test_a_pointer_identity_is_restored_to_the_seeds_id_on_a_save():
    """The shared store keeps the canonical signed-in person, whoever saved."""
    proxy = WorkerAppProxy.__new__(WorkerAppProxy)
    proxy.identity_key, proxy.canonical_user = "currentUserId", "user-1"
    proxy.user_record, proxy.user_pointer = POINTER_STATE["users"][1], "user-7"
    proxy.merge_writes, proxy.placeholder_images, proxy.attribution_log = False, False, None
    proxy.base = None
    calls = []

    def upstream_call(method, path, raw=None, headers=None):
        calls.append(json.loads(raw) if raw else None)
        return 200, json.dumps({"state": {}}).encode(), {}

    body = {"action": "set_current", "state": {**POINTER_STATE, "currentUserId": "user-7"}}
    proxy.write_state(body, "/post?sid=s", {}, upstream_call)
    assert calls[-1]["state"]["currentUserId"] == "user-1"


def test_a_proxy_refusal_names_itself_so_a_browser_fault_check_can_tell_them_apart():
    """Every refusal the proxy can serve carries a machine-readable class.

    31 of the 98 clones on disk POST ``action: "set"`` on load -- 29 of the 90 in the catalogue --
    and ``set`` is the *seeding* action: passing
    it upstream would redefine the session's initial state, which is the grader's zero point. The
    refusal was right and its 403 was indistinguishable from an app request that genuinely broke, so
    a guest-side "no browser faults" check reported a fault for all 31 apps and therefore said
    nothing. (The load-time ``set`` no longer reaches a refusal at all; see
    test_hub_concurrency.)
    """
    from company_envs.world.hub_app import worker_allowed
    from company_envs.world.hub_identity import REFUSED_HEADER, refusal

    # The underlying policy still refuses a raw worker seed; only the proxy's rewrite admits content.
    assert not worker_allowed("POST", "/post", {"action": "set", "state": {}})
    assert worker_allowed("POST", "/post", {"action": "set_current", "state": {}})
    assert REFUSED_HEADER == "X-Company-Envs-Refused"
    assert refusal({"a": 1}, {})["refused"] == "dropped_keys"
    tickets = {"tickets": [{"id": i} for i in range(8)]}
    assert refusal(tickets, {"tickets": []})["refused"] == "mass_removal"
    assert refusal(tickets, {"tickets": [{"id": 99}]})["refused"] == "demo_state"
    assert refusal(tickets, tickets) is None
