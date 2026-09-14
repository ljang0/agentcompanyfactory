import json

from company_envs.world import names
from company_envs.world.state_seed import read_registry, register_people


def test_colliding_people_are_renamed_everywhere_and_registered(tmp_path):
    companies = tmp_path / "companies"
    register_people(companies, "earlier-co", {"Marcus Bell", "Anika Desai"}, {"marcus.bell@earlier.test"})
    folder = companies / "acme"
    (folder / "world" / "materials" / "boss").mkdir(parents=True)
    (folder / "world" / "world.json").write_text(
        json.dumps(
            {
                "staff": [
                    {"id": "s1", "name": "Marcus Bell", "email": "marcus.bell@acme.test"},
                    {"id": "s2", "name": "Ines Duarte"},
                ]
            }
        )
    )
    (folder / "world" / "identities.json").write_text(
        json.dumps({"boss": {"gmail_mock": {"id": "u1", "name": "Marcus Bell", "email": "mbell@acme.test"}}})
    )
    (folder / "world" / "gmail_mock.state.json").write_text(
        json.dumps(
            {
                "emails": [
                    {
                        "id": "e1",
                        "from": {"name": "Marcus Bell", "email": "marcus.bell@acme.test"},
                        "body": "Hi Ines, Marcus Bell here. Marcus Bellamy is someone else.",
                    }
                ]
            }
        )
    )
    (folder / "world" / "materials" / "boss" / "notes.md").write_text("Call Marcus Bell about the invoice.\n")
    mapping = names.dedupe_people(folder, companies, seed=1)
    assert set(mapping) == {"Marcus Bell"} and mapping["Marcus Bell"] != "Marcus Bell"
    new = mapping["Marcus Bell"]
    state = json.loads((folder / "world" / "gmail_mock.state.json").read_text())
    assert (
        state["emails"][0]["from"]["name"] == new
        and "Marcus Bellamy" in state["emails"][0]["body"]
        and "Marcus Bell " not in state["emails"][0]["body"]
    )
    assert state["emails"][0]["from"]["email"].split("@")[0] in names.email_forms(new).values()
    assert json.loads((folder / "world" / "identities.json").read_text())["boss"]["gmail_mock"]["name"] == new
    assert new in (folder / "world" / "materials" / "boss" / "notes.md").read_text()
    assert json.loads((folder / "world" / "NAMES.json").read_text())["renamed"] == mapping
    assert (
        read_registry(companies)["names"][new] == "acme"
        and read_registry(companies)["names"]["Ines Duarte"] == "acme"
    )
    assert names.dedupe_people(folder, companies, seed=1) == {}  # nothing left to rename


def test_a_thing_with_a_person_shaped_name_is_not_renamed():
    """Two or three capitalised words with no digits is not a person, and this renamed 38 things.

    Of the 202 renames on disk, 38 gave a human name to a QuickBooks report ("Balance Sheet",
    "General Ledger", "Trial Balance", "Unpaid Bills"), an Airtable field ("Supplier ID",
    "Observation ID"), a team ("People Operations"), a building ("Juniper Court"), a vendor
    ("Amazon Web Services") and a desk ("Order Desk") -- in both delivered worlds among them. Each
    one was also claimed in the cross-company registry, so it made other companies rename too.
    Names are harvested from the records that are people: where people sit, or carrying what only a
    person carries.
    """
    from company_envs.world.state_seed import is_person_record, looks_like_person, people_in

    assert looks_like_person("Marisol Vega") and looks_like_person("Ada de Vries")
    for thing in (
        "Balance Sheet",
        "Supplier ID",
        "US Operations",
        "Amazon Web Services",
        "Juniper Court",
        "Order Desk",
        "Payroll Summary",
        "Employee Directory",
    ):
        assert not looks_like_person(thing), thing

    assert is_person_record({"id": "u1", "name": "Jo Park", "email": "jo@acme.test"})
    assert is_person_record({"id": "p9", "name": "Jo Park"}, "active_patients")
    assert is_person_record({"id": "p9", "name": "Jo Park"}, "panel")
    assert not is_person_record({"id": "r1", "name": "Jo Park", "starred": False}, "reports")
    assert not is_person_record({"id": "v1", "name": "Jo Park", "apps": []}, "companies")

    tree = {
        "users": [{"id": "u1", "name": "Marisol Vega", "email": "marisol.vega@acme.test"}],
        "panel": [{"id": "p1", "name": "Adelia Brant"}],
        "reports": [{"id": "r1", "name": "Trial Balance"}, {"id": "r2", "name": "Cedric Mowbray"}],
        "companies": [{"id": "c1", "name": "Corbel Uniforms"}],
    }
    names, emails = people_in([tree])
    assert names == {"Marisol Vega", "Adelia Brant"}, "the panel is people, the report list is not"
    assert "Cedric Mowbray" not in names, "a report keeps its name even when the name reads human"
    assert emails == {"marisol.vega@acme.test"}


def test_a_renamed_person_loses_their_old_handles_and_ids(tmp_path):
    """Only the address was rewritten, so a renamed person kept their old identity everywhere else.

    197 of the 202 renames on disk leave residue: a person renamed from Celia Renshaw has the handle
    ``celia``, sits in ``thread-celia-signature`` and in the conversation ``dm-avril-celia``. A name
    is an entity with derived forms; renaming the string it appears under is not renaming the person.
    """
    companies = tmp_path / "companies"
    register_people(companies, "earlier-co", {"Celia Renshaw"}, set())
    folder = companies / "acme"
    (folder / "world").mkdir(parents=True)
    (folder / "world" / "world.json").write_text(
        json.dumps(
            {
                "staff": [
                    {"id": "u-celia", "name": "Celia Renshaw", "email": "celia.renshaw@acme.test"},
                    {"id": "u-avril", "name": "Avril Henshaw", "email": "avril.henshaw@acme.test"},
                ]
            }
        )
    )
    (folder / "world" / "slack_mock.state.json").write_text(
        json.dumps(
            {
                "users": [{"id": "u-celia", "name": "Celia Renshaw", "handle": "celia"}],
                "dms": [{"id": "dm-avril-celia", "userId": "u-celia"}],
                "threads": {"thread-celia-signature": [{"id": "m1", "senderId": "u-celia"}]},
                "mail": [{"id": "e1", "from": "crenshaw@acme.test", "cc": "renshaw.celia@acme.test"}],
            }
        )
    )
    mapping = names.dedupe_people(folder, companies, seed=1)
    assert set(mapping) == {"Celia Renshaw"}
    new = mapping["Celia Renshaw"]
    first, last = new.split()
    state = json.loads((folder / "world" / "slack_mock.state.json").read_text())
    assert state["users"][0]["handle"] == first.lower(), "the handle is the person, not a string"
    assert state["dms"][0]["id"] == f"dm-avril-{first.lower()}"
    assert f"thread-{first.lower()}-signature" in state["threads"]
    assert state["mail"][0]["from"] == f"{first[0].lower()}{last.lower()}@acme.test"
    assert state["mail"][0]["cc"] == f"{last.lower()}.{first.lower()}@acme.test"
    assert "celia" not in json.dumps(state).lower() and "renshaw" not in json.dumps(state).lower()
    assert "avril" in json.dumps(state).lower(), "the person nobody renamed keeps their identifiers"
    identifiers = json.loads((folder / "world" / "NAMES.json").read_text())["identifiers"]
    assert (
        identifiers["celia"] == first.lower()
        and identifiers["crenshaw"] == f"{first[0].lower()}{last.lower()}"
    )


def test_a_first_name_two_people_share_is_left_alone():
    """A bare token is only this person's when it is nobody else's.

    "Marcus Bell" renamed with "Marcus Bellamy" still in the world must not take Bellamy's first
    name with it, and prose is never touched by a bare token at all: "Celia" in a sentence may be a
    Celia the registry never renamed, and the string cannot say which.
    """
    shared = names.derived_map({"Marcus Bell": "Gustavo Calloway"}, {"Marcus Bell", "Marcus Bellamy"})
    assert "marcus" not in shared and shared["bell"] == "calloway"
    alone = names.derived_map({"Celia Renshaw": "Gustavo Calloway"}, {"Celia Renshaw"})
    assert alone["celia"] == "gustavo" and alone["celia.renshaw"] == "gustavo.calloway"
    prose = names.rename_text("Thanks, Celia. See celia@acme.test", {}, alone)
    assert prose == "Thanks, Celia. See gustavo@acme.test", "an identifier yes, a sentence no"


def _renamed_world(tmp_path):
    """A world in the shape the rename left on disk: NAMES.json with `renamed` and `emails` only."""
    folder = tmp_path / "companies" / "bluestone"
    (folder / "world" / "materials" / "manager").mkdir(parents=True)
    (folder / "world" / "world.json").write_text(
        json.dumps({"properties": ["Xiomara Okonkwo", "Hawthorn Works"], "staff": ["Desmond Okonkwo"]})
    )
    (folder / "world" / "identities.json").write_text(
        json.dumps({"manager": {"slack_mock": {"fullName": "Godfrey Delacroix", "displayName": "Audrey"}}})
    )
    (folder / "world" / "quickbooks_mock.state.json").write_text(
        json.dumps(
            {
                "customers": [
                    {"id": "c1", "name": "Xiomara Okonkwo", "contact": "balance.sheet@bluestone.test"},
                    {"id": "c2", "name": "Desmond Okonkwo"},
                ],
                "reports": [{"title": "Ulrike Sandoval"}],
            }
        )
    )
    (folder / "world" / "BULK.json").write_text(
        json.dumps({"apps": {"quickbooks_mock": {"tables_json": ["Xiomara Okonkwo", "Rivergate Offices"]}}})
    )
    (folder / "world" / "materials" / "manager" / "notes.txt").write_text(
        "Inspect Xiomara Okonkwo on Friday.\n"
    )
    (folder / "world" / "NAMES.json").write_text(
        json.dumps(
            {
                "renamed_at": "2026-09-09T00:00:00Z",
                "renamed": {
                    "Juniper Court": "Xiomara Okonkwo",
                    "Balance Sheet": "Ulrike Sandoval",
                    "Marcus Bell": "Desmond Okonkwo",
                },
                "emails": {
                    **{
                        k: v
                        for k, v in zip(
                            names.email_forms("Juniper Court").values(),
                            names.email_forms("Xiomara Okonkwo").values(),
                        )
                    },
                    **{
                        k: v
                        for k, v in zip(
                            names.email_forms("Balance Sheet").values(),
                            names.email_forms("Ulrike Sandoval").values(),
                        )
                    },
                    **{
                        k: v
                        for k, v in zip(
                            names.email_forms("Marcus Bell").values(),
                            names.email_forms("Desmond Okonkwo").values(),
                        )
                    },
                },
            }
        )
    )
    return folder


def test_renames_of_things_that_are_not_people_are_reverted_and_people_are_left_alone(tmp_path):
    """38 of 202 renames across 8 of 42 worlds took a thing for a person, and the rename is on disk.

    ``Balance Sheet`` became a person's name, so did ``Open Invoices``, ``Payroll Summary`` and the
    Airtable field names ``History ID`` and ``Team IDs``. One property, ``Juniper Court``, now
    appears under its new name 34 times in quickbooks, 317 in drive and 3 in world.json. Every one
    of the 38 fails today's person test, and NAMES.json records the mapping, so it is invertible.
    """
    folder = _renamed_world(tmp_path)
    plan = names.revert_plan(folder)
    assert set(plan["renamed"]) == {"Juniper Court", "Balance Sheet"}
    assert plan["kept"] == ["Marcus Bell"]  # a real person's rename stands
    assert plan["ambiguous"] == {}
    assert plan["occurrences"] == 5 and set(plan["files"]) == {
        "world/world.json",
        "world/BULK.json",
        "world/quickbooks_mock.state.json",
        "world/materials/manager/notes.txt",
    }

    names.revert_non_people(folder, apply=True)
    world = json.loads((folder / "world" / "world.json").read_text())
    state = json.loads((folder / "world" / "quickbooks_mock.state.json").read_text())
    assert world["properties"] == ["Juniper Court", "Hawthorn Works"]
    assert state["customers"][0]["name"] == "Juniper Court"
    assert state["reports"][0]["title"] == "Balance Sheet"
    # The bulk spec is written after the rename and re-lays the records; skipping it would put the
    # renamed property straight back on the next add-bulk.
    assert "Juniper Court" in (folder / "world" / "BULK.json").read_text()
    assert "Juniper Court" in (folder / "world" / "materials" / "manager" / "notes.txt").read_text()
    # The rename never touched the other Okonkwo, and neither does the revert.
    assert world["staff"] == ["Desmond Okonkwo"] and state["customers"][1]["name"] == "Desmond Okonkwo"
    record = json.loads((folder / "world" / "NAMES.json").read_text())
    assert set(record["renamed"]) == {"Marcus Bell"}
    assert set(record["reverted"]["renamed"]) == {"Juniper Court", "Balance Sheet"}
    assert names.revert_plan(folder)["renamed"] == {}  # nothing left to revert


def test_a_derived_form_the_rename_never_wrote_is_not_reverted(tmp_path):
    """Undoing a form that was never applied is not a no-op: it rewrites other people.

    ``Juniper Court`` became ``Xiomara Okonkwo`` and the world has a Desmond Okonkwo too. The
    inverse is built only from the shapes the record shows were written -- on disk that is the six
    address shapes -- so the bare last name is never reverted. 17 of the 38 share a bare token with
    another name in their own world.
    """
    folder = _renamed_world(tmp_path)
    plan = names.revert_plan(folder)
    assert "okonkwo" not in plan["token_map"] and "xiomara" not in plan["token_map"]
    assert plan["token_map"]["xiomara.okonkwo"] == "juniper.court"
    lengths = [len(token) for token in plan["token_map"]]
    assert lengths == sorted(lengths, reverse=True), "longest form first, as rename_text requires"


def test_an_identity_field_that_is_only_a_given_name_is_renamed_in_the_case_it_had(tmp_path):
    """A rename left bare given names behind, and then lower-cased the ones it did reach.

    One world's Slack identity has ``fullName: "Godfrey Delacroix"`` beside ``displayName:
    "Audrey"`` from the pre-rename name, and three visual judges blocked the company for it. 53
    identity fields across 18 of the 42 renamed worlds hold a bare given name, 44 of them
    ``displayName``. The whole value of such a field is the person, not prose -- but it is a
    display name, so it has to come back capitalised, not as the map's lower-case token.
    """
    name_map = {"Audrey Fenwick": "Godfrey Delacroix"}
    token_map = names.derived_map(name_map, {"Audrey Fenwick", "Marguerite Pemberton"})
    tree = {
        "fullName": "Audrey Fenwick",
        "displayName": "Audrey",
        "userId": "U-RL-AUDREY",
        "email": "audrey.fenwick@rl.test",
        "body": "Thanks, Audrey.",
    }
    renamed = names.rename_in(tree, name_map, token_map)
    assert renamed["displayName"] == "Godfrey"
    assert renamed["userId"] == "U-RL-GODFREY"
    assert renamed["email"] == "godfrey.delacroix@rl.test"
    assert renamed["body"] == "Thanks, Audrey.", "a bare first name in prose stays prose"


def test_a_short_given_name_is_swapped_in_an_identity_field_and_nowhere_else(tmp_path):
    """Three letters is a month as often as a name, so the bare form is kept out of prose.

    One of the 53 residues is ``displayName: "Jun"`` beside a rewritten full name. Adding "jun" to
    the identifier map would turn "12-Jun-2026" into a person, so the short forms apply to the
    whole value of an identity field and to nothing else.
    """
    name_map = {"Jun Seo": "Rafferty Vanterpool"}
    world = {"Jun Seo", "Marguerite Pemberton"}
    token_map = names.derived_map(name_map, world)
    short = names.given_map(name_map, world)
    assert "jun" not in token_map and short["jun"] == "rafferty"
    tree = {"displayName": "Jun", "note": "Due 12-Jun-2026", "shift": "Jun"}
    renamed = names.rename_in(tree, name_map, token_map, given_map=short)
    assert renamed["displayName"] == "Rafferty"
    assert renamed["note"] == "Due 12-Jun-2026"
    assert renamed["shift"] == "Jun", "only an identity field's whole value is the person"
