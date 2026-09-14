"""Locate the host shell or guest browser selected by the installed Playwright version."""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def browser_executable(plan, *, guest=False):
    """Resolve the advertised installation, never a different cached version."""
    kind = "chromium" if guest else "chromium-headless-shell"
    section = re.search(rf"^[^\n]*\b{kind}(?![-\w])[^\n]*\n(.*?)(?:\n\n|\Z)", plan, re.MULTILINE | re.DOTALL)
    location = re.search(r"^\s*Install location:\s*(.+)$", section[1], re.MULTILINE) if section else None
    if location:
        root = Path(location[1].strip())
        for path in sorted(root.rglob("*")):
            if (
                path.name
                in (
                    {"chrome"} if guest else {"chrome-headless-shell", "headless_shell", "headless_shell.exe"}
                )
                and path.is_file()
                and os.access(path, os.X_OK)
            ):
                return path.resolve()
    raise RuntimeError(
        f"The matching {kind} is not installed. Run: uv run playwright install --with-deps {kind}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guest", action="store_true", help="Print the full Linux browser directory")
    args = parser.parse_args()
    kind = "chromium" if args.guest else "chromium-headless-shell"
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", kind],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    path = browser_executable(result.stdout, guest=args.guest)
    print(path.parent if args.guest else path)


if __name__ == "__main__":
    main()
