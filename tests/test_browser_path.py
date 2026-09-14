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
    assert browser_path.browser_executable(plan) == selected.resolve()


def test_missing_shell_reports_install_command_instead_of_selecting_another_browser(tmp_path):
    plan = (
        f"Chrome Headless Shell (playwright chromium-headless-shell v1234)\n  Install location: {tmp_path}\n"
    )
    with pytest.raises(RuntimeError, match="install --with-deps chromium-headless-shell"):
        browser_path.browser_executable(plan)


def test_guest_browser_never_selects_the_headless_distribution(tmp_path):
    shell, full = tmp_path / "shell" / "chrome", tmp_path / "full" / "chrome-linux64" / "chrome"
    for path in (shell, full):
        path.parent.mkdir(parents=True)
        path.write_text("browser")
        path.chmod(0o755)
    plan = (
        f"Chrome Headless Shell (playwright chromium-headless-shell v1234)\n  Install location: {shell.parent}\n\n"
        f"Chrome for Testing (playwright chromium v1234)\n  Install location: {full.parent.parent}\n\n"
    )
    assert browser_path.browser_executable(plan, guest=True) == full.resolve()
