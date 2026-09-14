"""Populate independent CRM collections concurrently after its shared directories exist."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path

from company_envs.config import load_config
from company_envs.models import Models
from company_envs.storage import digest, read, write
from company_envs.world.blueprint import load_core
from company_envs.world.population import additive_world, human_targets
from company_envs.world.population_contract import NATIVE_RULES
from company_envs.world.state_seed import app_contract, core_payload, make_author, parse_json, validate_core


def main():
    root = Path(__file__).resolve().parents[3]
    folder = root / "experiments/pilot-sanmar/company"
    work = folder / "world/population"
    config = load_config(root)
    config["design"].update(seed_shards=1, seed_collections_per_call=100)
    core, _ = load_core(root, folder)
    manifest, contract = app_contract(root, folder)
    app = next(a for a in contract if a["app_id"] == "hubspot_mock")
    apps = {a["app_id"]: a for a in manifest["apps"]}
    base, identities, _materials, grants = validate_core(core, apps, manifest["workers"])
    world = additive_world(base, read(work / "EXTENSION.json"))
    state = {k: [] for k in app["top_level_keys"]}
    state["appState"] = {}
    directories = {}
    # Recovery must use named source receipts, never whichever 220-row response
    # happens to be encountered last in a directory traversal.
    sources_path = work / "CRM-DIRECTORY-SOURCES.json"
    if not sources_path.exists():
        sources_path = work / "CRM-COLLECTIONS.json"
    if not sources_path.exists():
        raise ValueError("pin companies/contacts source receipts in CRM-DIRECTORY-SOURCES.json first")
    pinned = read(sources_path)["directories"]
    for key in ("contacts", "companies"):
        path = (folder / pinned[key]["source"]).resolve()
        if not path.is_relative_to((folder / "world/calls").resolve()):
            raise ValueError("CRM directory source must be a saved provider result")
        result = read(path)
        data = result.get("data", {})
        if data.get("app_id") != "hubspot_mock" or result["receipt"]["call_id"] != path.parent.name:
            raise ValueError(f"CRM directory receipt/app mismatch: {key}")
        if pinned[key].get("sha256") and pinned[key]["sha256"] != digest(path.read_bytes()):
            raise ValueError(f"CRM directory source bytes changed: {key}")
        parsed = parse_json(data["state_json"], "saved CRM group")
        expected = {r["id"]: r for r in world["accounts" if key == "companies" else "contacts"]}
        records = parsed[key]
        if {r["id"] for r in records} != set(expected) or len(records) != len(expected):
            raise ValueError(f"CRM directory does not match canonical IDs: {key}")
        for row in records:
            source = expected[row["id"]]
            fields = ("name", "domain", "city", "state") if key == "companies" else ("email",)
            if any(source.get(f) is not None and row.get(f) != source[f] for f in fields):
                raise ValueError(f"CRM directory source fact mismatch: {row['id']}")
        state[key] = records
        directories[key] = {
            "source": str(path.relative_to(folder)),
            "sha256": digest(path.read_bytes()),
            "receipt": result["receipt"],
        }
    models = Models(config, folder / "world", max_calls=config["design"]["seed_call_budget"], cumulative=True)
    instructions = NATIVE_RULES
    receipts, problems = {}, {}
    author = make_author(
        root,
        folder,
        config,
        models,
        instructions,
        world=world,
        reference_date=core.reference_date,
        operating_scope=core.operating_scope,
        identities=identities,
        worker_apps=grants,
        company=core_payload(root, folder)["company"],
        tasks=[],
        apps=apps,
        receipts=receipts,
        problems=problems,
        population_plan=human_targets(read(work / "targets.json")),
    )
    completed = set(directories)
    phases = [
        [{"deals"}, {"tickets"}, {"meetings"}],
        [
            {"tasks"},
            {"notes"},
            set(app["top_level_keys"])
            - {"contacts", "companies", "deals", "tickets", "meetings", "tasks", "notes"},
        ],
    ]
    for phase in phases:
        snapshot = deepcopy(state)

        def one(keys, snapshot=snapshot):
            label = "-".join(sorted(keys))
            path = work / "crm-collections" / f"{label}.json"
            fingerprint = digest({"world": world, "initial": snapshot, "keys": sorted(keys)})
            if path.exists():
                saved = read(path)
                if saved["inputs"] != fingerprint:
                    raise ValueError(f"CRM collection inputs changed: {label}")
                return saved["collections"]
            _, result = author(app, previous=snapshot, only=keys)
            selected = {k: result[k] for k in keys}
            write(
                path,
                {
                    "inputs": fingerprint,
                    "collections": selected,
                    "receipts": [
                        r for r in receipts.get("hubspot_mock", []) if set(r.get("collections", [])) & keys
                    ],
                },
            )
            print(
                f"CRM {label}: { {k: len(v) if hasattr(v, '__len__') else None for k, v in selected.items()} }",
                flush=True,
            )
            return selected

        with ThreadPoolExecutor(max_workers=3) as pool:
            for future in as_completed([pool.submit(one, keys) for keys in phase]):
                result = future.result()
                state.update(result)
                completed.update(result)
    write(work / "hubspot_mock.initial.json", state)
    write(
        work / "CRM-COLLECTIONS.json",
        {
            "directories": directories,
            "completed": sorted(completed),
            "receipts": receipts,
            "problems": problems,
        },
    )


if __name__ == "__main__":
    main()
