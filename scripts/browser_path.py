"""Print the headless-shell executable selected by the installed Playwright version."""

import os
import re
import subprocess
import sys
from pathlib import Path

INSTALL = "uv run playwright install --with-deps chromium-headless-shell"


def headless_shell(plan):
    """Resolve the advertised shell installation, never a different cached version."""
    section = re.search(
        r"^[^\n]*chromium-headless-shell[^\n]*\n(.*?)(?:\n\n|\Z)", plan, re.MULTILINE | re.DOTALL
    )
    location = re.search(r"^\s*Install location:\s*(.+)$", section[1], re.MULTILINE) if section else None
    if location:
        root = Path(location[1].strip())
        for path in sorted(root.rglob("*")):
            if (
                path.name in {"chrome-headless-shell", "headless_shell", "headless_shell.exe"}
                and path.is_file()
                and os.access(path, os.X_OK)
            ):
                return path.resolve()
    raise RuntimeError(f"The matching headless shell is not installed. Run: {INSTALL}")


def main():
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium-headless-shell"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    print(headless_shell(result.stdout))


if __name__ == "__main__":
    main()
