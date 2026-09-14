"""The visual gate: what the model is asked, how votes are counted, and what blocks a company."""

import json

import pytest
from pydantic import ValidationError

from company_envs.models import strict_schema
from company_envs.world import render_check as rc
from company_envs.world import visual_judge as vj


class Judge:
    """A model double: answers from a list, or raises when told to."""

    def __init__(self, answers, error=None):
        self.answers, self.error, self.calls = list(answers), error, []

    def call(self, job, prompt, response_type, *, images=(), **_kw):
        self.calls.append({"job": job, "prompt": prompt, "images": list(images)})
        if self.error:
            raise self.error
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        return response_type.model_validate(answer), {"job": job}


def verdict(*, blocking=False, reason=None, problems=(), looks_real=True, works=True, note="fine"):
    """One model answer. ``blocking`` is shorthand for naming a repairable fault."""
    return {
        "looks_real": looks_real,
        "works": works,
        "problems": list(problems),
        "blocking_reason": reason or ("broken" if blocking else "none"),
        "note": note,
    }


def test_the_verdict_schema_is_strict_enough_for_the_provider():
    """Codex refuses a schema with a free-form object; every field must be a named scalar or list."""
    schema = json.dumps(strict_schema(vj.VisualVerdict))
    assert '"additionalProperties": false' in schema
    assert '"additionalProperties": true' not in schema
    for field in ("looks_real", "works", "problems", "blocking_reason", "note"):
        assert field in schema
    assert all(reason in schema for reason in vj.BLOCKING_REASONS)
    with pytest.raises(ValidationError):
        vj.VisualVerdict.model_validate({**verdict(), "extra": 1})


def test_a_majority_of_votes_decides_and_every_problem_is_kept(tmp_path):
    image = tmp_path / "app.jpg"
    image.write_bytes(b"jpeg")
    judge = Judge(
        [
            verdict(blocking=True, problems=["main pane says Channel not found"], works=False),
            verdict(blocking=True, problems=["Channel not found in the main pane"], works=False),
            verdict(blocking=False, problems=[]),
        ]
    )
    result = vj.judge_screen(judge, image, "look at this", votes=3)
    assert result["blocking"] is True and result["ok"] is False and result["votes"] == 3
    assert result["works"] is False  # two of three said it does not work
    assert len(result["problems"]) == 2  # both wordings kept; they are not the same string
    assert [c["images"] for c in judge.calls] == [[str(image)]] * 3


def test_one_harsh_vote_does_not_fail_a_screen(tmp_path):
    image = tmp_path / "app.jpg"
    image.write_bytes(b"jpeg")
    judge = Judge([verdict(blocking=True, problems=["the logo is dull"]), verdict(), verdict()])
    result = vj.judge_screen(judge, image, "look", votes=3)
    assert result["ok"] is True and result["blocking"] is False
    assert result["problems"] == ["the logo is dull"]  # reported, not blocking


def test_a_judge_that_cannot_be_reached_is_not_a_verdict(tmp_path):
    image = tmp_path / "app.jpg"
    image.write_bytes(b"jpeg")
    judge = Judge([], error=RuntimeError("provider process failed"))
    result = vj.judge_screen(judge, image, "look", votes=2)
    assert result["ok"] is True and result["judged"] is False and result["votes"] == 0
    assert result["errors"] and "provider process failed" in result["errors"][0]


def test_free_text_is_bounded_so_one_long_answer_cannot_bloat_the_report(tmp_path):
    image = tmp_path / "app.jpg"
    image.write_bytes(b"jpeg")
    judge = Judge([verdict(problems=["x" * 900] * 20, note="y" * 900)])
    result = vj.judge_screen(judge, image, "look", votes=1)
    assert len(result["problems"]) <= 8 and len(result["problems"][0]) == vj.PROBLEM_LENGTH
    assert len(result["note"]) == vj.NOTE_LENGTH


def company(tmp_path, apps=("slack_mock", "gmail_mock")):
    folder = tmp_path / "companies" / "acme"
    (folder / "runtime" / "render").mkdir(parents=True)
    (folder / "company.json").write_text(
        json.dumps({"name": "Harborlight Hospice", "workers": [{"id": "boss", "title": "Manager"}]})
    )
    (folder / "world").mkdir(parents=True, exist_ok=True)
    (folder / "world/identities.json").write_text(json.dumps({"boss": {"gmail_mock": {"name": "Celia R"}}}))
    report = {"ok": True, "apps": {}}
    for app in apps:
        shot = folder / rc.SHOT_DIR / f"{app}.jpg"
        shot.write_bytes(b"jpeg")
        report["apps"][app] = {"ok": True, "screenshot": str(shot.relative_to(folder))}
    (folder / "runtime/RENDER.json").write_text(json.dumps(report))
    return folder


def test_the_prompt_names_the_company_the_app_and_the_person(tmp_path):
    folder = company(tmp_path, apps=("slack_mock",))
    judge = Judge([verdict()])
    report = vj.judge_folder(folder, judge, votes=1)
    prompt = judge.calls[0]["prompt"]
    assert "Harborlight Hospice" in prompt and "Celia R" in prompt and "Slack" in prompt
    assert "answer key" not in prompt.lower() and "golden" not in prompt.lower()
    assert report["ok"] is True and report["apps"]["slack_mock"]["screenshot"].endswith("slack_mock.jpg")


def test_one_blocking_app_fails_the_whole_world(tmp_path):
    folder = company(tmp_path)
    judge = Judge([verdict(blocking=True, problems=["Channel not found"])])
    report = vj.judge_folder(folder, judge, votes=1)
    assert report["ok"] is False
    assert all(not r["ok"] for r in report["apps"].values())
    assert not vj.visual_ok(report) and vj.visual_ok({"ok": True})
    assert not vj.visual_ok({}) and not vj.visual_ok({"ok": "yes"})
    lines = vj.format_lines(report)
    assert lines[-1].startswith("visual-judge FAILED") and "Channel not found" in lines[0]


def test_a_world_with_no_screenshots_is_judged_without_calls(tmp_path):
    folder = tmp_path / "companies" / "acme"
    (folder / "runtime").mkdir(parents=True)
    (folder / "runtime/RENDER.json").write_text(json.dumps({"ok": True, "apps": {}}))
    judge = Judge([verdict()])
    report = vj.judge_folder(folder, judge, votes=3)
    assert report["apps"] == {} and judge.calls == []
    # No call is made, and that is still not a pass. The render here succeeded and produced no
    # screens, so there was something to look at and nothing looked: unmeasured, and refused.
    assert report["outcome"] == "unmeasured" and report["ok"] is None
    assert vj.admissible(report) is False


def test_the_prompt_rules_out_the_false_alarms_that_blocked_six_worlds():
    """A clipped label, a quiet week and a shop name are not defects; the gate said they were."""
    for phrase in ("cut off to fit", "quiet week", "differs from the company's own name"):
        assert phrase in vj.APP_PROMPT
    assert "Set blocking_reason to the one thing" in vj.APP_PROMPT


def test_each_vote_is_a_question_the_cache_can_tell_apart(tmp_path):
    """Three identical prompts are one call; the other two are its cached answer, not judges."""
    image = tmp_path / "screen.jpg"
    image.write_bytes(b"jpeg")
    judge = Judge([verdict(blocking=True), verdict(), verdict()])
    result = vj.judge_screen(judge, image, "Look at this screen.", votes=3)
    asked = [call["prompt"] for call in judge.calls]
    assert len(asked) == 3
    assert len(set(asked)) == 3, "every ballot must differ, or the cache answers the later ones"
    assert all(call["prompt"].startswith("Look at this screen.") for call in judge.calls)
    # The majority still decides: one blocking reading out of three does not fail a company.
    assert result["blocking"] is False and result["votes"] == 3


def test_a_single_vote_asks_exactly_the_prompt_so_its_cached_answer_still_counts(tmp_path):
    image = tmp_path / "screen.jpg"
    image.write_bytes(b"jpeg")
    judge = Judge([verdict()])
    vj.judge_screen(judge, image, "Look at this screen.", votes=1)
    assert [call["prompt"] for call in judge.calls] == ["Look at this screen."]


def test_a_figure_the_judge_disbelieves_is_reported_and_does_not_block(tmp_path):
    """The judge blocked on business arithmetic no stage can repair.

    Measured blocking verdicts: "the net income figure could mislead someone about profitability"
    (bluestone's quickbooks) and "contradictory employee counts could mislead a manager"
    (childrens-aid's adp) held 2 of the 12 visually judged companies. Nothing downstream
    recomputes a company's books from a screenshot, so those are reports, not blocks.
    """
    image = tmp_path / "app.jpg"
    image.write_bytes(b"jpeg")
    money = "Net income shows $5,057.75 without a minus sign against $11,567.75 of expenses"
    judge = Judge([verdict(reason="none", problems=[money])] * 3)
    result = vj.judge_screen(judge, image, "look", votes=3)
    assert result["ok"] is True and result["blocking"] is False and result["problems"] == [money]
    # The four faults a repair round can act on still fail the screen, one at a time.
    for reason in ("empty", "foreign_records", "wrong_identity", "broken"):
        harsh = Judge([verdict(reason=reason, problems=[reason])] * 3)
        blocked = vj.judge_screen(harsh, image, "look", votes=3)
        assert blocked["blocking"] is True and blocked["blocking_reason"] == reason, reason


def test_the_prompt_names_every_repairable_fault_and_rules_out_the_arithmetic():
    for reason in vj.BLOCKING_REASONS:
        assert f'"{reason}"' in vj.APP_PROMPT
    assert "does not add up" in vj.APP_PROMPT and "held back forever" in vj.APP_PROMPT
    assert all(f'"{reason}"' in vj.DESKTOP_PROMPT for reason in vj.BLOCKING_REASONS)


def test_the_judge_is_handed_what_the_render_check_already_counted(tmp_path):
    """The judge saw a screenshot and a short prompt while the render check had the numbers.

    Three judges voted looks_real and works on a Drive showing 1 of 400 of the company's own
    values in about 210 visible characters.
    """
    folder = company(tmp_path, apps=("google_drive_mock",))
    report = json.loads((folder / "runtime/RENDER.json").read_text())
    report["apps"]["google_drive_mock"] |= {
        "seeded_strings_seen": "1/400",
        "text_chars": 212,
        "controls": 19,
        "route": "/recent",
        "broken_images": ["https://cdn.example/a.png"],
        "empty_images": 2,
    }
    (folder / "runtime/RENDER.json").write_text(json.dumps(report))
    judge = Judge([verdict()])
    vj.judge_folder(folder, judge, votes=1)
    prompt = judge.calls[0]["prompt"]
    assert "1 of 400 of this company's own record values are visible" in prompt
    assert "212 characters of visible text, 19 things to click" in prompt
    assert "/recent page" in prompt
    assert "1 image(s) the browser could not draw" in prompt
    assert "2 image(s) the app drew from its own empty default" in prompt
    # A screen with no measurements says so rather than inventing a number.
    assert vj.measured({}) == "- (nothing was measured for this screen)"
    assert "0 characters of visible text" in vj.measured({"seeded_strings_seen": "0/0"})


def test_a_judgement_of_no_screens_says_unmeasured_instead_of_passing(tmp_path, monkeypatch):
    """`report = {"ok": True, ...}` then `ok = ok and result["ok"]` per screen: with no screens the
    report was a visual **pass** over a world nobody had looked at. It is the same line barrier.py had
    -- a collision is reported when it is found, so finding nothing meant two different things.

    The two empty judgements are different runs. A host without Chrome rendered nothing, so this
    could not be taken either: the fault is forwarded from RENDER.json and ``admissible`` lets it
    through, because no stage installs a browser and the VM stage looks at these screens in a real
    one. A judgement with no screens behind a render that *succeeded* is refused instead -- there was
    something to look at and nothing looked.
    """
    from company_envs.storage import write
    from company_envs.world.visual_judge import judge_folder

    folder = tmp_path / "company"
    (folder / "runtime").mkdir(parents=True)
    write(folder / "company.json", {"name": "Demo", "workers": []})
    write(folder / "runtime/RENDER.json", {"ok": True, "skipped": "no chrome on this host", "apps": {}})
    report = judge_folder(folder, models=None)
    assert report["apps"] == {} and report["desktops"] == {}
    assert report["outcome"] == "faulted", "the host failed; the world was never looked at"
    assert "no chrome on this host" in report["skipped"], "which kind of nothing, from the render"
    assert report["ok"] is False and vj.admissible(report) is True, "recorded, and not a blocker"


def test_a_visual_verdict_is_stale_when_the_render_code_that_fed_it_changes(tmp_path):
    """render_check.py and visual_judge.py were not inputs to their own gates, and the two code lists
    were hand-copied tuples neither of which named hub_app.py -- so the storage-quota fix could not
    restale a RENDER.json or a VISUAL.json.

    A visual verdict is taken *of* the screenshots the render produced, so anything that changes what
    the browser draws changes what the judge sees. Reusing render_check's list rather than restating
    it is what keeps the two from drifting apart again.
    """
    from company_envs.storage import decided_by, write
    from company_envs.world import render_check as rc

    assert vj.DECIDES_THIS[0].name == "visual_judge.py"
    assert set(rc.DECIDES_THIS) < set(vj.DECIDES_THIS), "everything the render depends on, and itself"
    assert all(p.is_file() for p in vj.DECIDES_THIS), "a typo digests as absence and never restales"
    assert vj.current({}) is False, "no digest is stale: the migration"
    assert vj.current({"decided_by": decided_by(vj.DECIDES_THIS)}) is True
    # A render-code change restales the judgement as well, which the separate tuples did not do.
    assert vj.current({"decided_by": decided_by((vj.DECIDES_THIS[0],))}) is False
    assert vj.current({"decided_by": decided_by(rc.DECIDES_THIS)}) is False, "and so does a judge change"

    folder = tmp_path / "companies" / "acme"
    (folder / "runtime").mkdir(parents=True)
    write(folder / "runtime/RENDER.json", {"ok": False, "skipped": "no chrome on this host", "apps": {}})
    write(folder / "company.json", {"name": "Demo", "workers": []})
    assert vj.current(vj.judge_folder(folder, models=None)) is True, "the host-fault report carries it"
