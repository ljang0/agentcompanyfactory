"""Explicit corrections layered over an immutable accepted canonical checkpoint."""

from copy import deepcopy
from pathlib import Path

from company_envs.storage import digest, read


def corrected_world(folder, world):
    path = Path(folder) / "world/population/AMENDMENTS.json"
    if not path.exists():
        return world
    amendment = read(path)
    if amendment["parent_world_hash"] != digest(world):
        raise ValueError("world amendments belong to different population inputs")
    result = deepcopy(world)
    for patch in amendment["world"]:
        if not patch.get("reason"):
            raise ValueError("world amendment requires a reason")
        rows = result[patch["collection"]]
        matches = [row for row in rows if row["id"] == patch["id"]]
        if len(matches) != 1 or matches[0][patch["field"]] != patch["before"]:
            raise ValueError(f"world amendment precondition failed: {patch}")
        matches[0][patch["field"]] = deepcopy(patch["after"])
    return result


def corrected_materials(folder, materials):
    path = Path(folder) / "world/population/AMENDMENTS.json"
    if not path.exists():
        return materials
    patches = read(path).get("materials", [])
    result = {f"{m.worker_id}/{m.path}": m for m in materials}
    for patch in patches:
        key = patch["path"]
        original = result[key]
        if digest(original.content) != patch["before_hash"]:
            raise ValueError(f"material amendment precondition failed: {key}")
        result[key] = original.model_copy(update={"content": patch["content"]})
    return list(result.values())


def desktop_root(folder):
    """An amended desktop is complete, including all unchanged accepted files."""
    folder = Path(folder)
    manifest = folder / "world/POPULATION.json"
    relative = read(manifest).get("desktop_root", "world/desktop") if manifest.exists() else "world/desktop"
    path = (folder / relative).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_dir():
        raise ValueError("invalid population desktop root")
    return path
