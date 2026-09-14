"""scripts/check.sh: the suite's exit code gates the commit, and only the named paths are in it.

The script is exercised in a throwaway repository with stub ruff and pytest, because what is under
test is which files reach the commit and what stops one -- not ruff and not the suite.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts" / "check.sh"


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=_env()
    ).stdout


def _env():
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


def stub(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def tree(tmp_path):
    """A repository shaped like this one: the gate, stub tools, a remote, and one commit to build on.

    The stub python answers the gate's one pytest call and the stub ruff its lint calls; a pytest-rc
    or ruff-rc file in the repo decides their exit codes, so a red suite is a file to write rather
    than a test to break.
    """
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    (repo / "scripts").mkdir(parents=True)
    for directory in ("src", "tests", ".agents"):
        (repo / directory).mkdir()
    shutil.copy2(GATE, repo / "scripts" / "check.sh")
    stub(repo / ".venv/bin/ruff", 'exit "$(cat ruff-rc 2>/dev/null || echo 0)"')
    stub(
        repo / ".venv/bin/python",
        'echo "1 passed"\nexit "$(cat pytest-rc 2>/dev/null || echo 0)"',
    )
    (repo / "src" / "kept.py").write_text("a = 1\n")
    # Every directory --all names has to exist and be tracked, as they are in the real tree: git
    # refuses a pathspec that matches nothing, and an empty directory matches nothing.
    (repo / "tests" / "test_base.py").write_text("def test_base():\n    pass\n")
    (repo / ".agents" / "skills.md").write_text("skills\n")
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=_env())
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=_env())
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "add", "-A", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "push", "-q", "origin", "main")
    return repo


def run(repo, *args):
    return subprocess.run(
        ["bash", "scripts/check.sh", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_env(),
        check=False,
    )


def head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def changed(repo, ref="HEAD"):
    """The files one commit touched, or [] when that ref is still the fixture's base commit."""
    if git(repo, "log", "--format=%s", "-1", ref).strip() == "base":
        return []
    return sorted(git(repo, "show", "--name-only", "--format=", ref).split())


def test_only_the_named_paths_are_committed(tree):
    """`git add -A src scripts tests .agents` staged whatever every agent had touched. On 2026-09-10
    one commit pulled in another agent's 15 new tests without the hub_patches.json they read, leaving
    HEAD red, and reverted a third agent's appended tests in four files."""
    (tree / "src" / "mine.py").write_text("mine = 1\n")
    (tree / "src" / "theirs.py").write_text("theirs = 1\n")  # another agent, mid-edit
    (tree / "tests" / "test_theirs.py").write_text("def test_x():\n    pass\n")
    result = run(tree, "my change", "src/mine.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert changed(tree) == ["src/mine.py"]
    assert git(tree, "status", "--porcelain").count("??") == 2, "their files are untouched and unstaged"
    assert "not committed (still dirty):" in result.stdout, "what the narrowing left behind is named"
    assert "src/theirs.py" in result.stdout and "tests/test_theirs.py" in result.stdout
    # The remote got that commit and nothing else.
    assert changed(tree, "origin/main") == ["src/mine.py"]
    # Work another agent had already staged in the index does not ride along with the next commit.
    git(tree, "add", "src/theirs.py")
    (tree / "src" / "second.py").write_text("second = 1\n")
    assert run(tree, "second change", "src/second.py").returncode == 0
    assert changed(tree) == ["src/second.py"]


def test_a_message_with_no_paths_commits_nothing(tree):
    """The old call signature was one argument, and it meant "commit everything". A caller who still
    types that gets a refusal naming what to type instead, never a commit of the whole tree."""
    (tree / "src" / "mine.py").write_text("mine = 1\n")
    result = run(tree, "my change")
    assert result.returncode == 2 and "name what to commit" in result.stdout
    assert changed(tree) == [] and git(tree, "log", "--oneline").count("\n") == 1
    # --all is how the whole tree is asked for, and it still has to pass the same gate.
    assert run(tree, "--all", "everything").returncode == 0
    assert changed(tree) == ["src/mine.py"]


def test_a_failing_suite_commits_nothing_whatever_its_output_says(tree):
    """Why the script exists: two commits landed with failing tests behind a grep that hid pytest's
    exit code. The stub prints a passing line and exits 1, so only the exit status can stop it."""
    (tree / "pytest-rc").write_text("1")
    (tree / "src" / "mine.py").write_text("mine = 1\n")
    result = run(tree, "my change", "src/mine.py")
    assert result.returncode == 1 and "nothing committed" in result.stdout
    assert changed(tree) == []
    (tree / "pytest-rc").write_text("0")
    (tree / "ruff-rc").write_text("1")
    result = run(tree, "my change", "src/mine.py")
    assert result.returncode == 1 and "lint failed" in result.stdout
    assert changed(tree) == []


def test_a_path_that_does_not_exist_stops_before_anything_runs(tree):
    """A typo in a path is the one way the narrowed gate could commit less than the caller meant, so
    it is refused by name rather than committing whatever else was listed."""
    (tree / "src" / "mine.py").write_text("mine = 1\n")
    result = run(tree, "my change", "src/mine.py", "src/typo.py")
    assert result.returncode == 2 and "no such path: src/typo.py" in result.stdout
    assert changed(tree) == []
    result = run(tree, "my change", "src/kept.py")  # named, but unchanged
    assert result.returncode == 2 and "nothing staged" in result.stdout
    assert changed(tree) == []


def test_ruff_rewrites_only_the_named_paths(tree):
    """--fix and the formatter used to run over all of src scripts tests. Rewriting a file this commit
    does not own is the same defect as staging it, and worse in one way: the owner's next edit lands
    on a file that moved under them. The judging pass still covers the whole tree."""
    stub(tree / ".venv/bin/ruff", 'echo "$*" >> ruff-calls\nexit 0')
    (tree / "src" / "mine.py").write_text("mine = 1\n")
    assert run(tree, "my change", "src/mine.py", "config.toml").returncode == 2  # no config.toml here
    (tree / "config.toml").write_text("x = 1\n")
    assert run(tree, "my change", "src/mine.py", "config.toml").returncode == 0
    calls = [line.split() for line in (tree / "ruff-calls").read_text().splitlines()]
    fixed = [c for c in calls if "--fix" in c or "format" in c]
    assert fixed, "the caller's own paths are still fixed and formatted"
    for call in fixed:
        assert "src/mine.py" in call
        assert "config.toml" not in call, "ruff is never handed a file it cannot parse"
        assert not {"tests", ".agents"} & set(call), "another agent's files are judged, never rewritten"
    assert any("check" in c and "src" in c and "scripts" in c and "tests" in c for c in calls)
    assert changed(tree) == ["config.toml", "src/mine.py"]


def test_the_runbook_command_commits_a_path_outside_the_four_directories(tree):
    """What the freeze runbook asks for. `git add -A src scripts tests .agents` does not reach
    experiments/, so the manifest was never in the commit that claimed to carry it -- no
    FREEZE-MANIFEST.json is tracked in this repository today. Naming the path is what makes it
    committable, and the runbook now names it."""
    manifest = tree / "experiments" / "FREEZE-MANIFEST.json"
    manifest.parent.mkdir()
    manifest.write_text('{"commit": "abc"}\n')
    assert run(tree, "--all", "everything").returncode == 2, "the old four directories do not reach it"
    result = run(tree, "Freeze manifest for cohort NAME", "experiments/FREEZE-MANIFEST.json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert changed(tree) == ["experiments/FREEZE-MANIFEST.json"]


CHECK_SH = re.compile(r"scripts/check\.sh(?P<args>[^\n`#]*)")


def documented_invocations():
    """Every `scripts/check.sh ...` the docs tell a reader to type, with its arguments."""
    root = Path(__file__).resolve().parents[1]
    for doc in ("README.md", "docs/GATE4-RUNBOOK.md"):
        for match in CHECK_SH.finditer((root / doc).read_text()):
            yield doc, match.group("args").strip()


def test_no_document_shows_a_command_that_would_refuse():
    """A runbook that tells someone to type a command that no longer works is worse than no runbook.
    The gate stopped committing on a message alone, so any `scripts/check.sh "message"` left in the
    docs now prints an instruction instead of committing. This pins every documented invocation to a
    form the gate accepts: no message, --all, or a message followed by at least one path."""
    shown = list(documented_invocations())
    assert shown, "the docs describe the commit gate; if they stop, this test has to be told"
    for doc, args in shown:
        if not args or args.startswith("--all"):
            continue
        quoted = re.match(r'"[^"]*"|PATH|\S+', args)
        assert quoted, f"{doc}: cannot read the arguments of `scripts/check.sh {args}`"
        rest = args[quoted.end() :].strip()
        assert rest, f"{doc}: `scripts/check.sh {args}` names no path, so the gate would refuse it"
