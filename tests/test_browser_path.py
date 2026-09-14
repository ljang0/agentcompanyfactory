"""Browser setup selects the CLI headless shell matching the locked dependency."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "browser_path", Path(__file__).resolve().parents[1] / "scripts/browser_path.py"
)
browser_path = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser_path)


def test_selects_the_requested_shell_even_with_spaces_and_other_cached_versions(tmp_path):
    old = tmp_path / "cache with spaces" / "old" / "chrome-headless-shell"
    selected = tmp_path / "cache with spaces" / "current" / "bin" / "chrome-headless-shell"
    for path in (old, selected):
        path.parent.mkdir(parents=True)
        path.write_text("shell")
        path.chmod(0o755)
    plan = (
        f"Chrome Headless Shell (playwright chromium-headless-shell v1234)\n"
        f"  Install location:    {selected.parent.parent}\n"
        "  Download url:        https://example.com/shell.zip\n\n"
        f"FFmpeg\n  Install location: {old.parent}\n"
    )
    assert browser_path.headless_shell(plan) == selected.resolve()


def test_missing_shell_reports_install_command_instead_of_selecting_another_browser(tmp_path):
    plan = (
        f"Chrome Headless Shell (playwright chromium-headless-shell v1234)\n  Install location: {tmp_path}\n"
    )
    with pytest.raises(RuntimeError, match="install --with-deps chromium-headless-shell"):
        browser_path.headless_shell(plan)
