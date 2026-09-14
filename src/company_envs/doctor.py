"""Read-only readiness checks, with explicit limits on what was measured."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .company_layout import current_pins, pin_errors, verify_pins
from .config import config_path, load_config
from .research_bundle import research_inputs
from .schemas import Company
from .storage import digest, read
from .world.hub_app import hub_apps, top_level_keys
from .world.hub_identity import identity_key


def inspect_prerequisites(root, *, folder=None, profile="inspect"):
    if profile not in {"inspect", "generate", "desktop"}:
        raise ValueError(f"unknown doctor profile: {profile}")
    root = Path(root).resolve()
    checks = []

    def record(name, ok, detail):
        checks.append({"check": name, "status": "pass" if ok else "fail", "detail": detail})

    def attempt(name, function):
        try:
            return function()
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            record(name, False, str(exc))
            return None

    record("python", sys.version_info >= (3, 12), sys.version.split()[0])
    config = attempt("configuration", lambda: load_config(root))
    if config is None:
        return {"ok": False, "profile": profile, "checks": checks}
    record("configuration", True, str(config_path(root)))
    design = config.get("design", {})
    if not isinstance(design, dict) or not isinstance(config.get("models", {}), dict):
        record("configuration_tables", False, "design and models must be TOML tables")
        return {"ok": False, "profile": profile, "checks": checks}

    def catalogs():
        manifest = read(root / "catalogs/manifest.json")
        drift = [
            name
            for name, row in manifest["files"].items()
            if not (root / "catalogs" / name).is_file()
            or digest((root / "catalogs" / name).read_bytes()) != row["sha256"]
        ]
        record("catalog_integrity", not drift, {"files": len(manifest["files"]), "drift": drift})
        catalog = hub_apps(read(root / "catalogs/apps.json")["apps"])
        exported = read(Path(folder) / "apps.json") if folder else None
        apps = (
            [app["app_id"] for app in exported["apps"]]
            if exported
            else design.get("available_runtime_apps", [])
        )
        contract_errors = []
        required_files = {"apps.json"}
        for app_id in apps:
            entry = catalog.get(app_id)
            schema = root / "catalogs/app_schemas" / Path(entry["schema"]).name if entry else None
            keys = top_level_keys(schema.read_text()) if schema and schema.is_file() else []
            if schema:
                required_files.add(str(schema.relative_to(root / "catalogs")))
            record(f"schema:{app_id}", bool(keys), {"state_keys": keys})
            if exported and schema and schema.is_file():
                actual = next(a for a in exported["apps"] if a["app_id"] == app_id)
                expected = {
                    "schema": str(schema.relative_to(root)),
                    "top_level_keys": keys,
                    "identity_key": identity_key(schema.read_text()),
                    "state_file": f"world/{app_id}.state.json",
                    "hub_seedable": True,
                }
                contract_errors.extend(f"{app_id}.{k}" for k, v in expected.items() if actual.get(k) != v)
        missing = sorted(required_files - set(manifest["files"]))
        record("catalog_coverage", not missing, {"missing_hashes": missing})
        if exported:
            company = Company.model_validate(read(Path(folder) / "company.json"))
            roster = [w.id for w in company.workers]
            if sorted(exported.get("workers", [])) != sorted(roster):
                contract_errors.append("workers do not match the dossier")
            if (
                exported.get("identity") != "per_worker_proxy"
                or exported.get("identities_file") != "world/identities.json"
            ):
                contract_errors.append("worker identity contract differs from the exported layout")
            if len(apps) != len(set(apps)):
                contract_errors.append("duplicate apps")
            export_manifest = read(Path(folder) / "MANIFEST.json")
            if export_manifest.get("export_mode") == "dossier_only":
                required = {a for s in company.software for a in s.catalog_app_ids} | set(
                    design.get("standard_apps", [])
                )
                if not required.issubset(apps):
                    contract_errors.append(f"missing dossier apps: {sorted(required - set(apps))}")
            record("export_app_contract", not contract_errors, {"errors": contract_errors})
        record("app_surface", bool(apps), {"apps": apps, "runtime_behavior": "unmeasured"})

    attempt("catalogs", catalogs)
    pins = attempt("pins", lambda: current_pins(root))
    if pins is not None:
        errors = pin_errors(pins)
        record("pins", not errors, {"values": pins, "errors": errors})

    if folder:
        folder = Path(folder).resolve()

        def dossier():
            company = Company.model_validate(read(folder / "company.json"))
            minimum = design.get("minimum_workers", 3)
            record("dossier", True, {"company_id": company.id, "workers": len(company.workers)})
            record(
                "worker_floor",
                type(minimum) is int and len(company.workers) >= minimum >= 3,
                {
                    "minimum": minimum,
                    "present": len(company.workers),
                    "consequential_collaboration": "unmeasured until task trials",
                },
            )
            evidence, _, provenance = research_inputs(root, folder, company)
            sourced = [row for row in evidence if row["kind"] == "sourced"]
            supported = sum(row["status"] == "supported" for row in sourced)
            record(
                "research",
                bool(sourced) and supported == len(sourced),
                {
                    "sourced": len(sourced),
                    "supported": supported,
                    "declared": len(evidence) - len(sourced),
                    "captured_pages": provenance.get("captured_pages"),
                    "issues": provenance["issues"],
                },
            )
            manifest = read(folder / "MANIFEST.json")
            drift = [
                relative
                for relative, expected in manifest.get("hashes", {}).items()
                if not (folder / relative).is_file() or digest((folder / relative).read_bytes()) != expected
            ]
            record("export_integrity", bool(manifest.get("hashes")) and not drift, {"drift": drift})
            record("export_pins", manifest.get("pins") == pins, {"recorded": manifest.get("pins")})

        attempt("dossier", dossier)

    if profile in {"generate", "desktop"}:
        providers = {
            value.split("/", 1)[0]
            for key, values in config.get("models", {}).items()
            if key in {"discover", "research", "expand", "review", "world_states", "world_review"}
            and isinstance(values, list)
            for value in values
        }
        record("model_configuration", bool(providers), {"providers": sorted(providers)})
        for provider in sorted(providers):
            record(f"executable:{provider}", bool(shutil.which(provider)), provider)
        if "codex" in providers:
            homes = config.get("models", {}).get("codex_homes") or [os.environ.get("CODEX_HOME", "~/.codex")]
            present = sum((Path(home).expanduser() / "auth.json").is_file() for home in homes)
            record("model_auth_file", present > 0, {"configured": len(homes), "present": present})
        checks.append(
            {
                "check": "model_access_and_quota",
                "status": "unmeasured",
                "detail": "No authentication request, inference call or quota probe was made.",
            }
        )
    if profile == "desktop":
        for executable in ("node", "npm", "qemu-system-x86_64", "qemu-img", "ssh"):
            record(f"executable:{executable}", bool(shutil.which(executable)), executable)
        for key, directory in (("vm_base_image", False), ("vm_browser_dir", True)):
            path = Path(design.get(key) or "/__company_envs_missing_setting__")
            exists = path.is_dir() if directory else path.is_file()
            record(key, exists, str(path))
        browser = Path(design.get("vm_browser_dir") or "/__company_envs_missing_setting__") / "chrome"
        record("browser_executable", browser.is_file() and os.access(browser, os.X_OK), str(browser))
        record("kvm", os.access("/dev/kvm", os.R_OK | os.W_OK), "/dev/kvm read/write access")
        result = attempt("hub_checkout", lambda: verify_pins(root, folder))
        if result is not None:
            record("hub_checkout", result["ok"], result)
        checks.append(
            {
                "check": "desktop_execution",
                "status": "unmeasured",
                "detail": "No app build, browser launch, VM boot, write/readback or reset was run.",
            }
        )
    return {
        "ok": all(row["status"] != "fail" for row in checks),
        "profile": profile,
        "config": str(config_path(root)),
        "checks": checks,
    }
