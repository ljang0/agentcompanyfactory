"""Record exactly what a frozen pipeline consists of.

    .venv/bin/python scripts/freeze_manifest.py [out.json]

Commit, dirty files, config hash, every skill's hash, the model identifiers in config, the
Codex CLI version, the VM base image and browser hashes (size + mtime when hashing 7 GB is
not worth it, a sha256 when --hash-images is given), and the Python and package versions.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()


def main():
    hash_images = "--hash-images" in sys.argv
    out = next(
        (Path(a) for a in sys.argv[1:] if not a.startswith("--")),
        ROOT / "experiments" / "FREEZE-MANIFEST.json",
    )
    config = tomllib.loads((ROOT / "config.toml").read_text())
    design = config.get("design", {})
    images = {}
    for key in ("vm_base_image", "vm_browser_dir", "base_image", "browser_dir"):
        value = design.get(key) or config.get(key)
        if value:
            path = Path(str(value)).expanduser()
            if path.is_file():
                st = path.stat()
                images[key] = {"path": str(path), "bytes": st.st_size, "mtime": int(st.st_mtime)}
                if hash_images:
                    images[key]["sha256"] = sha256(path)
            elif path.is_dir():
                images[key] = {"path": str(path), "files": sum(1 for _ in path.rglob("*"))}
    manifest = {
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git("rev-parse", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": [
            line
            for line in git(
                "status", "--short", "src", "scripts", "tests", ".agents", "config.toml"
            ).splitlines()
        ],
        "config_sha256": sha256(ROOT / "config.toml"),
        "models": config.get("models", {}),
        "skills": {
            p.parent.name: sha256(p) for p in sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md"))
        },
        "codex_cli": subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, check=False
        ).stdout.strip(),
        "python": platform.python_version(),
        "packages": subprocess.run(
            [sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, check=False
        ).stdout.splitlines(),
        "vm_images": images,
    }
    out.write_text(json.dumps(manifest, indent=1))
    print(
        f"wrote {out} ({manifest['commit'][:8]}, {len(manifest['skills'])} skills, dirty={len(manifest['dirty'])})"
    )


if __name__ == "__main__":
    main()
