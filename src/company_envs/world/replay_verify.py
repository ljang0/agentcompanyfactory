"""Rebuild a world from cached model answers and compare its files byte for byte.

SEED.json and BULK.json are run receipts with wall-clock timestamps. They, call
caches, and receipt files outside worker materials are excluded. CHECKS.json,
REVIEW.json, app states, identities and every worker material are compared.
Current company inputs, config, schemas and skills must still match the cache.
"""

import json
import shutil
from pathlib import Path

from company_envs.config import load_config
from company_envs.models import Models
from company_envs.storage import digest, read, write

from .bulk_layer import add_bulk
from .state_seed import WorldCore, seed_world


class _ReplayModels(Models):
    """Keep the original cohort context while checking every other prompt input."""

    def __init__(self, config, world, seed):
        super().__init__(config, world, cache_only=True)
        receipt = seed["receipts"]["world_core"]
        prompt = world / "calls" / receipt["call_id"] / f"attempt-{receipt['attempt']:03d}" / "prompt.txt"
        self.core_input = json.loads(prompt.read_text().rsplit("\n", 1)[1])

    def call(self, job, prompt, response_type, **kwargs):
        if response_type is WorldCore:
            instructions, body = prompt.rsplit("\n", 1)
            payload = json.loads(body)
            # Other companies may have been seeded since this call. Their names
            # were inputs then, so use the lists from that recorded request.
            for key in ("avoid_person_names", "avoid_emails"):
                if key in self.core_input:
                    payload[key] = self.core_input[key]
                else:
                    payload.pop(key, None)
            prompt = instructions + "\n" + json.dumps(payload, ensure_ascii=False)
        return super().call(job, prompt, response_type, **kwargs)


def _hashes(world):
    hashes = {}
    for path in sorted(world.rglob("*")):
        relative = path.relative_to(world)
        if not path.is_file():
            continue
        if relative.parts[0] != "materials":
            if any(part in ("calls", "receipts") for part in relative.parts[:-1]):
                continue
            if (
                relative.as_posix() in ("SEED.json", "BULK.json", "avoid.json")
                or path.name in ("receipt.json", "receipts.json")
                or path.name.endswith(".receipt.json")
            ):
                continue
        hashes[f"world/{relative.as_posix()}"] = digest(path.read_bytes())
    return hashes


def replay_verify(root, folder, out_dir):
    """Seed a separate company folder; cache misses raise without calling a model."""
    root, folder, out_dir = Path(root).resolve(), Path(folder).resolve(), Path(out_dir).resolve()
    if out_dir == folder or out_dir.is_relative_to(folder) or folder.is_relative_to(out_dir):
        raise ValueError("replay output and original company must be separate folders")
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise ValueError("replay output must be a new or empty directory")
    seed = read(folder / "world/SEED.json")
    review_path = folder / "world/REVIEW.json"
    rounds = read(review_path).get("rounds", []) if review_path.is_file() else []
    config = load_config(root)
    config.setdefault("models", {}).setdefault("world_states", config["models"].get("expand"))
    config.setdefault("generation", {}).setdefault("completion_timeout_seconds", 1500)
    if rounds:
        config.setdefault("design", {})["review_votes"] = rounds[0].get("receipt", {}).get("votes", 1)
    models = _ReplayModels(config, folder / "world", seed)
    original = _hashes(folder / "world")
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs = [folder / name for name in ("MANIFEST.json", "company.json", "apps.json")]
    inputs += (
        [folder / "world" / "avoid.json"] if (folder / "world" / "avoid.json").is_file() else []
    )  # frozen input
    inputs += sorted(folder.glob("tasks/*/workflow.json"))
    inputs += sorted(folder.glob("tasks/*/assignment.json"))
    for path in inputs:
        target = out_dir / path.relative_to(folder)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    seed_world(
        root,
        out_dir,
        models=models,
        review_rounds=max(1, max((r["round"] for r in rounds), default=0))
        if rounds or seed.get("review")
        else 0,
    )
    if (folder / "world/BULK.json").is_file():
        add_bulk(root, out_dir, models=Models(config, folder / "world/bulk", cache_only=True))
    replayed = _hashes(out_dir / "world")
    differing = sorted(
        path for path in original.keys() | replayed.keys() if original.get(path) != replayed.get(path)
    )
    report = {"identical": not differing, "differing": differing}
    write(out_dir / "REPLAY.json", report)
    return report
