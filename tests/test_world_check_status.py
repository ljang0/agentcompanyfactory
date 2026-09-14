"""Cross-app issue status comparison respects each native lifecycle."""

import pytest

from company_envs.world.world_check import check_facts


@pytest.mark.parametrize("github", ["open", "closed"])
@pytest.mark.parametrize("jira", ["To Do", "In Progress", "In Review", "Done"])
@pytest.mark.parametrize("reverse", [False, True])
def test_github_and_jira_agree_on_completion(github, jira, reverse):
    pairs = [("github_mock", github), ("jira_mock", jira)]
    if reverse:
        pairs.reverse()
    states = {app: {"issues": [{"id": "work-1", "status": status}]} for app, status in pairs}
    findings = check_facts({}, states)
    conflict = (github == "closed") != (jira == "Done")
    assert len(findings) == int(conflict)
    if conflict:
        assert "work-1.status" in findings[0]["message"]


def test_issue_status_mapping_does_not_hide_other_fact_changes():
    states = {
        "github_mock": {"issues": [{"id": "work-1", "status": "open", "title": "Keep account scope"}]},
        "jira_mock": {"issues": [{"id": "work-1", "status": "In Review", "title": "Remove account scope"}]},
    }
    findings = check_facts({}, states)
    assert len(findings) == 1 and "work-1.title" in findings[0]["message"]


def test_issue_mapping_does_not_apply_to_accounts_or_unknown_statuses():
    accounts = {
        "github_mock": {"accounts": [{"id": "account-1", "status": "open"}]},
        "jira_mock": {"accounts": [{"id": "account-1", "status": "in_progress"}]},
    }
    assert len(check_facts({}, accounts)) == 1
    issues = {
        "github_mock": {"issues": [{"id": "work-1", "status": "closed"}]},
        "jira_mock": {"issues": [{"id": "work-1", "status": "Unknown"}]},
    }
    assert len(check_facts({}, issues)) == 1
