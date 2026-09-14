"""Resumable Stage 3 population. This stage never reviews or serves a world."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path

from company_envs.config import load_config
from company_envs.models import Models
from company_envs.storage import digest, now, read, run_lock, write

from .blueprint import load_core
from .bulk import BulkSpecs, people_of
from .bulk import __doc__ as PLACEHOLDERS
from .bulk_layer import BULK_VERSION, _summary, check_specs, lay_bulk, max_bytes_for
from .hub_app import validate_state
from .population_amendments import corrected_materials, corrected_world
from .population_comments import author_comment_threads, new_comment_requests
from .population_contract import NATIVE_RULES, app_fingerprint
from .population_derived import repair_derived_state
from .population_quality import (
    materialize_drive,
    quality_errors,
    resource_errors,
    save_handoff,
    sync_documents,
)
from .seed_calls import AppStateResult, load_skill, skill_for_call
from .state_seed import (
    app_contract,
    app_payload,
    canonical_people,
    core_payload,
    make_author,
    merge_records,
    parse_json,
    population_findings,
    project_world,
    record_count,
    record_ids,
    seed_schema,
    validate_core,
)
from .world_check import check_folder


def additive_world(base, extension):
    """Apply additions only; callers cannot smuggle a changed accepted fact in."""
    if extension.get("parent_world_hash") != digest(base):
        raise ValueError("population extension belongs to a different canonical world")
    world = deepcopy(base)
    for collection, additions in extension.get("collections", {}).items():
        if collection not in world:
            world[collection] = []
        if not isinstance(world[collection], list) or not isinstance(additions, list):
            raise TypeError(f"extension collection must be a list: {collection}")
        existing = {r["id"]: r for r in world[collection]}
        seen = set()
        for row in additions:
            rid = row["id"]
            if not isinstance(rid, str) or not rid or rid in seen:
                raise ValueError(f"duplicate or empty extension ID: {rid}")
            seen.add(rid)
            if rid in existing:
                previous = existing[rid]
                if any(k in previous and previous[k] != v for k, v in row.items()):
                    raise ValueError(f"extension changes accepted fact: {collection}/{rid}")
                previous.update(deepcopy(row))
            else:
                world[collection].append(deepcopy(row))
                existing[rid] = world[collection][-1]
    return world


def missing_targets(state, plan, app_id):
    def count(collection):
        value = state.get(collection)
        ids = record_ids(value)
        return len(set(ids)) if ids else record_count(value)

    return [
        {
            **p,
            "existing_records": count(p["collection"]),
            "missing_records": max(0, p["target_records"] - count(p["collection"])),
        }
        for p in plan
        if p["app_id"] == app_id and count(p["collection"]) < p["target_records"]
    ]


def check_bulk_targets(specs, targets):
    """A template plan must fill exactly the missing quantities and respect their windows."""
    expected = {p["collection"]: p for p in targets}
    counts = {}
    errors = []
    for spec in specs:
        target = expected.get(spec.collection)
        if target is None:
            errors.append(f"unrequested collection: {spec.collection}")
            continue
        counts[spec.collection] = counts.get(spec.collection, 0) + spec.count
        if spec.start[:10] < target["first_date"] or spec.end[:10] > target["last_date"]:
            errors.append(f"{spec.collection}: spec exceeds requested date window")
    for name, target in expected.items():
        if counts.get(name, 0) != target["missing_records"]:
            errors.append(
                f"{name}: requested {target['missing_records']} missing records, specs supply {counts.get(name, 0)}"
            )
    return errors


def human_targets(plan):
    """Bound literal authorship without reducing final history targets."""
    return [
        {**p, "target_records": min(p["target_records"], 24)}
        if p["app_id"] == "google_docs_mock" and p["collection"] == "documents"
        else {**p, "target_records": 24 if p["collection"] == "documents" else 80}
        if p["target_records"] > 280
        else p
        for p in plan
    ]


def populate(root, folder, *, models=None, app_ids=None):
    with run_lock(Path(folder) / "world/population"):
        return _populate(root, folder, models=models, app_ids=app_ids)


def _populate(root, folder, *, models=None, app_ids=None):
    root, folder = Path(root), Path(folder)
    config = load_config(root)
    core, _ = load_core(root, folder)
    manifest, contract = app_contract(root, folder)
    apps = {a["app_id"]: a for a in manifest["apps"]}
    if app_ids is not None and (not app_ids or set(app_ids) - set(apps)):
        raise ValueError("--app must name one or more declared apps")
    base, identities, materials, grants = validate_core(core, apps, manifest["workers"])
    work = folder / "world/population"
    extension = (
        read(work / "EXTENSION.json")
        if (work / "EXTENSION.json").exists()
        else {"parent_world_hash": digest(base)}
    )
    world = corrected_world(folder, additive_world(base, extension))
    materials = corrected_materials(folder, materials)
    plan = (
        read(work / "targets.json")
        if (work / "targets.json").exists()
        else [p.model_dump() for p in core.population_plan]
    )
    # A final target remains unchanged. Human-authored anchors are followed by
    # deterministic routine traffic; documents get additional literal batches.
    human_plan = human_targets(plan)
    settings = deepcopy(config)
    settings["design"].update(
        seed_shards=1,
        seed_collections_per_call=100,
        seed_small_collection_limit=12,
        seed_compile_directories=True,
    )
    models = models or Models(
        config, folder / "world", max_calls=config["design"].get("seed_call_budget"), cumulative=True
    )
    _, _skill = load_skill(root)
    instructions = NATIVE_RULES
    payload = core_payload(root, folder)
    receipts, problems, states, failures = {}, {}, {}, {}
    author = make_author(
        root,
        folder,
        settings,
        models,
        instructions,
        world=world,
        reference_date=core.reference_date,
        operating_scope=core.operating_scope,
        identities=identities,
        worker_apps=grants,
        company=payload["company"],
        tasks=[],
        apps=apps,
        receipts=receipts,
        problems=problems,
        population_plan=human_plan,
    )
    fingerprints = {
        app["app_id"]: app_fingerprint(
            read(folder / "world/CORE.json")["hashes"], world, plan, app, identities, grants, config
        )
        for app in contract
    }
    fingerprint = digest(fingerprints)
    write(
        work / "RUN.json",
        {
            "status": "running",
            "started_at": now(),
            "inputs": fingerprint,
            "app_inputs": fingerprints,
            "concurrency": 4,
            "final_targets": plan,
            "human_targets": human_plan,
        },
    )

    def one(app):
        app_id = app["app_id"]
        app_inputs = fingerprints[app_id]
        granted_people = []
        canonical = canonical_people(world, list(grants))
        for worker, allowed_apps in grants.items():
            if app_id in allowed_apps:
                person = identities.get(worker, {}).get(app_id) or canonical.get(worker, {})
                granted_people.append(
                    {
                        "id": person.get("id") or person.get("userId"),
                        "name": person.get("name") or person.get("username"),
                        "email": person.get("email"),
                    }
                )
        checkpoint = work / f"{app_id}.json"
        supplied = work / f"{app_id}.initial.json"
        initial_hash = digest(read(supplied)) if supplied.exists() else None
        saved = None
        if checkpoint.exists():
            saved = read(checkpoint)
            if saved["inputs"] != app_inputs:
                raise ValueError(
                    f"population inputs changed for {app_id}; inspect saved output before replacing"
                )
            if digest(read(folder / "world" / f"{app_id}.state.json")) != saved["state_hash"]:
                raise ValueError(f"native population drift: {app_id}")
            if saved.get("initial_state_hash") != initial_hash:
                raise ValueError(f"supplied initial population changed for {app_id}")
            state = read(folder / "world" / f"{app_id}.state.json")
            if not missing_targets(state, plan, app_id):
                print(f"reuse {app_id}", flush=True)
                return app_id, state, saved
        partial = work / f"{app_id}.partial.json"
        partial_meta = work / f"{app_id}.partial-meta.json"
        if partial.exists() and partial_meta.exists():
            meta = read(partial_meta)
            if meta["inputs"] != app_inputs or meta["initial_state_hash"] != initial_hash:
                raise ValueError(f"partial population inputs changed: {app_id}")
            candidate = read(partial)
            if meta["state_hash"] != digest(candidate):
                raise ValueError(f"partial population drift: {app_id}")
            state = candidate
        elif saved is not None:
            pass  # Resume the verified, underfilled state without authoring its anchors again.
        elif supplied.exists():
            state = read(supplied)
        else:
            _, state = author(app)
        if not saved and not partial_meta.exists():
            write(work / f"{app_id}.human.json", state)

        def save_partial():
            write(partial, state)
            write(
                partial_meta,
                {"inputs": app_inputs, "initial_state_hash": initial_hash, "state_hash": digest(state)},
            )

        save_partial()
        print(
            f"human layer {app_id}: { {k: record_count(v) for k, v in state.items() if isinstance(v, (list, dict))} }",
            flush=True,
        )
        # Persist before the next provider call; an interruption must not lose
        # completed human work. Provider calls also retain content-addressed cache.
        extra_receipts, ids, specs, spec_errors = [], list((saved or {}).get("bulk_ids", [])), [], []
        targets = missing_targets(state, plan, app_id)
        if app_id == "google_docs_mock":
            for target in targets:
                collection = target["collection"]
                if collection == "comments":
                    requests = new_comment_requests(
                        state, target["missing_records"], target["first_date"], target["last_date"]
                    )
                    comments, batches = author_comment_threads(
                        state,
                        requests,
                        models,
                        work,
                        reference_date=core.reference_date,
                        concurrency=4,
                        first_date=target["first_date"],
                        last_date=target["last_date"],
                    )
                    state[collection] = merge_records(state[collection], comments)
                    extra_receipts.extend(r for b in batches for r in b["receipts"])
                    save_partial()
                    continue
                # Two bounded extra rounds handle partial batches and duplicate IDs.
                for batch in range((target["missing_records"] + 19) // 20 + 2):
                    current = missing_targets(state, [target], app_id)
                    remaining = current[0]["missing_records"] if current else 0
                    if remaining <= 0:
                        break
                    body = app_payload(
                        {
                            "world": world,
                            "reference_date": core.reference_date,
                            "operating_scope": core.operating_scope,
                        },
                        app,
                        {w: d[app_id] for w, d in identities.items() if app_id in grants[w]},
                        grants,
                        payload["company"],
                        [],
                        collections=[collection],
                        authored=_summary(state, list(state)),
                    )
                    body["population_batch"] = {
                        "count": min(20, remaining),
                        "index": batch,
                        "existing_ids": record_ids(state[collection]),
                        "window": [target["first_date"], target["last_date"]],
                    }
                    body["output_contract"] = (
                        f"Return exactly {min(20, remaining)} NEW literal {collection} records. Each document has a different plausible substantive piece of ordinary work and complete readable body. No template specs or task answers. Existing records are immutable. Use unique IDs; comment document IDs must exist in already_authored_collections."
                    )
                    result, receipt = models.call(
                        "world_states",
                        skill_for_call(instructions, "app_state") + "\n" + json.dumps(body),
                        AppStateResult,
                    )
                    addition = parse_json(result.state_json, f"{app_id} batch", expected=[collection])
                    state[collection] = merge_records(state[collection], addition[collection])
                    extra_receipts.append(receipt)
                    save_partial()
        elif targets:
            keys = [p["collection"] for p in targets]
            body = {
                "call": "bulk_specs",
                "app": {
                    "app_id": app_id,
                    "schema_document": seed_schema(app["schema_document"]),
                    "record_collections": keys,
                },
                "reference_date": core.reference_date,
                "canonical_world": project_world(world, app_id),
                "human_layer": _summary(state, list(state)),
                "people": granted_people or people_of(state),
                "population_targets": targets,
                "placeholders": PLACEHOLDERS,
                "budget": {
                    "max_bytes_for_app": max_bytes_for(config, app_id),
                    "bytes_used": len(json.dumps(state)),
                },
                "output_contract": "Supply exactly the missing_records for each requested collection across your specs, within the target dates. Preserve the human layer. Only explicit automated notifications/imports grounded in supplied event rows and real recurring schedules. Never template human correspondence, discussions, substantive CRM notes or meeting outcomes. A completed deal must refer to an actual accepted transaction; otherwise request literal episode authoring instead. Split substantial streams into several varied templates; use meaningful choice/table variations and correlated table fields. No invented campaign performance, new policies, order/credit execution authority, or duplicate counterparties. Do not fill byte quotas. Every reference must be a supplied native ID. Native collection shapes must match the samples. No task creation.",
            }
            for attempt in range(3):
                result, receipt = models.call(
                    "world_states",
                    skill_for_call(instructions, "bulk_specs") + "\n" + json.dumps(body),
                    BulkSpecs,
                )
                extra_receipts.append(receipt)
                spec_errors = check_bulk_targets(result.specs, targets) + check_specs(
                    result.specs,
                    app,
                    state,
                    keys,
                    max_bytes_for(config, app_id),
                    people=granted_people or None,
                )
                if not spec_errors:
                    candidate = deepcopy(state)
                    _, candidate_ids, short = lay_bulk(
                        candidate,
                        result.specs,
                        config["generation"].get("seed", 0),
                        max_bytes_for(config, app_id),
                        people=granted_people or None,
                    )
                    spec_errors = [str(s) for s in short] + [
                        str(s) for s in missing_targets(candidate, plan, app_id)
                    ]
                if not spec_errors:
                    specs = result.specs
                    state = candidate
                    ids.extend(candidate_ids)
                    break
                body["revision_feedback"] = spec_errors
        if app_id == "google_drive_mock":
            materialize_drive(folder, state)
        write(folder / "world" / f"{app_id}.state.json", state)
        entry = {
            "inputs": app_inputs,
            "initial_state_hash": initial_hash,
            "state_hash": digest(state),
            "completed_at": now(),
            "receipts": (saved or {}).get("receipts", []) + receipts.get(app_id, []) + extra_receipts,
            "bulk_ids": sorted(ids),
            "specs": (saved or {}).get("specs", []) + [s.model_dump() for s in specs],
            "bulk_version": BULK_VERSION,
            "problems": problems.get(app_id, []) + spec_errors,
            "bytes": len(json.dumps(state, ensure_ascii=False).encode()),
            "shortfalls": missing_targets(state, plan, app_id),
        }
        write(checkpoint, entry)
        partial.unlink(missing_ok=True)
        partial_meta.unlink(missing_ok=True)
        print(f"finished {app_id}: {entry['bytes']} bytes; {len(entry['shortfalls'])} shortfalls", flush=True)
        return app_id, state, entry

    entries = {}
    selected = [app for app in contract if app_ids is None or app["app_id"] in app_ids]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(one, app): app["app_id"] for app in selected}
        for future in as_completed(futures):
            app_id = futures[future]
            try:
                app_id, state, entry = future.result()
                states[app_id], entries[app_id] = state, entry
            except Exception as exc:  # noqa: BLE001 -- record independent app failures, including adapter defects
                failures[app_id] = f"{type(exc).__name__}: {exc}"
                write(work / f"{app_id}.failure.json", {"at": now(), "error": failures[app_id]})
                print(f"failed {app_id}: {failures[app_id]}", flush=True)
    for app_id in apps:
        if app_id not in states and app_id not in failures:
            checkpoint = work / f"{app_id}.json"
            native = folder / "world" / f"{app_id}.state.json"
            if checkpoint.exists() and native.exists():
                entry, state = read(checkpoint), read(native)
                if entry["inputs"] != fingerprints[app_id] or entry["state_hash"] != digest(state):
                    failures[app_id] = "existing app checkpoint does not match its inputs/state"
                else:
                    entries[app_id], states[app_id] = entry, state
    # Also run on reused checkpoints. Derived metadata must not need a model or
    # a company-specific repair script; record every change before committing it.
    for aid in list(states):
        try:
            candidate, changes = repair_derived_state(aid, states[aid])
            if changes:
                report = {
                    "app": aid,
                    "before_hash": digest(states[aid]),
                    "after_hash": digest(candidate),
                    "reason": "Recompute native indices and activity summaries from existing records",
                    "changes": changes,
                }
                path = work / "derived-repairs" / f"{digest(report)}.json"
                write(path, report)
                entry = entries[aid]
                entry.setdefault("derived_repairs", []).append(str(path.relative_to(folder)))
                entry.update(
                    state_hash=digest(candidate),
                    bytes=len(json.dumps(candidate, ensure_ascii=False).encode()),
                )
                write(folder / f"world/{aid}.state.json", candidate)
                write(work / f"{aid}.json", entry)
                states[aid] = candidate
        except (ValueError, KeyError, TypeError) as exc:
            failures[aid] = f"Derived metadata repair failed: {exc}"
    if "google_docs_mock" in states and "google_drive_mock" in states:
        try:
            sync_documents(states["google_docs_mock"], states["google_drive_mock"])
            for aid in ("google_docs_mock", "google_drive_mock"):
                state = states[aid]
                entries[aid].update(
                    state_hash=digest(state), bytes=len(json.dumps(state, ensure_ascii=False).encode())
                )
                write(folder / "world" / f"{aid}.state.json", state)
                write(work / f"{aid}.json", entries[aid])
        except ValueError as exc:
            failures["document_registry"] = str(exc)
    # A rejected checkpoint still has inspectable output. Preserve it in the
    # handoff and counts while failures prevent acceptance; reporting zero
    # records would misdiagnose a changed compiler as lost population data.
    for app in contract:
        aid = app["app_id"]
        native, checkpoint = folder / "world" / f"{aid}.state.json", work / f"{aid}.json"
        if aid not in states and native.is_file() and checkpoint.is_file():
            states[aid], entries[aid] = read(native), read(checkpoint)
    native_errors = quality_errors(states) + resource_errors(folder)
    for app in contract:
        aid = app["app_id"]
        if aid not in states:
            continue
        try:
            validate_state(
                {"id": aid, "state_keys": app["top_level_keys"]}, states[aid], app["schema_document"]
            )
        except (ValueError, TypeError) as exc:
            native_errors.append(f"{aid}: {exc}")
        if len(json.dumps(states[aid], ensure_ascii=False).encode()) > max_bytes_for(config, aid):
            native_errors.append(f"{aid}: byte limit exceeded")
        native_errors.extend(f"{aid}: {p}" for p in entries[aid].get("problems", []))
    checks = check_folder(
        root,
        folder,
        states,
        world=world,
        identities=identities,
        materials={f"{m.worker_id}/{m.path}": m.content for m in materials},
        reference_date=core.reference_date,
        worker_apps=grants,
        bulk_ids={a: set(e["bulk_ids"]) for a, e in entries.items()},
        authoring=True,
    )
    shortfalls = population_findings(states, plan)
    result = {
        "status": "populated_for_review"
        if not failures and not shortfalls and not native_errors and checks["ok"]
        else "population_needs_repair",
        "completed_at": now(),
        "inputs": fingerprint,
        "apps": entries,
        "failures": failures,
        "shortfalls": shortfalls,
        "checks": checks,
        "native_errors": native_errors,
        "review": "Stage 4, not run",
        "runtime": "not started",
    }
    write(work / "RESULT.json", result)
    save_handoff(folder, core, world, states, entries, result["status"], native_errors)
    run = read(work / "RUN.json")
    run.update(status=result["status"], completed_at=result["completed_at"])
    write(work / "RUN.json", run)
    # Check again to prove no stage-three write modified the accepted checkpoint.
    load_core(root, folder)
    return result
