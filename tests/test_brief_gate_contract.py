"""The brief gate and the instructions that must satisfy it say the same thing."""

import re
from pathlib import Path

import pytest

from company_envs.workflows import (
    REGISTER_TERMS,
    RELATIVE_TIME,
    RELATIVE_TIME_EXAMPLES,
    brief_gate,
    check_plain_english,
)

SKILLS = Path(__file__).resolve().parents[1] / ".agents" / "skills"


def test_the_payload_names_every_word_the_gate_rejects():
    """company-workflows hand-copied the list and carried 23 of the 29 terms, so a brief rejected
    for downstream, leverage, operationalize, evidentiary, retention or "within the approved" was
    rejected on a word its instructions never named."""
    assert len(REGISTER_TERMS) == 29
    assert set(brief_gate()["register_terms"]) == set(REGISTER_TERMS)


def test_no_skill_keeps_its_own_copy_of_the_word_list_to_drift_from():
    """Two copies of 29 terms in prose is how 6 went missing; the authoring skill now points at
    the generated field. company-plain-brief keeps its list on purpose: it is writing guidance,
    complete, and stricter than the gate."""
    workflows_skill = (SKILLS / "company-workflows" / "SKILL.md").read_text()
    assert "brief_gate" in workflows_skill
    assert "reconcile, evidence" not in workflows_skill  # the enumeration that lost six terms
    plain = (SKILLS / "company-plain-brief" / "SKILL.md").read_text().lower()
    assert len([t for t in REGISTER_TERMS if t in plain]) == 29


def test_every_relative_time_form_the_payload_names_is_one_the_gate_rejects():
    """The skill named three forms of about twenty. "end of the week" and "by Friday" were
    rejected and appeared in no instruction."""
    assert len(RELATIVE_TIME_EXAMPLES) == 10
    for phrase in RELATIVE_TIME_EXAMPLES:
        assert RELATIVE_TIME.search(f"Send the answer {phrase}."), phrase
    assert not RELATIVE_TIME.search("Send the answer by Friday, September 18.")


def test_the_thresholds_the_payload_states_are_the_ones_the_gate_applies():
    gate = brief_gate()

    class W:
        id = "x_o1"
        title = "Answer a client's expense dispute"
        brief = "Birch Instruments disputes the charges. " * 3

    assert check_plain_english(W())  # the plain brief passes
    W.brief = " ".join(["word"] * (gate["max_mean_words_per_sentence"] + 1)) + "."
    with pytest.raises(ValueError, match="words per sentence"):
        check_plain_english(W())
    W.brief = "Birch disputes it. " + " ".join(["documentation"] * 3 + ["word"] * 12) + "."
    share = 3 / len(re.findall(r"[A-Za-z][A-Za-z'-]*", W.brief))
    assert share > gate["max_share_of_words_over_eleven_letters"]
    with pytest.raises(ValueError, match="long abstract words"):
        check_plain_english(W())


def test_one_register_term_in_a_title_sinks_it_while_two_in_a_brief_do_not():
    """The gate is stricter on titles than on briefs, and neither skill said so."""
    gate = brief_gate()
    assert gate["register_terms_that_reject_a_title"] == 1
    assert gate["register_terms_that_reject_a_brief"] == 3

    class W:
        id = "x_o1"
        title = "Answer a client's expense dispute"
        brief = "Reconcile the charges with Birch. Retain what you find. Send an answer by September 15."

    assert len(check_plain_english(W())["register_terms"]) == 2
    W.title = "Reconcile the expense dispute"
    with pytest.raises(ValueError, match="titles say what the team is doing"):
        check_plain_english(W())
