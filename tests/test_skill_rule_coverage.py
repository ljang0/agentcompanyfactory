"""Rules a validator enforces must appear in the skill whose output it judges.

Every case here is a rule that rejected real drafts while the skill said nothing about it, or
said the opposite. The counts come from companies/*/tasks/_rejected and from the repair feedback
carried in companies/*/tasks/*/*_calls/calls/*/attempt-*/prompt.txt.
"""

from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / ".agents" / "skills"


def skill(name):
    return (SKILLS / name / "SKILL.md").read_text()


def test_the_grader_skill_forbids_the_whole_collection_selector_its_gate_rejects():
    """57 of the 94 grader repair rounds were "selects a whole collection"; the skill asked only
    to "select the content, supporting records and counterevidence needed to judge the criterion"."""
    text = skill("company-task-grader")
    assert "never a bare collection" in text and "$.documents" in text


def test_the_golden_skill_asks_for_the_writes_its_gate_counts():
    """contract_problems requires a consequential write from EVERY worker the task gives a
    contribution; the skill asked for "at least three workers ... when the roster has three or
    more", which a five-worker roster satisfies and the gate then rejects."""
    text = skill("company-task-golden")
    assert "at least three workers" not in text
    assert "Every worker the task gives a contribution makes a consequential write" in text
    assert "every\ndecisive collection changes" in text  # cells nobody touches are rejected
    assert "write only in an app that worker holds" in text  # held-app rule


def test_the_golden_skill_warns_about_the_ids_dangling_references_rejects():
    """2 of 19 golden repair rounds: "refers to msg-plastic-withdrawal, which the world never had
    and the golden never writes"."""
    assert "Never cite" in skill("company-task-golden")


def test_the_workflow_skill_states_the_all_or_none_contribution_rule():
    """4 of 64 rejected drafts: "every contribution names apps, or none does". The skill said the
    standard bundle "need not be listed", so a worker holding only the bundle returned apps=[]."""
    text = skill("company-workflows")
    assert "Either every contribution names apps or none does" in text


def test_the_workflow_skill_states_the_one_visit_per_phase_rule():
    """The single largest validator/prompt gap in the repo: 72 rejected drafts, 67 of the 70 under
    runs/ and 5 of the 64 under companies/. The skill invites "bounded rework" and outputs workers
    "build, assess and revise", which is a second visit to a phase, and never said that is refused."""
    assert "no phase twice" in skill("company-workflows")


def test_no_skill_still_carries_the_sentence_that_lost_its_verb():
    """ "In V2, when states applicability" read as a subordinate clause with no main clause; the
    backticks that made `when` a field name had been dropped."""
    assert "In V2, `when` states applicability" in skill("company-workflows")


def test_the_workflow_skill_tells_the_designer_where_the_record_bodies_are():
    """55 of 64 rejected drafts were empty batches whose selection_reason said the index "omits
    their contents" or that no read_seed was enabled. Supplying the tool is half the fix; the
    instruction that the bodies exist behind it is the other half."""
    text = skill("company-workflows")
    assert "`read_seed` returns the body at pointer `/app_id/collection/N`" in text
    assert "none of its body" in text


def flat(name):
    """One skill's text with every run of whitespace collapsed, so a reflowed line still matches."""
    return " ".join(skill(name).split())


def test_the_seeding_rule_about_the_harness_reaches_the_call_that_writes_the_desktops():
    """The "nothing knows how it is run" paragraph lived inside `## Call app_state`, so
    skill_for_call stripped it from world_core -- the call that writes `materials`. 7 of 60 seeded
    worlds published `GET /state?sid=` in a desktop file or a document, 24 named a VM or CUA, and
    the world-records repair call had to inline its own copy of the rule in the payload."""
    from company_envs.world.seed_calls import load_skill, skill_for_call

    instructions, _ = load_skill(Path(__file__).resolve().parents[1], "company-world-states")
    for call in ("world_core", "app_state", "bulk_specs"):
        text = skill_for_call(instructions, call)
        assert "does not know how it is run" in text, call
        assert "`stored_state`" in text and "`GET /state`" in text, call
        assert "does not know it is a benchmark" in text, call


def test_every_machine_tell_rule_is_stated_once_and_reaches_all_three_seeding_calls():
    """The four template shapes were written twice, once for app_state and once for bulk_specs,
    with different wording and different numbers -- the same two-copy arrangement that left
    company-workflows carrying 23 of the gate's 29 register terms."""
    from company_envs.world.seed_calls import load_skill, skill_for_call

    instructions, _ = load_skill(Path(__file__).resolve().parents[1], "company-world-states")
    assert instructions.count("## Records a person wrote, not a template stamped") == 1
    assert instructions.count("No em dash in a title") == 1
    assert instructions.count("Address every record to somebody here") == 1
    for call in ("world_core", "app_state", "bulk_specs"):
        text = skill_for_call(instructions, call)
        assert "No em dash in a title" in text, call
        assert "Address every record to somebody here" in text, call


def test_the_apps_tag_every_record_needs_is_asked_for_where_the_records_are_written():
    """`core_payload`'s output_contract requires `apps` on every entity and history record on every
    world_core call, but the skill put that sentence under "When tasks is empty" in the world-first
    section. 22 of 60 worlds projected under 3,000 characters into google_sheets_mock, and
    "group returned every collection empty" is 1,084 of the repair findings on disk."""
    text = skill("company-world-states")
    head = text.split("## Call `app_state`")[0]
    assert "names its destination apps" in head
    assert "not only for world-first calls" in head


def test_the_seeding_skill_states_no_limit_that_nothing_measures():
    """Do not retain the unmeasured core cap or the removed projection-size cliff."""
    text = skill("company-world-states")
    assert "60,000 characters" not in text
    assert "80,000 JSON characters" not in text


def test_the_seeding_skill_does_not_claim_the_calendar_draws_an_organizer():
    """The rule "do not restate the organizer, the app already draws it" was false: the
    google_calendar_mock Event object has no organizer field, so 12,366 of 17,465 seeded event
    descriptions opened with "Organizer:" and 214 events put it in a field the app ignores."""
    schema = (
        Path(__file__).resolve().parents[1] / "catalogs/app_schemas/google_calendar_mock.md"
    ).read_text()
    events = schema.split("### Event Object")[1].split("###")[0]
    assert "organizer" not in events.lower()  # the premise the old wording rested on
    text = flat("company-world-states")
    assert "no organizer" in text
    assert "put whoever called the meeting into `guests`" in text


def test_the_seeding_skill_carries_the_document_and_arithmetic_rules_its_checks_enforce():
    """check_documents and check_arithmetic are errors the skill never mentioned: 159 repair
    findings for thin documents and 107 for a total that disagrees with its own line items."""
    from company_envs.world import world_check

    text = flat("company-world-states")
    assert "under 300 characters of content" in text
    assert "30% of them" in text
    assert world_check.check_documents.__defaults__ == (300, 0.3)
    assert "the total equals the sum of its lines, within a cent" in text


def test_the_grader_skill_names_the_interface_keys_its_own_gate_refuses():
    """_validate_grader rejects a scored state check on a UI_STATE_KEYS collection, and the skill
    named none of them. 50 of 100 grader calls on disk are repair rounds, and selectedItems
    appears in five of them."""
    from company_envs.world.task_author import INTERFACE_STATE

    text = skill("company-task-grader")
    missing = sorted(key for key in INTERFACE_STATE if f"`{key}`" not in text)
    assert missing == [], missing
    # currentUser is refused as interface state AND as the signed-in account; a judgment or
    # artifact app_path may still read it, which the built-in instructions already allow.
    assert "`currentUser`" in text


def test_the_workflow_skill_forbids_the_interface_state_criteria_its_gate_rejects():
    """check_criteria_are_about_records raises on a success criterion whose observable is a sort
    order or an open view; no skill said so, and the grader then cannot express that criterion."""
    text = skill("company-workflows")
    for key in ("currentSortDirection", "selectedItems", "searchQuery", "sidebarOpen"):
        assert f"`{key}`" in text, key
    assert "is rejected, because every worker can set it without doing any of the work" in text


def test_the_golden_skill_excludes_the_interface_writes_its_gate_does_not_count():
    """contract_problems counts a worker's write only over keys outside UI_STATE_KEYS, so a
    trajectory whose patches touch selectedItems alone is "no consequential write"."""
    text = flat("company-task-golden")
    assert "patches that touch only\ninterface state" in skill("company-task-golden")
    for key in ("selectedItems", "uploadQueue", "undoStack"):
        assert key in text, key


def test_the_amplify_skill_speaks_to_the_model_and_not_to_an_operator():
    """470 of the skill's 1,501 bytes told a sandboxed model with shell_tool=false to read a docs
    path and run a CLI command, and the rest restated amplify.INSTRUCTIONS, which is prepended to
    every call. Nothing in it had ever run: there are no amplify calls on disk."""
    from company_envs.world.amplify import INSTRUCTIONS

    text = skill("company-task-amplify")
    assert "docs/RUNTIME_SURFACE.md" not in text
    assert "company-envs amplify" not in text
    assert "grader.template.json" not in text
    # Nothing the built-in instructions already say is repeated here.
    for sentence in ("Use ONLY allowed_records", "No executable code", "Never weaken checks"):
        assert sentence in INSTRUCTIONS and sentence not in text, sentence
    # What only the skill says: the two facts the payload's own comment calls out.
    assert "relative** pointer you put in `edit.path`" in text
    assert "base world and the rest of the company are not" in text


def test_the_grader_skill_does_not_document_the_harness_to_the_model_authoring_checks():
    """The skill spent ~1,900 bytes on author_grader/calibrate/grade signatures, reference.json
    file formats and "verify with doubles", none of which a model with shell_tool=false can act on,
    and another block addressed the separate task_judgment call, which never receives this skill."""
    text = skill("company-task-grader")
    for leak in (
        "author_grader(root, folder",
        "calibrate(folder, task_id",
        "grade(folder, task_id",
        "reference_states=",
        "HubClient",
        "runtime/sessions.json",
        "CalibrationError",
        "Verify with doubles",
    ):
        assert leak not in text, leak


def test_the_register_examples_shared_between_two_skills_stay_identical():
    """The Cedar Chair examples live in company-review and company-world-states and the Birch
    Instruments pair in company-workflows and company-plain-brief. Two hand-kept copies of one
    calibration is how the 29-term register list came to be 23 terms in one of them."""
    cedar = (
        (
            "Hi Jo, can we collect Mira's sofa on Friday afternoon? "
            "Please check the truck schedule before I promise her a time."
        ),
        (
            "Hi Mira, we can extend your sofa rental through Friday for $40. "
            "Would you like me to update your booking?"
        ),
        "Welcome to Cedar Chair. Check the booking calendar before offering a collection time.",
    )
    for example in cedar:
        assert example in flat("company-review"), example
        assert example in flat("company-world-states"), example
    birch = (
        (
            "Using records available September 8, prepare an internally approved response, "
            "reconcile the governing lease and statements, and integrate consequential "
            "specialist work within the approved authority limits."
        ),
        (
            "Birch Instruments is disputing last year's expense charges. Work out which charges "
            "they can actually challenge, whether they can take the overcharge off October's "
            "rent, and what their 2027 forecast should say. Get them a written answer by "
            "September 15."
        ),
    )
    for example in birch:
        assert example in flat("company-workflows"), example
        assert example in flat("company-plain-brief"), example


def test_the_research_skill_states_the_sector_and_naics_gate_that_refuses_a_dossier_first():
    """Catalogs.validate refuses a dossier whose NAICS prefix does not map to its economic sector
    before reading anything else, and neither Stage 1 skill mentioned NAICS at all: 5 research
    repair rounds under runs/ are that one message."""
    text = flat("company-research")
    assert "`naics` is a code whose longest matching prefix maps to that same sector" in text


def test_the_research_skill_keeps_the_episode_mechanics_out_of_the_dossiers_own_words():
    """company_view strips provenance keys but not `assumptions`, so the dossier's own text reaches
    every seeding prompt as company fact. 98 of 99 dossiers on disk carry "CUA/bash in separate
    personal VMs" and 221 assumption strings name computer-use automation, a virtual machine or
    bash -- one of them asking for onboarding that defines them. That is where the seeded worlds'
    harness documents came from."""
    text = flat("company-research")
    assert "That check is yours; it is never a fact about the company" in text
    assert "Assumptions are about scale, workload, history and policy." in text


def test_the_no_empty_batch_rule_is_in_the_skill_and_in_the_payload_it_must_agree_with():
    """55 of the 64 rejected drafts were empty batches. The rule now lives both in the skill and in
    workflows.seeded_world_rules, which is a hand-written string: if one loses it, this fails."""
    import company_envs.workflows as wf

    assert "Returning an empty batch is the worst available outcome" in skill("company-workflows")
    source = Path(wf.__file__).read_text()
    assert 'payload["seeded_world_rules"]' in source
    assert "do not return an " in source and "empty batch" in source
