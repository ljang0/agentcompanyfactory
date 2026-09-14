"""Versioned stage instructions: one source shared by interactive and batch use."""

from .storage import digest

STAGES = {
    "discover": "company-discovery",
    "research": "company-research",
    "expand": "company-workflows",
    "review": "company-review",
}


def freeze_skills(root):
    prompts, records = {}, {}
    for stage, name in STAGES.items():
        path = root / ".agents" / "skills" / name / "SKILL.md"
        source = path.read_text()
        parts = source.split("---", 2)
        if len(parts) != 3 or parts[0].strip() or f"name: {name}" not in parts[1]:
            raise ValueError(f"invalid stage skill: {path}")
        prompts[stage] = parts[2].strip() + "\n"
        records[stage] = {"path": str(path.relative_to(root)), "hash": digest(source), "source": source}
    return prompts, records
