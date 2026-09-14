"""The scoreboard reads the review verdict from SEED.json's explicit field when a seed has one."""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cohort_scoreboard.py"
spec = importlib.util.spec_from_file_location("cohort_scoreboard", SCRIPT)
scoreboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scoreboard)


def _seed(root, company, seed, review=None):
    world = root / "companies" / company / "world"
    world.mkdir(parents=True)
    (world / "SEED.json").write_text(json.dumps(seed))
    if review is not None:
        (world / "REVIEW.json").write_text(json.dumps({"rounds": [{"verdict": {"verdict": review}}]}))


def test_accepted_prefers_the_explicit_verdict_and_falls_back_to_review_json(tmp_path, monkeypatch):
    monkeypatch.setattr(scoreboard, "ROOT", tmp_path)
    # The reviewers accepted but the mechanical rules failed: status says failed, the verdict says accept.
    _seed(
        tmp_path, "a", {"status": "seeded_review_failed", "review_verdict": "accept", "mechanical_ok": False}
    )
    _seed(tmp_path, "b", {"status": "seeded_review_failed", "review_verdict": "revise"}, review="accept")
    _seed(tmp_path, "c", {"status": "seeded_reviewed"}, review="accept")
    _seed(tmp_path, "d", {"status": "seeded_not_verified", "review_verdict": None})
    assert scoreboard.world("a")["accepted"] is True
    assert scoreboard.world("b")["accepted"] is False  # the explicit field wins over an older REVIEW.json
    assert scoreboard.world("c")["accepted"] is True  # older seeds: REVIEW.json's last round
    assert scoreboard.world("d")["accepted"] is False
    assert scoreboard.world("missing")["accepted"] is False and scoreboard.world("missing")["seeded"] is False
