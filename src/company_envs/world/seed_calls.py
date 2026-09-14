"""What the Stage 2 seeding calls return, and the skill text they read.

Every model call that seeds a hub company (``world_core``, ``app_state``, the JSON and
identity repairs) has its result schema here, next to the loader for the versioned skill
that instructs it and the per-call trimming of that skill. Seeding and the world review
both build on this module; it imports neither.
"""

from pydantic import BaseModel, ConfigDict, Field

from company_envs.storage import digest

SKILL = "company-world-states"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Identity(Contract):
    worker_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    user_json: str = Field(
        min_length=2, description="This worker's user record in that app, as a JSON object string"
    )


class WorkerMaterial(Contract):
    worker_id: str = Field(min_length=1)
    path: str = Field(min_length=1, description="Relative desktop path, e.g. notes/onboarding.md")
    content: str = Field(min_length=1)


class WorkerApps(Contract):
    worker_id: str = Field(min_length=1)
    app_ids: list[str]


class WorldCore(Contract):
    rationale: str = Field(min_length=1)
    reference_date: str = Field(min_length=4)
    operating_scope: str = Field(min_length=1)
    assumptions: list[str]
    entities_json: str = Field(
        min_length=2, description="Canonical entities and dated history as a JSON object string"
    )
    identities: list[Identity]
    worker_apps: list[WorkerApps] = Field(min_length=1)
    materials: list[WorkerMaterial]


class JsonRepair(Contract):
    fixed_json: str = Field(min_length=2, description="The same content as valid JSON; change nothing else")


class IdentitiesOnly(Contract):
    identities: list[Identity] = Field(min_length=1)


class WorldRecordsRepair(Contract):
    rationale: str = Field(min_length=1)
    records_json: str = Field(
        min_length=2,
        description="A JSON object mapping each given pointer to its corrected record; nothing else",
    )


class AppStateResult(Contract):
    app_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    state_json: str = Field(
        min_length=2, description="Exactly the requested named collections as a JSON object string"
    )


def load_skill(root, name=SKILL):
    path = root / ".agents" / "skills" / name / "SKILL.md"
    source = path.read_text()
    parts = source.split("---", 2)
    if len(parts) != 3 or f"name: {name}" not in parts[1]:
        raise ValueError(f"invalid skill: {path}")
    return parts[2].strip() + "\n", {"path": str(path.relative_to(root)), "hash": digest(source)}


CALL_SECTIONS = {
    "world_core": "Call `world_core`",
    "app_state": "Call `app_state`",
    "bulk_specs": "Call `bulk_specs`",
}


def skill_for_call(instructions, call):
    """The seeding skill with the sections written for other calls left out.

    The shared rules (one fact everywhere, tasks stay unsolved, write like the people there)
    always go; the per-call sections go only to their own call, so every prompt carries what
    that call needs and nothing about calls it is not making.
    """
    keep_marker = CALL_SECTIONS.get(call)
    if keep_marker is None:
        return instructions
    parts = instructions.split("\n## ")
    kept = [parts[0]]
    for part in parts[1:]:
        header = part.split("\n", 1)[0]
        other_call = any(marker in header for c, marker in CALL_SECTIONS.items() if c != call)
        revision_for_other = "revision_feedback" in header and call != "app_state"
        if other_call or revision_for_other:
            continue
        kept.append(part)
    return "\n## ".join(kept)
