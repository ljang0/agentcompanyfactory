"""Validate the exact Stage 3 handoff before review, serving or freezing it."""

from pathlib import Path

from company_envs.storage import digest, read

from .blueprint import load_core
from .population import additive_world
from .population_amendments import corrected_world


def population_snapshot(root, folder, *, require_complete=True):
    folder = Path(folder)
    load_core(root, folder)
    manifest = read(folder / "world/POPULATION.json")
    allowed = (
        {"populated_for_review"} if require_complete else {"populated_for_review", "population_needs_repair"}
    )
    if manifest.get("schema_version") != 2 or manifest.get("status") not in allowed:
        raise ValueError("Stage 3 must complete before world acceptance or serving")
    if require_complete and manifest.get("native_errors"):
        raise ValueError("population has unresolved native errors")
    if manifest["core_hashes"] != read(folder / "world/CORE.json")["hashes"]:
        raise ValueError("population belongs to another accepted core")

    def local(relative):
        path = (folder / relative).resolve()
        if not path.is_relative_to(folder.resolve()) or not path.is_file():
            raise ValueError(f"invalid population path: {relative}")
        return path

    for aid, entry in manifest["states"].items():
        if digest(read(local(entry["path"]))) != entry["state_hash"]:
            raise ValueError(f"population state drift: {aid}")
    for relative, expected in manifest["artifacts"].items():
        if digest(local(relative).read_bytes()) != expected:
            raise ValueError(f"population artifact drift: {relative}")
    base = read(folder / "world/world.json")
    extension = folder / "world/population/EXTENSION.json"
    world = corrected_world(
        folder,
        additive_world(base, read(extension) if extension.exists() else {"parent_world_hash": digest(base)}),
    )
    if read(local(manifest["effective_world"])) != world:
        raise ValueError("effective world differs from explicit population sources")
    return {
        "manifest_hash": digest(manifest),
        "states": {a: e["state_hash"] for a, e in manifest["states"].items()},
        "artifacts": manifest["artifacts"],
        "world_hash": digest(world),
    }
