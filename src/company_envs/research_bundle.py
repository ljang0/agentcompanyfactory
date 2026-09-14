"""Carry captured research across the dossier/world/task boundary without retrieval."""

from pathlib import Path

from .sources import Sources, normalize
from .storage import digest, read, write


def amend_dossier(folder, replacement, reason):
    """Revise construction assumptions before seeding while preserving research claims."""
    from .schemas import Company
    from .storage import now

    folder = Path(folder)
    original = read(folder / "company.json")
    Company.model_validate(replacement)
    if original.get("evidence") != replacement.get("evidence"):
        raise ValueError("dossier amendment cannot change research claims")
    if any(original.get(k) != replacement.get(k) for k in original if k not in {"workers", "assumptions"}):
        raise ValueError("only worker construction details and assumptions may be amended")
    if {w["id"] for w in original["workers"]} != {w["id"] for w in replacement["workers"]}:
        raise ValueError("dossier amendment cannot change the exported worker roster")
    if (folder / "world/world.json").exists() or list((folder / "world").glob("*.state.json")):
        raise ValueError("amend the dossier before the world exists")
    research = folder / "research"
    provenance = read(research / "PROVENANCE.json")
    if provenance["company_sha256"] != digest(original):
        raise ValueError("existing dossier binding is stale")
    if (research / "AMENDMENT.json").exists():
        raise ValueError("a dossier amendment already exists; inspect it before another revision")
    write(research / "dossier-original.json", original)
    write(
        research / "AMENDMENT.json",
        {
            "at": now(),
            "reason": reason,
            "before_sha256": digest(original),
            "after_sha256": digest(replacement),
            "unchanged_claims_sha256": digest(original["evidence"]),
        },
    )
    write(folder / "company.json", replacement)
    provenance["company_sha256"] = digest(replacement)
    for name in ("dossier-original.json", "AMENDMENT.json"):
        provenance["hashes"][name] = digest((research / name).read_bytes())
    write(research / "PROVENANCE.json", provenance)
    manifest = read(folder / "MANIFEST.json")
    manifest["hashes"] = {
        str(p.relative_to(folder)): digest(p.read_bytes())
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name != "MANIFEST.json"
    }
    write(folder / "MANIFEST.json", manifest)


def _local(root, relative):
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError(f"research path escapes its root: {relative}")
    return path


def _evidence_file(root, manifest, company):
    """Only reuse a job whose entire claim set matches this dossier version."""
    candidates = []
    run_id, company_id = manifest.get("source_run"), company.get("id")
    if run_id and company_id:
        candidates.append(f"runs/{run_id}/jobs/{company_id}/evidence.json")
    source = manifest.get("source_paths", {}).get("company")
    if source:
        candidates.append(str(Path(source).parent / "evidence.json"))
    for relative in candidates:
        path = _local(root, relative)
        dossier = path.parent / "company.json"
        if not path.is_file() or not dossier.is_file():
            continue
        original = read(dossier)
        if original.get("id") == company_id and original.get("evidence") == company.get("evidence"):
            return path
    return None


def _captured(root, manifest, company):
    path = _evidence_file(root, manifest, company)
    rows = read(path) if path else []
    by_id = {row["claim_id"]: row for row in rows}
    evidence, pages, issues = [], {}, []
    for claim in company.get("evidence", []):
        row = dict(by_id.get(claim["id"], {}))
        row.update(claim_id=claim["id"], kind=claim["kind"])
        if claim["kind"] != "sourced":
            row["status"] = "declared"
        else:
            url = claim.get("source_url", "")
            cached = Path(root) / "data/sources" / f"{digest(url)}.json"
            page = read(cached) if cached.is_file() else {}
            text_hash = digest(page.get("text", ""))
            valid = (
                row.get("status") == "supported"
                and row.get("source_url") == url
                and page.get("url") == url
                and page.get("status") == "captured"
                and text_hash == page.get("text_hash") == row.get("text_hash")
                and bool(normalize(claim.get("quote", "")))
                and normalize(claim["quote"]) in normalize(page.get("text", ""))
            )
            if valid:
                pages[digest(url)] = page
            else:
                row["status"] = "unverified"
                issues.append(f"{claim['id']}: missing matching evidence or captured source text")
        evidence.append(row)
    return (
        evidence,
        pages,
        {
            "source_evidence": str(path.relative_to(Path(root).resolve())) if path else None,
            "status": "verified" if evidence and not issues else "unverified",
            "issues": issues,
        },
    )


def export_research(root, output, manifest, company):
    """Freeze supporting pages once per URL; never update the shared source cache."""
    output = Path(output)
    evidence, pages, provenance = _captured(root, manifest, company)
    research = output / "research"
    write(research / "evidence.json", evidence)
    for key, page in pages.items():
        write(research / "sources" / f"{key}.json", page)
    provenance.update(
        schema_version=1,
        company_sha256=digest(company),
        source_run=manifest.get("source_run"),
        captured_pages=len(pages),
        hashes={
            str(p.relative_to(research)): digest(p.read_bytes())
            for p in sorted(research.rglob("*"))
            if p.is_file()
        },
    )
    write(research / "PROVENANCE.json", provenance)
    return provenance


def research_inputs(root, folder, company):
    """Read a frozen bundle, or explicitly unverified legacy inputs.

    A damaged bundle raises instead of silently falling back to a mutable cache.
    The returned excerpts use the same quote-preserving bounds as research review.
    """
    folder = Path(folder)
    raw = company.model_dump() if hasattr(company, "model_dump") else company
    manifest = read(folder / "MANIFEST.json")
    research = folder / "research"
    if research.exists():
        provenance = read(research / "PROVENANCE.json")
        if provenance["company_sha256"] != digest(raw):
            raise ValueError("research bundle belongs to a different dossier version")
        if "evidence.json" not in provenance["hashes"]:
            raise ValueError("research bundle is missing its evidence hash")
        for relative, expected in provenance["hashes"].items():
            path = _local(research, relative)
            if not path.is_file() or digest(path.read_bytes()) != expected:
                raise ValueError(f"research bundle drift: {relative}")
        evidence = read(research / "evidence.json")
        claims = {c["id"]: c for c in raw["evidence"]}
        if (
            not isinstance(evidence, list)
            or any(not isinstance(row, dict) for row in evidence)
            or len(evidence) != len(claims)
            or {row.get("claim_id") for row in evidence} != set(claims)
        ):
            raise ValueError("research bundle must cover every dossier claim exactly once")
        for row in evidence:
            claim = claims[row["claim_id"]]
            if row.get("kind") != claim["kind"]:
                raise ValueError(f"research bundle claim kind differs: {claim['id']}")
            if row.get("status") != "supported":
                continue
            relative = f"sources/{digest(claim['source_url'])}.json"
            if relative not in provenance["hashes"]:
                raise ValueError(f"research bundle missing source hash: {relative}")
            page = read(_local(research, relative))
            quote = normalize(claim.get("quote", ""))
            if not (
                claim["kind"] == "sourced"
                and row.get("source_url") == page.get("url") == claim["source_url"]
                and page.get("status") == "captured"
                and row.get("text_hash") == page.get("text_hash") == digest(page.get("text", ""))
                and quote
                and quote in normalize(page.get("text", ""))
            ):
                raise ValueError(f"research bundle support does not match claim: {claim['id']}")
        sources = Sources(root, cache_path=research / "sources")
    else:
        evidence, _, provenance = _captured(root, manifest, raw)
        sources = Sources(root)
    # Company instances are used by authors; dictionary-only inspection needs no excerpts.
    excerpts = sources.excerpts(company) if hasattr(company, "evidence") else []
    supported_urls = {r.get("source_url") for r in evidence if r.get("status") == "supported"}
    excerpts = [row for row in excerpts if row["url"] in supported_urls]
    return evidence, excerpts, provenance
