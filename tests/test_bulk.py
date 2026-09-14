import json

import pytest

from company_envs.models import ModelOutputInvalid
from company_envs.storage import read, write
from company_envs.world import bulk
from company_envs.world.bulk import BulkSpec, BulkSpecs, add_to_state, expand, people_of
from company_envs.world.seed_calls import AppStateResult

PEOPLE = [{"id": "u1", "name": "Jo Park", "email": "jo@acme.test"}]


def spec(**over):
    base = dict(  # noqa: C408 -- keyword form keeps the fixture readable
        app_id="gmail_mock",
        collection="emails",
        what="monthly statements",
        count=6,
        start="2025-09-01",
        end="2026-08-31",
        cadence="monthly",
        id_prefix="stmt-",
        tables={"accounts": [{"name": "Harbor Supply", "code": "HS"}, {"name": "Birch", "code": "BI"}]},
        template={
            "id": "{{id:stmt-}}",
            "subject": "Statement {{date}} for {{pick:accounts.name}} ({{pick:accounts.code}})",
            "to": [{"name": "{{person}}", "email": "{{person.email}}"}],
            "amount": "{{amount:200-900}}",
            "body": "Balance ${{amount:200-900}} as of {{date}}, due {{date+30}}. Ref {{seq:4}}.",
            "timestamp": "{{datetime}}",
        },
    )
    base.update(over)
    base["template_json"] = json.dumps(base.pop("template"))
    base["tables_json"] = json.dumps(base.pop("tables"))
    return BulkSpec(**base)


def test_expand_is_deterministic_coherent_and_in_window():
    a, b = expand(spec(), PEOPLE, seed=7), expand(spec(), PEOPLE, seed=7)
    assert a == b and len(a) == 6
    first = a[0]
    assert first["id"] == "stmt-1" and first["to"][0]["email"] == "jo@acme.test"
    assert f"${first['amount']}" in first["body"]  # same number quoted in the body
    assert "{{" not in json.dumps(first)
    assert all("2025-09-01" <= r["timestamp"][:10] <= "2026-08-31" for r in a)
    assert (
        "Harbor Supply (HS)" in first["subject"] or "Birch (BI)" in first["subject"]
    )  # one row, both fields
    assert [r["timestamp"] for r in a] == sorted(r["timestamp"] for r in a)


def test_add_to_state_respects_shapes_and_budget():
    records = expand(spec(count=10), PEOPLE, seed=1)
    state = {"emails": [{"id": "e1"}]}
    assert add_to_state(state, spec(), records, max_bytes=10_000_000) == 10 and len(state["emails"]) == 11
    keyed = {"messages": {"general": []}}
    assert (
        add_to_state(keyed, spec(collection="messages"), records[:3], max_bytes=10_000_000) == 3
        and len(keyed["messages"]["general"]) == 3
    )
    docs = {"documents": {}}
    assert add_to_state(docs, spec(collection="documents"), records[:2], max_bytes=10_000_000) == 2 and set(
        docs["documents"]
    ) == {"stmt-1", "stmt-2"}
    tight = {"emails": []}
    assert add_to_state(tight, spec(), records, max_bytes=len(json.dumps(records[0])) * 3) <= 3
    assert (
        people_of({"users": [{"id": "u1", "fullName": "Jo Park", "email": "jo@x"}]})[0]["name"] == "Jo Park"
    )


EMAIL = {
    "id": "e1",
    "from": {"name": "Ana", "email": "ana@x.test"},
    "to": [{"name": "Jo Park", "email": "jo@acme.test"}],
    "subject": "Hi",
    "body": "Hello Jo, quick question about the July invoice. Ana",
    "timestamp": "2026-09-01T09:00:00",
}
# One-line bodies: the check stage's stub rule fires once these outnumber the human emails.
STUB_TEMPLATE = {
    "id": "{{id:stmt-}}",
    "from": {"name": "Billing", "email": "billing@harbor.test"},
    "to": [{"name": "{{person}}", "email": "{{person.email}}"}],
    "subject": "Statement {{date}} {{pick:accounts.name}}",
    "body": "Balance ${{amount:100-300}} due {{date+30}}.",
    "timestamp": "{{datetime}}",
}
FULL_TEMPLATE = {
    **STUB_TEMPLATE,
    "body": (
        "Balance ${{amount:100-300}} is due {{date+30}}. This statement lists the deliveries of the "
        "month, the credits applied since the last statement and the open items still awaiting a "
        "purchase order number; reply to billing with any question about a line."
    ),
}


def _seeded_company(tmp_path, emails):
    """A seeded one-app company folder the way seed-world leaves it, ready for add-bulk."""
    root = tmp_path
    (root / ".agents/skills/company-world-states").mkdir(parents=True)
    (root / ".agents/skills/company-world-states/SKILL.md").write_text(
        "---\nname: company-world-states\n---\nSeed.\n"
    )
    (root / "config.toml").write_text(
        "[design]\nbulk_max_bytes_per_app = 400000\nseed_shards = 1\n[generation]\nseed = 3\n[models]\n"
    )
    (root / "schemas").mkdir()
    (root / "schemas/gmail_mock.md").write_text(
        "## State Schema\n| Key | Type | Description |\n|---|---|---|\n| `emails` | array | All emails |\n| `users` | array | Users |\n"
    )
    folder = root / "companies/acme"
    (folder / "world/materials").mkdir(parents=True)
    (folder / "company.json").write_text(
        json.dumps(
            {"id": "acme", "name": "Acme", "workers": [{"id": "w1", "name": "Jo Park", "title": "Manager"}]}
        )
    )
    (folder / "apps.json").write_text(
        json.dumps(
            {
                "workers": ["w1"],
                "apps": [
                    {
                        "app_id": "gmail_mock",
                        "hub_seedable": True,
                        "schema": "schemas/gmail_mock.md",
                        "top_level_keys": ["emails", "users"],
                    }
                ],
            }
        )
    )
    (folder / "MANIFEST.json").write_text(json.dumps({"hashes": {}}))
    (folder / "world/SEED.json").write_text(
        json.dumps(
            {"status": "seeded_reviewed", "reference_date": "2026-09-08", "operating_scope": "one desk"}
        )
    )
    (folder / "world/world.json").write_text(
        json.dumps({"vendors": [{"id": "v1", "name": "Harbor Supply", "apps": ["gmail_mock"]}]})
    )
    (folder / "world/identities.json").write_text(
        json.dumps({"w1": {"gmail_mock": {"id": "u1", "name": "Jo Park", "email": "jo@acme.test"}}})
    )
    (folder / "world/worker_apps.json").write_text(json.dumps({"w1": ["gmail_mock"]}))
    (folder / "world/gmail_mock.state.json").write_text(
        json.dumps({"users": [{"id": "u1", "name": "Jo Park", "email": "jo@acme.test"}], "emails": emails})
    )
    return root, folder


class Models:
    """Answers the bulk_specs call with one spec and, when asked, repairs the app state."""

    def __init__(self, template, repaired=None, app_id="gmail_mock"):
        self.template, self.repaired, self.app_id, self.payloads = template, repaired, app_id, []

    def call(self, job, prompt, response_type):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        self.payloads.append(payload)
        if response_type is BulkSpecs:
            assert payload["call"] == "bulk_specs" and "placeholders" in payload
            return BulkSpecs(rationale="statements", specs=[spec(count=12, template=self.template)]), {
                "model": "fake"
            }
        assert response_type is AppStateResult and payload["call"] == "app_state"
        previous = json.loads(payload["previous_state_json"])
        fixed = self.repaired(previous) if self.repaired else previous
        return AppStateResult(app_id=self.app_id, rationale="repaired", state_json=json.dumps(fixed)), {
            "model": "fake",
            "call_id": "repair",
        }


def test_add_bulk_expands_specs_into_the_seeded_state(tmp_path):
    from company_envs.world.bulk_layer import add_bulk

    root, folder = _seeded_company(tmp_path, [EMAIL])
    models = Models(STUB_TEMPLATE)
    report = add_bulk(root, folder, models=models)
    assert report["added"] == {"gmail_mock": {"emails": 12}}
    state = read(folder / "world/gmail_mock.state.json")
    assert len(state["emails"]) == 13 and state["emails"][-1]["id"] == "stmt-12"
    bulk = read(folder / "world/BULK.json")
    assert bulk["apps"]["gmail_mock"]["added"] == {"emails": 12}
    # The stub bodies fail the check, but the human layer alone is clean: that fault is the
    # spec's, so no author round is spent on it and the check stage reports it as before.
    assert bulk["mechanical"]["errors"] == 1 and bulk["mechanical_repair_rounds"] == 0
    assert [p["call"] for p in models.payloads] == ["bulk_specs"]
    assert add_bulk(root, folder, models=Models(STUB_TEMPLATE))["skipped"]


def test_add_bulk_spends_each_app_its_own_byte_budget(tmp_path):
    """A per-app budget must reach ``add_to_state``, not merely resolve in isolation.

    One number for the world cannot serve this fleet: slack_mock rendered, wrote and read back at
    12.28 MB while google_docs_mock replaced a company's 1,947 documents with its own five demo
    documents at 5.36 MB (experiments/VOLUME-CEILINGS.md). ``design.bulk_max_bytes_by_app`` is what
    separates them, and a map the layer never reads would look exactly like a map that works. Here
    the default alone refuses every record and the app's own entry lays all twelve.
    """
    from company_envs.world.bulk_layer import add_bulk

    def config(extra=""):
        (root / "config.toml").write_text(
            f"[design]\nbulk_max_bytes_per_app = 900\nseed_shards = 1\n{extra}"
            "[generation]\nseed = 3\n[models]\n"
        )

    root, folder = _seeded_company(tmp_path, [EMAIL])
    config()
    add_bulk(root, folder, models=Models(STUB_TEMPLATE))
    refused = read(folder / "world/BULK.json")["apps"]["gmail_mock"]
    assert "storage budget" in refused["skipped"] and refused["added"] == {}, "900 bytes holds nothing"

    config("[design.bulk_max_bytes_by_app]\ngmail_mock = 400000\n")
    report = add_bulk(root, folder, models=Models(STUB_TEMPLATE), again=True)
    entry = read(folder / "world/BULK.json")["apps"]["gmail_mock"]
    assert report["added"] == {"gmail_mock": {"emails": 12}}
    assert entry["max_bytes"] == 400000, "the app's own entry, not the 900 default"
    # The reason travels with the budget, because storage and the clock have different remedies:
    # gmail is storage-clean to 12.07 MB and capped by page render time against a 25-minute episode.
    assert "clock" in entry["max_bytes_reason"]


def test_add_bulk_repairs_a_human_layer_fault_and_lays_the_bulk_again(tmp_path):
    from company_envs.world.bulk_layer import add_bulk

    faulty = {**EMAIL, "id": "e2", "body": "expand these literally for i=1..600 to fill the inbox"}
    root, folder = _seeded_company(tmp_path, [EMAIL, faulty])

    def repaired(previous):
        # The author sees the human layer only, never the generated records.
        assert [e["id"] for e in previous["emails"]] == ["e1", "e2"]
        return {
            **previous,
            "emails": [EMAIL, {**faulty, "body": "Hello Jo, the July invoice is attached. Ana"}],
        }

    models = Models(FULL_TEMPLATE, repaired)
    report = add_bulk(root, folder, models=models)
    assert report["mechanical_repair_rounds"] == 1 and report["mechanical"] == 0
    assert [p["call"] for p in models.payloads] == ["bulk_specs", "app_state"]
    feedback = models.payloads[1]["revision_feedback"]
    assert feedback[0]["target"] == "gmail_mock" and "generator rule" in feedback[0]["issue"]
    assert feedback[0]["evidence"] == "/emails/1/body"
    state = read(folder / "world/gmail_mock.state.json")
    assert [e["id"] for e in state["emails"]] == ["e1", "e2", *(f"stmt-{i}" for i in range(1, 13))]
    assert "expand these" not in json.dumps(state)
    bulk = read(folder / "world/BULK.json")
    assert bulk["mechanical_repair_rounds"] == 1 and bulk["mechanical"]["errors"] == 0
    assert len(bulk["apps"]["gmail_mock"]["ids"]) == 12 and bulk["apps"]["gmail_mock"]["added"] == {
        "emails": 12
    }
    assert bulk["mechanical_repair_receipts"]["gmail_mock"][0]["call_id"] == "repair"
    assert read(folder / "world/CHECKS.json")["ok"] is True


def test_add_bulk_records_a_failed_repair_and_keeps_the_layered_states(tmp_path):
    from company_envs.world.bulk_layer import add_bulk

    faulty = {**EMAIL, "id": "e2", "body": "expand these literally for i=1..600 to fill the inbox"}
    root, folder = _seeded_company(tmp_path, [EMAIL, faulty])
    report = add_bulk(root, folder, models=Models(FULL_TEMPLATE, app_id="other_mock"))
    assert report["mechanical_repair_rounds"] == 0 and report["mechanical"] == 1
    bulk = read(folder / "world/BULK.json")
    assert "returned other_mock" in bulk["mechanical_repair_error"] and bulk["mechanical_repair_rounds"] == 0
    assert len(read(folder / "world/gmail_mock.state.json")["emails"]) == 14
    assert read(folder / "world/CHECKS.json")["ok"] is False
    # The layer is laid and validated, so the marker's outcome is the layer's and not the optional
    # repair's: folding the repair's fault in would make the marker stale and buy the whole world's
    # bulk again -- hours of model calls for a step this stage is allowed to skip.
    from company_envs.world.bulk_layer import current

    assert bulk["outcome"] == "passed" and current(bulk) is True


def test_a_record_is_not_a_duplicate_of_what_it_points_at():
    """A generated record carries foreign keys, and those must not read as its identity.

    Dedupe compared every id-shaped field, so a Slack message naming an existing channel, sender
    or thread matched a record already in the state and was skipped. The first insertion seeded
    the match and everything after it was refused: Slack kept 3 of 273,453 declared records
    fleet-wide, the calendar 3 of 124,265, while gmail and drive, whose records carry no such
    keys, kept everything.
    """
    from company_envs.world.bulk import add_to_state, own_identifiers

    keyed = spec(count=4, collection="messages", keyed_by="channelId", id_prefix="bulk-msg-")
    record = {"messageId": "bulk-msg-1", "channelId": "general", "senderId": "u-ana"}
    owned = own_identifiers(record, keyed)
    assert owned == {"bulk-msg-1"}, "the bucket and the sender are references, not identity"

    state = {"messages": {"general": [{"messageId": "seed-1", "channelId": "general", "senderId": "u-ana"}]}}
    fresh = [{"messageId": f"bulk-msg-{i}", "channelId": "general", "senderId": "u-ana"} for i in range(1, 5)]
    assert add_to_state(state, keyed, fresh, 10_000_000) == 4
    assert add_to_state(state, keyed, fresh, 10_000_000) == 0, "a rerun still adds nothing twice"


def test_records_the_byte_budget_refused_are_reported_not_swallowed():
    """A spec asks for a number and the budget decides; the difference has to be visible.

    Fleet-wide well under two thirds of declared bulk records reached disk, and two companies
    declared thousands and wrote none, because only the inserted count was ever recorded.
    """
    from company_envs.world.bulk_layer import _volume, lay_bulk

    roomy = {"emails": [], "users": PEOPLE}
    added, _ids, short = lay_bulk(roomy, [spec(count=5)], 3, 10_000_000)
    assert added == {"emails": 5} and short == []

    cramped = {"emails": [], "users": PEOPLE}
    _added, _ids, short = lay_bulk(cramped, [spec(count=5)], 3, len(json.dumps(cramped)) + 200)
    assert short and short[0]["collection"] == "emails"
    assert short[0]["written"] < short[0]["asked"] == 5

    whole = _volume({"gmail_mock": {"specs": [{"count": 5}], "added": {"emails": 1}, "short": short}})
    assert whole["asked"] == 5 and whole["written"] == 1 and whole["complete"] is False
    assert "gmail_mock" in whole["short_by_app"]
    assert _volume({"gmail_mock": {"specs": [{"count": 5}], "added": {"emails": 5}}})["complete"] is True


@pytest.mark.parametrize("shape", ["list", "keyed", "map"])
def test_lay_bulk_is_repeatable_over_a_fresh_human_layer(shape):
    from company_envs.world.bulk_layer import lay_bulk

    empty = {"list": [], "keyed": {"general": []}, "map": {}}[shape]
    a = {"emails": json.loads(json.dumps(empty)), "users": PEOPLE}
    b = {"emails": json.loads(json.dumps(empty)), "users": PEOPLE}
    added_a, ids_a, _ = lay_bulk(a, [spec(count=5)], 3, 10_000_000)
    added_b, ids_b, _ = lay_bulk(b, [spec(count=5)], 3, 10_000_000)
    assert added_a == added_b == {"emails": 5} and ids_a == ids_b and a == b


def test_a_spec_is_refused_for_the_tells_it_would_stamp_on_every_record():
    """One template writes every record it expands, so its habits are multiplied by the count.

    Of the 1,842 specs on disk, 371 of 400 Gmail specs put an em dash in the subject (77.6% of
    all Gmail subjects), 283 stamped a sentence denying what the record means onto 60,025
    records, 75% of calendar templates restated "Organizer: ..." in a description the app draws
    that field for, and 846 addressed their records to nobody who works at the company, leaving
    19,140 records no worker can open. ``check_specs`` is the last point where anything can ask
    for a better template: after the records are laid the repair path goes to the app author,
    who did not write them.
    """
    from company_envs.world.bulk_layer import check_specs

    app = {"app_id": "gmail_mock"}
    state = {"users": [{"id": "u1", "name": "Jo Park", "email": "jo@acme.test"}], "emails": [EMAIL]}
    tells = spec(
        template={
            "id": "{{id:rem-}}",
            "from": {"name": "Billing", "email": "billing@birch.test"},
            "to": [{"name": "Accounts", "email": "accounts@birch.test"}],
            "subject": "Time-entry reminder — {{date}}",
            "body": "Week of {{date}} is open. This reminder carries no new case assignments. "
            "Organizer: Nerys Quill.",
            "timestamp": "{{datetime}}",
        }
    )
    problems = " | ".join(check_specs([tells], app, state, ["emails"], 400_000))
    assert "em dash" in problems, problems
    assert "never denies what it is not" in problems
    assert "restates organizer" in problems
    assert "names nobody who works here" in problems

    written = spec(
        template={
            "id": "{{id:rem-}}",
            "from": {"name": "Billing", "email": "billing@birch.test"},
            "to": [{"name": "{{person}}", "email": "{{person.email}}"}],
            "subject": "Time entry due for the week of {{date}}",
            "body": "Your hours for the week of {{date}} are still open; Nerys needs them before "
            "the run on {{date+3}}.",
            "timestamp": "{{datetime}}",
        }
    )
    assert check_specs([written], app, state, ["emails"], 400_000) == []


def test_an_addressed_template_is_accepted_and_a_party_less_one_is_not_judged():
    """A record with no party field at all stays visible to everyone; only an addressed one hides.

    ``visible_to`` keeps a record whose scope fields are all empty -- a public holiday, a
    company-wide notice -- so a calendar spec that invites nobody is fine, while one that invites
    only outsiders expands into events no worker can see.
    """
    from company_envs.world.bulk_layer import check_addressing_template

    state = {"users": [{"id": "u1", "name": "Jo Park", "email": "jo@acme.test"}]}
    outsiders = spec(collection="events", template={"id": "{{id:ev-}}", "guests": ["ana@birch.test"]})
    assert check_addressing_template(outsiders, outsiders.template, state)
    holiday = spec(collection="events", template={"id": "{{id:ev-}}", "title": "Office closed"})
    assert check_addressing_template(holiday, holiday.template, state) == []
    named = spec(collection="events", template={"id": "{{id:ev-}}", "guests": ["{{person.email}}"]})
    assert check_addressing_template(named, named.template, state) == []
    literal = spec(collection="events", template={"id": "{{id:ev-}}", "guests": ["jo@acme.test"]})
    assert check_addressing_template(literal, literal.template, state) == []


def test_a_world_laid_by_older_code_is_stale_and_laid_again(tmp_path):
    """A marker that only has to exist makes a fixed expander unreachable.

    The driver gates this step on world/BULK.json being present, and nothing about the marker could
    make it stale, so the 42 worlds on disk -- laid by code that kept 3 of 273,453 declared Slack
    messages and 3 of 124,265 calendar events, and stamped "bulk" on 342,974 ids -- would have
    carried that layer to delivery however often the expander was fixed. The marker now records the
    expander's version, a change to it is staleness, and the previous layer comes off before the new
    one goes on: one world's gmail still holds 1,365 records of a layer BULK.json no longer names,
    which is what laying twice without stripping leaves behind.
    """
    from company_envs.world.bulk_layer import BULK_VERSION, add_bulk, current

    root, folder = _seeded_company(tmp_path, [EMAIL])
    add_bulk(root, folder, models=Models(FULL_TEMPLATE))
    marker = read(folder / "world/BULK.json")
    assert marker["bulk_version"] == BULK_VERSION and current(marker) is True
    assert add_bulk(root, folder, models=Models(FULL_TEMPLATE))["skipped"], "a current layer is left alone"

    stale = {**marker, "bulk_version": BULK_VERSION - 1}
    write(folder / "world/BULK.json", stale)
    assert current(stale) is False and current({}) is False
    report = add_bulk(root, folder, models=Models(FULL_TEMPLATE))
    assert report["added"] == {"gmail_mock": {"emails": 12}}, "the step runs again instead of skipping"
    state = read(folder / "world/gmail_mock.state.json")
    assert len(state["emails"]) == 13, "the old layer came off first: 1 human email and 12 laid once"
    assert read(folder / "world/BULK.json")["bulk_version"] == BULK_VERSION


def test_a_cadence_is_kept_or_the_count_is_cut_and_said_so():
    """A cadence was a label: the surplus landed uniformly at random across the window.

    1,578 of the 1,842 specs on disk ask for more records than their window holds, 195,700 records'
    worth. 5,350 records of "weekdays" specs landed on a weekend, and a "monthly" check landed 58
    times in one month and 6 times in one day. The surplus now cycles over the cadence's own days,
    and a template that repeats one thing rather than drawing an entity per record is capped at one
    per period with the difference recorded.
    """
    from datetime import datetime

    from company_envs.world.bulk import expand, periods, varies_per_record
    from company_envs.world.bulk_layer import lay_bulk

    # Two working weeks hold ten weekdays; the spec asks for forty.
    busy = spec(count=40, cadence="weekdays", start="2026-01-05", end="2026-01-19", collection="emails")
    records = expand(busy, PEOPLE, seed=2)
    days = {r["timestamp"][:10] for r in records}
    assert len(records) == 40, "a template that draws a person per record keeps its volume"
    assert len(periods(busy)) == 10 and len(days) == 10
    assert all(datetime.fromisoformat(r["timestamp"]).weekday() < 5 for r in records), "no weekend"

    # The same window, a template that names nobody: one record per weekday and a receipt.
    plain = spec(
        count=40,
        cadence="weekdays",
        start="2026-01-05",
        end="2026-01-19",
        template={"id": "{{id:rem-}}", "subject": "Time entry open", "timestamp": "{{datetime}}"},
    )
    assert varies_per_record(plain.template) is False and varies_per_record(busy.template) is True
    assert len(expand(plain, PEOPLE, seed=2)) == 10
    state = {"emails": [], "users": PEOPLE}
    _added, _ids, short = lay_bulk(state, [plain], 3, 10_000_000)
    assert short[0]["asked"] == 40 and short[0]["written"] == 10
    assert "cadence weekdays holds 10" in short[0]["reason"]
    assert lay_bulk({"emails": [], "users": PEOPLE}, [busy], 3, 10_000_000)[2] == [], "nothing lost"


def test_generated_ids_use_the_scheme_the_collection_already_uses():
    """One substring told a reader which records nobody wrote: "bulk", on 342,974 ids.

    A task that says "the newest invoice" was answerable with grep, and a reviewer reading a sample
    could see which records were filler. The word arrives glued as often as separated
    ("wmbulk-desk-handover-", "bulk92617cal-dover-desk-"), and specs reference each other's
    prefixes, so the whole call is mapped at once: a comments spec whose ticket_id is the tickets
    spec's prefix must still point at its ticket.
    """
    from company_envs.world.bulk import id_scheme, native_ids, with_native_ids
    from company_envs.world.bulk_layer import lay_bulk

    state = {
        "emails": [{"id": "msg-0007", "subject": "Hi"}, {"id": "msg-0008", "subject": "Re: Hi"}],
        "threads": {"th-1": {"id": "th-1"}},
        "users": PEOPLE,
    }
    assert id_scheme(state, "emails") == ("msg-", 0, 1)
    parent = spec(count=2, id_prefix="bulk-stmt-", template={"id": "{{id:bulk-stmt-}}", "s": "{{person}}"})
    child = spec(
        count=2,
        collection="threads",
        id_prefix="wmbulk-th-",
        keyed_by="id",
        template={"id": "{{id:wmbulk-th-}}", "emailId": "{{id:bulk-stmt-}}"},
    )
    mapping = native_ids(state, [parent, child])
    assert mapping["bulk-stmt-"] == "msg-stmt-" and "bulk" not in mapping["wmbulk-th-"]
    laid = with_native_ids(child, mapping)
    assert laid.template["emailId"] == "{{id:msg-stmt-}}", "the reference follows the prefix it names"

    lay_bulk(state, [parent, child], 3, 10_000_000)
    written = [e["id"] for e in state["emails"]][2:]
    assert written == ["msg-stmt-1", "msg-stmt-2"] and "bulk" not in json.dumps(state).lower()
    assert [t["emailId"] for t in state["threads"].values() if "emailId" in t] == written

    numbered = {"tickets": [{"id": "41"}, {"id": "42"}]}
    assert id_scheme(numbered, "tickets") == ("", 2, 43)
    ticket = spec(count=2, collection="tickets", id_prefix="88101", template={"id": "{{id:88101}}"})
    lay_bulk(numbered, [ticket], 3, 10_000_000)
    assert [r["id"] for r in numbered["tickets"]] == ["41", "42", "43", "44"], "the series continues"


def test_a_template_that_invents_its_parents_and_owners_is_refused():
    """One invented parent in a template is ``count`` records nobody can reach.

    A drive holds 2,573 items of which 2,529 are parented to folders never created and 2,517 owned
    by users the app has never heard of; another holds 3,004 items and no folder. Fleet-wide 20,155
    items have a dangling parent and 10,580 an impossible owner, and the check stage cannot say so:
    it collapses a field that never resolves into one warning, because a grouping key with no
    collection behind it is how Gmail's threadId works. 329 of the 1,842 specs on disk, 89,076
    records, are refused here instead.
    """
    from company_envs.world.bulk import reference_fields, state_identifiers
    from company_envs.world.bulk_layer import check_reference_template

    state = {
        "items": [
            {"id": "folder-buying", "type": "folder", "parentId": None, "ownerId": "u-buyer"},
            {"id": "file-offer", "type": "pdf", "parentId": "folder-buying", "ownerId": "u-buyer"},
        ],
        "users": [{"id": "u-buyer", "name": "Jo Park", "email": "jo@acme.test"}],
    }
    state["items"][1]["sharedWith"] = [{"userId": "u-buyer", "role": "viewer"}]
    assert "folder-buying" in state_identifiers(state) and "u-buyer" in state_identifiers(state)
    assert reference_fields(state) == {"parentId", "ownerId", "userId"}

    invented = spec(
        collection="items",
        count=368,
        template={
            "id": "{{id:cat-}}",
            "parentId": "folder-technical",
            "ownerId": "u-catalog",
            "sharedWith": [{"userId": "u-technical", "role": "viewer"}],
        },
    )
    problems = " | ".join(check_reference_template(invented, invented.template, state))
    assert "'folder-technical'" in problems and "368 records" in problems
    assert "'u-catalog'" in problems and "/sharedWith/0/userId" in problems

    real = spec(
        collection="items",
        count=368,
        template={"id": "{{id:cat-}}", "parentId": "folder-buying", "ownerId": "{{person.id}}"},
    )
    assert check_reference_template(real, real.template, state) == []
    # An id this expansion invents resolves only when some spec of the same call stamps it.
    chained = spec(collection="items", count=4, template={"id": "{{id:cat-}}", "parentId": "{{id:fold-}}"})
    assert check_reference_template(chained, chained.template, state, {"fold-"}) == []
    assert check_reference_template(chained, chained.template, state, {"other-"})
    # A grouping key with no collection behind it is not judged at all.
    grouped = {"emails": [{"id": "e1", "threadId": "t-77"}]}
    threaded = spec(collection="emails", template={"id": "{{id:m-}}", "threadId": "t-99"})
    assert check_reference_template(threaded, threaded.template, grouped) == []


def test_counts_and_sizes_are_recomputed_from_the_records_that_landed():
    """A count is a fact about other records, and a template stamps the same number on all of them.

    238 of 254 tickets in one world carry a comment_count that contradicts their comments, 19 of 300
    customers an ordersCount, and every generated file kept the template's constant size while its
    content varied by hundreds of bytes: 58,240 such fields across the fleet. Only the records this
    expansion wrote are corrected -- a number the app author typed is the author's to answer for.
    """
    from company_envs.world.bulk import recompute_derived
    from company_envs.world.bulk_layer import lay_bulk

    state = {
        "tickets": [{"id": "t-1", "comment_count": 9}],
        "comments": [{"id": "c-0", "ticket_id": "t-1"}],
        "users": PEOPLE,
    }
    assert recompute_derived(state, set()) == 0, "an author's number is left alone"
    assert state["tickets"][0]["comment_count"] == 9

    tickets = spec(
        collection="tickets",
        count=2,
        id_prefix="t-8",
        template={"id": "{{id:t-8}}", "comment_count": 7, "subject": "{{person}}"},
    )
    comments = spec(
        collection="comments",
        count=2,
        id_prefix="c-8",
        template={"id": "{{id:c-8}}", "ticket_id": "{{id:t-8}}", "body": "{{person}}"},
    )
    lay_bulk(state, [tickets, comments], 3, 10_000_000)
    laid = [t for t in state["tickets"] if t["id"] != "t-1"]
    assert len(laid) == 2 and [t["comment_count"] for t in laid] == [1, 1], "one comment each, not 7"
    assert state["tickets"][0]["comment_count"] == 9, "the human ticket still says what it said"

    files = {"items": [], "users": PEOPLE}
    doc = spec(
        collection="items",
        count=2,
        id_prefix="f-",
        template={"id": "{{id:f-}}", "size": 1350, "content": "Report for {{person}} on {{date}}"},
    )
    lay_bulk(files, [doc], 3, 10_000_000)
    assert [r["size"] for r in files["items"]] == [len(r["content"].encode()) for r in files["items"]]
    assert all(r["size"] != 1350 for r in files["items"])


def test_the_marker_lists_what_the_records_own_not_what_they_point_at():
    """A foreign key recorded as a bulk id is a human record, and the strip deletes it.

    ``ids`` held every id-shaped field of every generated record: 27,883 of the fleet's 752,503
    recorded ids are references, not identities. Stripping by that list before a re-lay destroyed
    4,732 of the fleet's 81,886 human records -- nordstrom's drive went from 2,608 items to 1,
    because its generated files name the human folders in ``parentId`` -- and the texture gate
    excused every human record a generated one happened to point at.
    """
    from company_envs.world.bulk_layer import lay_bulk
    from company_envs.world.world_repair import strip_bulk

    state = {
        "items": {
            "folder-reports": {"id": "folder-reports", "type": "folder", "parentId": None},
            "file-march": {"id": "file-march", "type": "pdf", "parentId": "folder-reports"},
        },
        "users": PEOPLE,
    }
    filed = spec(
        collection="items",
        count=3,
        id_prefix="doc-",
        keyed_by="id",
        template={"id": "{{id:doc-}}", "parentId": "folder-reports", "name": "Receipt {{person}}"},
    )
    _added, ids, _short = lay_bulk(state, [filed], 3, 10_000_000)
    # "folder-" is the leading token this collection already uses, so the prefix is rewritten into it.
    assert ids == {"folder-doc-1", "folder-doc-2", "folder-doc-3"}
    assert "folder-reports" not in ids, "the parent they are filed under is not one of their ids"
    human = strip_bulk(state, ids)
    assert set(human["items"]) == {"folder-reports", "file-march"}, "the folder survives the strip"


def test_a_layer_nothing_claims_is_stripped_before_the_next_one_goes_on():
    """A previous layer the marker does not list is never stripped, and it poisons what follows.

    26,369 records in 7 of 42 worlds belong to one: fairfax's gmail holds 2,808 under a marker that
    lists 60 ids, and its drive residue, 4,872 records, sits under an app the marker has no entry
    for at all. The residue ate the byte budget so the new layer got 60 of 12,895 declared records,
    it is not excluded from the texture check so 3,115 machine-stamped emails are judged as human
    prose -- both of that world's blocking errors -- and no repair can rewrite generated records.

    The recorded ``id_prefix`` finds 49 of the 33,880 such ids on disk, because the residue came
    from a generation whose specs BULK.json no longer holds. What every one carries is the word
    ``_clean_prefix`` now removes and a running number; what a person wrote does not.
    """
    from company_envs.world.bulk import machine_stamped
    from company_envs.world.bulk_layer import numbered_families, previous_layer_ids

    assert machine_stamped("crpa-bulk-mail-north-desk-364") and machine_stamped("glbulk-doc-nursing-1")
    assert not machine_stamped("prod-qa-pushin-bulkhead"), "bulk is also an English word"
    assert not machine_stamped("lr-history-251124-holiday-bulk-access")

    state = {
        "emails": [
            {"id": "mail-july", "subject": "July deposit"},
            {"id": "crpa-bulk-mail-desk-1", "subject": "Desk handover"},
            {"id": "crpa-bulk-mail-desk-2", "subject": "Desk handover"},
        ],
        "users": PEOPLE,
    }
    marker = {"ids": ["stmt-1", "stmt-2"], "added": {"emails": 2}, "specs": []}
    ids = previous_layer_ids(state, marker)
    assert ids == {"crpa-bulk-mail-desk-1", "crpa-bulk-mail-desk-2"}
    assert previous_layer_ids({"emails": [], "users": PEOPLE}, marker) == set(), (
        "nothing on disk, nothing to strip"
    )
    # An older marker's list is every id-shaped field, so only the ids a record of an added-to
    # collection calls itself by, and only the ones in a stamped run, may be stripped by it.
    wide = {"ids": ["doc-1", "doc-2", "folder-reports", "c1", "c2"], "added": {"items": 2}, "specs": []}
    laid = {
        "items": {k: {"id": k, "parentId": "folder-reports"} for k in ("doc-1", "doc-2")}
        | {"folder-reports": {"id": "folder-reports", "parentId": None}},
        "calendars": {"c1": {"id": "c1"}, "c2": {"id": "c2"}},
    }
    assert previous_layer_ids(laid, wide) == {"doc-1", "doc-2"}
    assert numbered_families({"e1", "drive-policies", "a-1", "a-2"}) == {"a-1", "a-2"}


def test_a_typescript_record_table_is_a_record_collection():
    """20 of the 94 pinned schemas write their record table in TypeScript, and it went unread.

    ``kind.startswith("array")`` misses ``Issue[]``, so ``record_collections`` returned None for
    ServiceNow, airtable, amazon, asana, azure, confluence, facebook, jira, monday, quickbooks,
    salesforce, twitter, workday, youtube and six more. Those apps got no bulk layer and no entry
    in BULK.json saying why, and the feature matrix fell back to every top-level key. hubspot was
    read but misread: its decisive collections were ``dealStages`` and ``ticketStatuses`` while
    contacts, deals and tickets were invisible.
    """
    from company_envs.world.hub_app import record_collections

    schema = (
        "## State Schema\n| Key | Type | Description |\n|---|---|---|\n"
        "| `issues` | `Issue[]` | All issues |\n"
        "| `boards` | `Record<string, Board>` | Boards keyed by id |\n"
        "| `comments` | `{[targetId: string]: Comment[]}` | Comments per target |\n"
        "| `currentUser` | `User` | Logged-in user |\n"
        "| `selectedIds` | `string[]` | Selection |\n"
        "| `starred` | `{[itemId: string]: boolean}` | Flags |\n"
        "| `notes` | array | Notes |\n"
    )
    assert record_collections(schema) == ["issues", "boards", "comments", "notes"]
    # A serialized blob and a timestamp are not records, and saying so is not the same as failing
    # to read the table: canvas_mock is the one app of the 20 that really has no collections.
    assert (
        record_collections("## State Schema\n| Key | Type |\n|---|---|\n| `canvasJSON` | `string` |\n")
        is None
    )


def test_an_app_with_no_record_collections_says_so_instead_of_vanishing(tmp_path):
    """An app absent from BULK.json reads exactly like an app nothing was ever tried on.

    20 of 94 schemas returned no collections, ``bulk_layer`` computed ``keys = []``, and the app was
    left out of the marker with no receipt -- so nothing downstream could tell a filled app from an
    unreadable schema from a crashed call.
    """
    from company_envs.world.bulk_layer import add_bulk

    root, folder = _seeded_company(tmp_path, [EMAIL])
    (root / "schemas/gmail_mock.md").write_text(
        "## State Schema\n| Key | Type | Description |\n|---|---|---|\n"
        "| `emails` | object | Inbox blob |\n| `users` | object | Directory blob |\n"
    )
    models = Models(STUB_TEMPLATE)
    report = add_bulk(root, folder, models=models)
    entry = read(folder / "world/BULK.json")["apps"]["gmail_mock"]
    assert "no record collections" in entry["skipped"] and entry["added"] == {}
    assert entry["record_collections"] == [] and report["apps"] == 1
    assert models.payloads == [], "no call is spent on an app with nothing to fill"


def test_an_invented_parent_is_filed_where_the_app_already_files_things():
    """A template's invented parent is ``count`` records nobody can open, and the answer is on disk.

    23,367 records' worth of templates name a parent their collection does not have: 20,559 drive
    items under folders nobody created and 2,808 outlook messages in a folder that does not exist.
    Refusing costs one of the three spec attempts and the revision has to guess; the human layer
    already says where records of this kind go, so the field is anchored to the closest value the
    collection actually resolves and the rewrite is recorded.
    """
    from company_envs.world.bulk_layer import anchor_references, check_reference_template

    state = {
        "items": [
            {"id": "folder-archive", "type": "folder", "parentId": None, "ownerId": "u1"},
            {"id": "folder-admin", "type": "folder", "parentId": None, "ownerId": "u1"},
            {"id": "file-may", "type": "pdf", "parentId": "folder-archive", "ownerId": "u1"},
        ],
        "users": PEOPLE,
    }
    invented = spec(
        collection="items",
        count=730,
        template={"id": "{{id:f-}}", "parentId": "folder-archiv", "ownerId": "u1", "name": "Receipt"},
    )
    assert check_reference_template(invented, invented.template, state), "refused before anchoring"
    anchored, notes = anchor_references(invented, state)
    assert anchored.template["parentId"] == "folder-archive", "the closest folder that exists"
    assert notes == ["/parentId: 'folder-archiv' is no record of this app, filed under 'folder-archive'"]
    assert check_reference_template(anchored, anchored.template, state) == []
    # A placeholder decides per record, and a field the state never resolves is not a reference.
    kept = spec(collection="items", template={"id": "{{id:f-}}", "parentId": "{{pick:folders}}"})
    assert anchor_references(kept, state)[1] == []
    grouped = {"emails": [{"id": "e1", "threadId": "t-77"}]}
    threaded = spec(collection="emails", template={"id": "{{id:m-}}", "threadId": "t-99"})
    assert anchor_references(threaded, grouped)[1] == []


def test_a_shortfall_names_the_cause_and_the_lever_for_it():
    """ "byte budget or duplicate ids" was one string for two causes with different fixes.

    The fleet wrote 637,854 of 653,424 declared records at the 4 MB cap, and 10,660 of the 15,570
    missing were refused by the cap itself: 42 of 199 app layers sit against it, every one of them
    google_drive_mock, Zendesk_mock or gmail_mock. At 5 MB that loss is 790 records, at 6 MB it is
    0 and the largest state laid is 5.40 MB -- so nothing above 6 MB buys a record, and what remains
    is the cadence, which is the spec's to answer for and not config.toml's.
    """
    from company_envs.world.bulk_layer import _volume, lay_bulk

    state = {"emails": [], "users": PEOPLE}
    records = expand(spec(count=40), PEOPLE, seed=3)
    cramped = len(json.dumps(state)) + len(json.dumps(records[0])) * 4
    _added, _ids, short = lay_bulk(state, [spec(count=40)], 3, cramped)
    assert short[0]["lost_to"]["byte budget"] and "byte budget holds" in short[0]["reason"]
    assert "cadence" not in short[0]["lost_to"], "a monthly spec over a year holds all 40"

    again = {"emails": [], "users": PEOPLE}
    _added, _ids, twice = lay_bulk(again, [spec(count=5), spec(count=5)], 3, 10_000_000)
    assert twice[0]["lost_to"] == {"duplicate ids": 5}, "the second spec stamps the first one's ids"

    whole = _volume({"gmail_mock": {"specs": [{"count": 40}], "added": {"emails": 4}, "short": short}})
    assert whole["share"] == 0.1 and whole["lost_to"]["byte budget"] == 36
    assert "bulk_max_bytes_per_app" in whole["levers"]["byte budget"]


def test_recorded_specs_are_laid_again_when_they_still_pass_the_gate(tmp_path):
    """BULK.json stores every spec verbatim, and a re-lay spent a model call rewriting them anyway.

    The expander changed tonight, not the specs: re-laying the 199 spec sets on disk over their
    stripped human layers writes 648,489 of 653,424 declared records against the 197,452 on disk.
    59 of those sets still pass ``check_specs`` against their human layer -- 312,979 records for no
    model call. The other 140 do not, and the gate is what decides: it judges shape, addressing and
    every reference against the layer the records are going onto, so a re-seeded world cannot reuse
    specs that no longer fit it.
    """
    from company_envs.world.bulk_layer import BULK_VERSION, add_bulk

    root, folder = _seeded_company(tmp_path, [EMAIL])
    add_bulk(root, folder, models=Models(FULL_TEMPLATE))
    marker = folder / "world/BULK.json"
    write(marker, {**read(marker), "bulk_version": BULK_VERSION - 1})  # the expander moved on

    again = Models(FULL_TEMPLATE)
    report = add_bulk(root, folder, models=again)
    assert again.payloads == [], "the specs are on disk: no call is spent asking for them again"
    assert report["added"] == {"gmail_mock": {"emails": 12}}
    entry = read(marker)["apps"]["gmail_mock"]
    assert entry["reused_specs"] is True and entry["receipt"] is None
    assert len(read(folder / "world/gmail_mock.state.json")["emails"]) == 13, "laid once, not twice"

    # A recorded spec the gate now refuses is re-asked instead: these records reach no worker.
    outside = spec(count=4, template={**FULL_TEMPLATE, "to": [{"email": "nobody@outside.test"}]})
    stale = read(marker)
    stale["bulk_version"] = BULK_VERSION - 1
    stale["apps"]["gmail_mock"]["specs"] = [outside.model_dump()]
    write(marker, stale)
    third = Models(FULL_TEMPLATE)
    add_bulk(root, folder, models=third)
    assert [p["call"] for p in third.payloads] == ["bulk_specs"]
    assert "reused_specs" not in read(marker)["apps"]["gmail_mock"]


def test_an_irregular_plural_finds_its_own_primary_key():
    """``"activities".removesuffix("s")`` is ``"activitie"``, so the universe looked for
    ``activitieId``.

    Salesforce keys activities on ``activityId`` and every one of 183 values resolves, yet not one
    entered ``state_identifiers``, so ``activityId`` read as a field that never resolves -- the one
    state in which every reference guard downstream abstains. Measured over the worlds on disk the
    fix is worth 587 ids and 864 references that used to dangle and now resolve, in salesforce
    alone; no other app changes.
    """
    state = {
        "activities": [
            {"activityId": "act901", "subject": "Renewal call", "relatedToId": "case901"},
            {"activityId": "act902", "subject": "Site visit", "relatedToId": "act901"},
        ],
        "cases": [{"caseId": "case901", "subject": "Billing query"}],
    }
    universe = bulk.state_identifiers(state)
    assert {"act901", "act902", "case901"} <= universe
    assert "activityId" in bulk.reference_fields(state), "a field that resolves must be judged"


def test_a_field_that_names_a_collection_is_judged_even_when_none_of_it_resolves():
    """The guard was switched off by the same defect it exists to prevent.

    A template's reference is judged only on fields the seeded state resolves at least once,
    because a field that never resolves is usually how the product works -- gmail groups by
    ``threadId`` and has no threads collection. That rule abstains exactly when the human layer is
    already broken: one drive holds four items whose ``parentId`` all dangle and no folder at all,
    ``parentId`` is dropped from the judged set, and 3,000 bulk items are laid under folders nobody
    wrote. Served, that world draws "This folder is empty" over 3,004 records.

    So a never-resolving field is judged as well when its own name names a record collection this
    app holds. Measured over the 60 worlds that is the whole difference and none of the exemption.
    """
    drive = {
        "items": {
            "doc-1": {"id": "doc-1", "name": "Close rules", "parentId": "folder-policies"},
            "doc-2": {"id": "doc-2", "name": "Eligibility", "parentId": "folder-policies"},
        },
        "users": [{"id": "u-fiscal", "name": "Jo Park", "email": "jo@x.test"}],
    }
    assert "parentId" in bulk.container_fields(drive)
    assert "parentId" not in bulk.reference_fields(drive), "nothing resolves it; that is the point"

    # gmail's threadId names no collection here and stays exempt, which is what the rule is for.
    gmail = {
        "emails": [{"id": "e1", "subject": "Renewal", "threadId": "t-990"}],
        "labels": [{"id": "lab-a", "name": "Tenancy"}],
    }
    assert "threadId" not in bulk.container_fields(gmail)
    # google_calendar's `user` is one signed-in person, not a directory, so userId stays exempt too.
    calendar = {
        "user": {"id": "cp-corinne", "username": "Corinne Vautrin", "email": "c@x.test"},
        "events": [{"id": "ev-1", "summary": "Review", "userId": "cp-devika"}],
    }
    assert "userId" not in bulk.container_fields(calendar)


def test_a_generated_record_is_filed_in_the_container_the_app_draws_from():
    """A card appended to ``state["cards"]`` and to no ``cardIds`` is a card on no board.

    Trello renders a list from ``list.cardIds`` (``List.jsx:59``), and the expander wrote the child
    and left the other side of the link alone: 62 generated cards name a list that exists and
    appear in no ``cardIds``. It is the only unreachability the generator itself makes -- the other
    27,596 records name a container nobody wrote -- and it is the one that would keep happening at
    every scale.

    The link is completed only where the record names that container itself, and only into a list
    the container already has: inventing a field would file records where the app does not read.
    """
    for lists, cards in (
        ([{"id": "l1", "boardId": "b1", "cardIds": []}], []),
        ({"l1": {"id": "l1", "boardId": "b1", "cardIds": []}}, {}),
    ):
        state = {"boards": [{"id": "b1", "listIds": ["l1"]}], "lists": lists, "cards": cards}
        spec = bulk.BulkSpec(
            app_id="trello_mock",
            collection="cards",
            what="index receipts",
            count=2,
            start="2026-08-01",
            end="2026-08-10",
            cadence="daily",
            id_prefix="bulk-idx-",
            template_json=json.dumps(
                {"id": "{{id:bulk-idx-}}", "title": "Index receipt {{seq}}", "listId": "l1"}
            ),
            tables_json="{}",
            keyed_by="id",
        )
        added = bulk.add_to_state(state, spec, bulk.expand(spec, [], seed=1), 4_000_000)
        held = (lists[0] if isinstance(lists, list) else lists["l1"])["cardIds"]
        assert added == 2 and held == ["bulk-idx-1", "bulk-idx-2"]

    # A card naming a list that does not exist is filed nowhere: that is the reference guard's
    # finding, and putting it in an arbitrary list would hide it.
    state = {"lists": [{"id": "l1", "cardIds": []}], "cards": []}
    ghost = bulk.BulkSpec(
        app_id="trello_mock",
        collection="cards",
        what="index receipts",
        count=2,
        start="2026-08-01",
        end="2026-08-10",
        cadence="daily",
        id_prefix="ghost-",
        template_json=json.dumps({"id": "{{id:ghost-}}", "title": "t", "listId": "l-never-written"}),
        tables_json="{}",
        keyed_by="id",
    )
    bulk.add_to_state(state, ghost, bulk.expand(ghost, [], seed=1), 4_000_000)
    assert state["lists"][0]["cardIds"] == []


def test_a_container_this_layer_fills_has_its_own_count_recomputed():
    """Filing 3,123 cards into a list that still says it holds 13 would be a defect we made.

    ``recompute_derived`` deliberately leaves the author's numbers alone -- a count the app author
    wrote is the author's to answer for -- but a count of children this layer just added is this
    layer's doing. The container stays out of the marker's id list all the same: a human id
    recorded as bulk is how the strip before a re-lay deleted 4,732 human records, one drive going
    from 2,608 items to 1.
    """
    from company_envs.world.bulk_layer import lay_bulk

    state = {
        "collections": [{"id": "col-1", "title": "Workspace", "productIds": ["p-1"], "productsCount": 1}],
        "products": [{"id": "p-1", "title": "Hose", "collectionId": "col-1"}],
    }
    spec = bulk.BulkSpec(
        app_id="shopify_admin_mock",
        collection="products",
        what="catalogue rows",
        count=3,
        start="2026-08-01",
        end="2026-08-10",
        cadence="daily",
        id_prefix="bulk-p-",
        template_json=json.dumps(
            {"id": "{{id:bulk-p-}}", "title": "Fitting {{seq}}", "collectionId": "col-1"}
        ),
        tables_json="{}",
        keyed_by="id",
    )
    added, owned, _short = lay_bulk(state, [spec], 0, 4_000_000)
    collection = state["collections"][0]
    assert added == {"products": 3} and len(collection["productIds"]) == 4
    assert collection["productsCount"] == 4, "the count follows the records this layer added"
    assert "col-1" not in owned, "the container is the author's record, not this layer's"


def test_a_provider_fault_leaves_the_layer_it_could_not_replace(tmp_path):
    """The strip ran on disk before any spec was authored, so a fault destroyed a good layer.

    Measured the hard way: one re-lay died on a cache miss and left blastx's trello at 106 cards
    having served 3,288, with nothing in the marker saying why. That is an operation meant to
    repair a world degrading it, and it is two rules at once -- a crashed run writing the receipt
    a verdict writes, and a world losing volume no stage asked it to lose.

    So the strip happens in memory and the state on disk is replaced only once its replacement
    exists. A refusal is a verdict: the app keeps its human layer and the marker says so. Anything
    else says nothing about the world, so nothing is written, the marker records a fault, and that
    key is what makes the marker stale so the step runs again.
    """
    from company_envs.world.bulk_layer import BULK_VERSION, add_bulk, current, laid_by_this_code

    class Unavailable(Models):
        def call(self, job, prompt, response_type):
            raise RuntimeError("cache miss for world_states")

    root, folder = _seeded_company(tmp_path, [EMAIL])
    add_bulk(root, folder, models=Models(FULL_TEMPLATE))
    laid = read(folder / "world/gmail_mock.state.json")
    assert len(laid["emails"]) == 13, "one human email and twelve laid"

    # Stale, and with no specs to reuse, so the run has to reach the model -- and cannot.
    marker = read(folder / "world/BULK.json")
    marker["bulk_version"] = BULK_VERSION - 1
    marker["apps"]["gmail_mock"] = {**marker["apps"]["gmail_mock"], "specs": []}
    write(folder / "world/BULK.json", marker)
    report = add_bulk(root, folder, models=Unavailable(FULL_TEMPLATE))
    assert read(folder / "world/gmail_mock.state.json") == laid, "the layer it could not replace survives"
    marker = read(folder / "world/BULK.json")
    assert "RuntimeError" in marker["faults"]["gmail_mock"], "the fault is recorded, not silent"
    assert report["faults"], "and the caller is told"
    # The same answer in the shared vocabulary of company_envs.receipt, so a reader of a dozen
    # stages' markers needs no bulk-specific knowledge to learn that this run measured nothing.
    assert marker["outcome"] == "faulted" and marker["apps"]["gmail_mock"]["outcome"] == "faulted"
    assert report["outcome"] == "faulted" and report["ok"] is False
    assert "stripped" not in marker, "nothing came off, so nothing is reported as having come off"
    # The layer is still this marker's to name, or its records would read as the human layer.
    assert marker["apps"]["gmail_mock"]["ids"], "the previous layer keeps its identifiers"
    # Stale by the fault alone: the expansion semantics are current, the world is not finished.
    assert laid_by_this_code(marker) is True and current(marker) is False

    # And a refusal is the other thing: a verdict, so the human layer is written and no fault.
    class Refuses(Models):
        def call(self, job, prompt, response_type):
            raise ModelOutputInvalid("no spec survives the gate", {}, {"model": "fake"})

    add_bulk(root, folder, models=Refuses(FULL_TEMPLATE), again=True)
    refused = read(folder / "world/BULK.json")
    assert "faults" not in refused and refused["apps"]["gmail_mock"]["skipped"]
    assert len(read(folder / "world/gmail_mock.state.json")["emails"]) == 1, "back to the human layer"
    # A verdict, and the marker says the word: re-running reaches the same answer, so unlike the
    # fault above it is not stale. The two used to be one shape, which is how the re-lay that died
    # on a cache miss read as "the specs were refused" and took a good layer with it.
    assert refused["outcome"] == "refused" and refused["apps"]["gmail_mock"]["outcome"] == "refused"
    assert current(refused) is True, "a refusal is a finished verdict; only a fault is stale"


def test_sparse_stream_spans_the_requested_history():
    from company_envs.world.bulk import expand

    item = spec(count=18, cadence="weekdays", start="2025-03-01", end="2026-09-01")
    records = expand(item, PEOPLE, seed=0)
    months = {r["timestamp"][:7] for r in records}
    assert len(months) >= 17
    assert max(months) == "2026-08"
    assert records == expand(item, PEOPLE, seed=0)


def test_monthly_periods_preserve_calendar_anchor():
    from company_envs.world.bulk import periods

    item = spec(cadence="monthly", start="2025-01-31", end="2025-05-01")
    assert [d.date().isoformat() for d in periods(item)] == [
        "2025-01-31",
        "2025-02-28",
        "2025-03-31",
        "2025-04-30",
    ]
