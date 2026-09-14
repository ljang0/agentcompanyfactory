"""Calls rotate across logged-in Codex account directories; receipts record which one served."""

from company_envs import models


def test_only_logged_in_homes_count_and_rotation_alternates(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for home in (a, b, c):
        home.mkdir()
    (a / "auth.json").write_text("{}")
    (b / "auth.json").write_text("{}")
    config = {"models": {"codex_homes": [str(a), str(b), str(c), str(tmp_path / "missing")]}}
    assert models.codex_homes(config) == [str(a), str(b)]
    picks = {models.next_codex_home(config) for _ in range(4)}
    assert picks == {str(a), str(b)}
    assert models.next_codex_home({"models": {}}) is None
    assert models.next_codex_home({"models": {"codex_homes": [str(c)]}}) is None


def test_call_environment_carries_the_account_and_drops_claude_variables(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_X", "1")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = models.call_environment({"CODEX_HOME": "/x/acct-b"})
    assert env["CODEX_HOME"] == "/x/acct-b" and env["PATH"] == "/usr/bin" and "CLAUDE_CODE_X" not in env
    assert (
        "CODEX_HOME" not in models.call_environment()
        or models.call_environment()["CODEX_HOME"] != "/x/acct-b"
    )
