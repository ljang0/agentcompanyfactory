"""Pinned reference imports and economic priorities; no company templates."""

import shutil
import subprocess
from pathlib import Path

from .storage import digest, now, read, write

TABLES = (
    "gdpval_occupations.json",
    "gdpval_socs.json",
    "industry_occupations.json",
    "soc_wages.json",
    "software_prior.json",
    "apps.json",
    "gdp_sectors.json",
    "naics_sector_map.json",
    "tool_counterparts.json",
    "gym_anything_kinds.json",
)


def bootstrap(root: Path, source: Path):
    target = root / "catalogs"
    if (target / "manifest.json").exists():
        return read(target / "manifest.json")
    target.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in TABLES:
        original = source / "taxonomy" / name
        content = original.read_bytes()
        (target / name).write_bytes(content)
        files[name] = {"sha256": digest(content), "original": str(original)}
    for original in sorted((source / "apps").glob("*.md")):
        dest = target / "app_schemas" / original.name
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(original, dest)
        files[str(dest.relative_to(target))] = {
            "sha256": digest(dest.read_bytes()),
            "original": str(original),
        }
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    manifest = {
        "captured_at": now(),
        "source_repository": str(source),
        "source_commit": commit,
        "note": "Working-tree bytes are pinned individually; they may differ from the source commit.",
        "files": files,
    }
    write(target / "manifest.json", manifest)
    return manifest


class Catalogs:
    def __init__(self, directory):
        self.path = Path(directory)
        self.sectors = read(self.path / "gdpval_occupations.json")["sectors"]
        self.anchors = read(self.path / "gdpval_socs.json")["socs"]
        self.apps = {a["id"]: a for a in read(self.path / "apps.json")["apps"]}
        names = {s["id"]: s["name"] for s in read(self.path / "gdp_sectors.json")["sectors"]}
        self.naics_sectors = {
            prefix: names[sid]
            for prefix, sid in read(self.path / "naics_sector_map.json")["prefixes"].items()
        }
        self.occupations = {}
        self.industries = read(self.path / "industry_occupations.json")["industries"]
        for row in self.industries.values():
            for o in row["occupations"]:
                self.occupations[o["soc_code"]] = o["onet_title"]

    def integrity_errors(self):
        manifest = read(self.path / "manifest.json")
        return [
            name
            for name, row in manifest["files"].items()
            if not (self.path / name).is_file() or digest((self.path / name).read_bytes()) != row["sha256"]
        ]

    def economic_context(self, sector):
        anchors = {soc: row["title"] for soc, row in self.anchors.items() if sector in row["sectors"]}
        return {
            "sector": sector,
            "priority_occupations": anchors,
            "other_occupations_allowed": True,
            "occupation_codes": self.occupations,
        }

    def software_context(self, surface=None):
        """Catalog rows the author may choose from; ``surface`` limits it to hostable apps.

        Off-surface rows are noise in every research/design prompt (249 gym-anything
        environments the runtime cannot host), so callers pass the run's surface.
        """
        allowed = None if surface is None else set(surface)
        return [
            {
                "id": a["id"],
                "name": a["name"],
                "reference_product": a.get("reference_tool", ""),
                "description": a.get("notes", ""),
                "schema_available": bool(a.get("schema")),
                "reported_seedable": a.get("seedable", False),
                "reported_ui_verified": a.get("verified", False),
                "source": a.get("source", ""),
                "reference_url": a.get("reference_url", ""),
            }
            for a in self.apps.values()
            if allowed is None or a["id"] in allowed
        ]

    def reference_provenance(self):
        """Describe the imported bytes, without mistaking them for an upstream build."""
        manifest = read(self.path / "manifest.json")
        upstreams = {}
        for app in self.apps.values():
            source = app.get("source", "unrecorded")
            upstreams.setdefault(source, set()).add(app.get("reference_url", ""))
        return {
            "catalog_manifest_hash": digest(manifest),
            "import_repository": manifest.get("source_repository"),
            "import_commit": manifest.get("source_commit"),
            "import_note": manifest.get("note", ""),
            "upstreams": {
                source: {"reference_urls": sorted(urls - {""}), "revision": "unrecorded"}
                for source, urls in sorted(upstreams.items())
            },
            "scope": "Imported documentation and reported capabilities, not runtime verification. "
            "The import commit identifies the intermediate repository, not an upstream app revision.",
        }

    def app_schemas(self):
        """Load only manifest-pinned schema documents; callers freeze these bytes per call."""
        manifest = read(self.path / "manifest.json")
        result = {}
        for app_id, app in self.apps.items():
            if not app.get("schema"):
                continue
            relative = "app_schemas/" + Path(app["schema"]).name
            row = manifest["files"].get(relative)
            if row is None:
                continue
            path = self.path / relative
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"missing or nonregular frozen app schema: {relative}")
            content = path.read_bytes()
            if digest(content) != row["sha256"]:
                raise ValueError(f"frozen app schema changed: {relative}")
            result[app_id] = {
                "app_id": app_id,
                "path": relative,
                "sha256": row["sha256"],
                "text": content.decode("utf-8"),
                "reference_url": app.get("reference_url", ""),
                "scope": "Imported schema documentation; runtime behavior remains unverified.",
            }
        return result

    def tool_context(self, sources=(), surface=None):
        schemas = self.app_schemas()
        if surface is not None:
            schemas = {app_id: doc for app_id, doc in schemas.items() if app_id in set(surface)}
        return {
            "sources": list(sources),
            "catalog": self.software_context(surface),
            "occupations": self.occupations,
            "app_schemas": schemas,
            "reference_provenance": self.reference_provenance(),
        }

    def validate(self, company):
        if company.sector not in {s["name"] for s in self.sectors}:
            raise ValueError("unknown economic sector")
        matches = [p for p in self.naics_sectors if company.naics.startswith(p)]
        if not matches or self.naics_sectors[max(matches, key=len)] != company.sector:
            raise ValueError("NAICS code does not match the selected economic sector")
        for w in company.workers:
            if w.soc not in self.occupations:
                raise ValueError(f"worker {w.id}: unknown SOC {w.soc}")
        for app in company.software:
            for aid in app.catalog_app_ids:
                if aid not in self.apps:
                    raise ValueError(f"unknown catalog app {aid}")
            if app.status == "gap" and app.catalog_app_ids:
                raise ValueError("a software gap must not pretend to have a mapped app")
            if app.status != "gap" and not app.catalog_app_ids:
                raise ValueError("mapped software needs a catalog app id")


def sector_targets(sectors, n):
    if n < 1:
        raise ValueError("company target must be positive")
    total = sum(s["gdp_share_pct"] for s in sectors)
    raw = {s["name"]: n * s["gdp_share_pct"] / total for s in sectors}
    counts = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: (-(raw[k] - counts[k]), k))[: n - sum(counts.values())]:
        counts[k] += 1
    if n >= len(sectors):
        for k, count in counts.items():
            if count == 0:
                donor = max(counts, key=lambda x: (counts[x] - raw[x], counts[x]))
                counts[donor] -= 1
                counts[k] = 1
    return counts
