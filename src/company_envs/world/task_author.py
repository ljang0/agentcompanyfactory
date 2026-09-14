"""Author reviewed digital tasks against an immutable, already seeded company world.

Initial materials use exact ``app_id.collection#id`` references, one per string.
This intentionally rejects prose-only material descriptions: guessing which tokens
are record IDs cannot provide a fail-closed referential-integrity check.
"""

import json
import re
import sys
from pathlib import Path

from company_envs import review, workflows
from company_envs.catalogs import Catalogs
from company_envs.config import load_config
from company_envs.models import ModelOutputInvalid, Models, PromptTooLarge
from company_envs.receipt import Receipt
from company_envs.research_bundle import research_inputs
from company_envs.schemas import Company
from company_envs.storage import digest, now, read, write

from .hub_app import UI_STATE_KEYS
from .state_seed import bound_states, load_skill
from .task_assessment import indexed_records

REFERENCE = re.compile(r"(?P<app>[A-Za-z][A-Za-z0-9_-]*)\.(?P<collection>[^.#\s]+)#(?P<id>[^\s]+)")
# The same set _validate_grader refuses a state check on: technical interface keys only, so that
# "id" or "title" as a top-level key stays data. Imported rather than restated so the design-time
# gate and the grading-time gate cannot drift apart.
INTERFACE_STATE = frozenset(k for k in UI_STATE_KEYS if re.search(r"[A-Z_]", k))
INDEX_FIELD = re.compile(r"^(id|title|subject|name|status|state|.*[Dd]ate|.*_at|.*At|start|end|due)$")


def _local(folder, relative):
    path = (folder / relative).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError(f"path leaves company folder: {relative}")
    return path


PROMPT_LIMIT = 1_048_576  # the provider refuses a prompt longer than this
# What the whole seeded-world payload may occupy. The old budget was 600,000 and it bounded
# ``app_index`` alone, so ``world`` and ``record_counts`` went through unbounded -- 109,316-133,695
# characters of canonical world on top of a capped index. Measured over the 234 refused mining
# attempts on disk, the payload itself was 878,083-1,254,675 characters and the rest of the prompt
# around it 73,140-202,135 (median 125,917: the skill, the dossier, the source excerpts, the runtime
# mounting). So the budget covers the payload whole and leaves 248,576 above the worst prompt
# remainder observed. It trims no company on disk more than the old one did: the largest bounded
# payload of the 12 that were refused is sanmar's 685,801 characters.
PAYLOAD_BUDGET = 800_000
COLLECTION_CAP = 2000  # records per collection before any budget trimming


def _capped(index, cap):
    return {
        app: {
            key: rows
            if len(rows) <= cap
            else [*rows[:cap], {"id": "__more__", "omitted_records": len(rows) - cap}]
            for key, rows in collections.items()
        }
        for app, collections in index.items()
    }


LOCAL_ONLY = ("provenance", "states")  # read locally; never serialized into a model payload


def bounded_snapshot(snapshot, budget=PAYLOAD_BUDGET):
    """The snapshot as the model sees it: as many records as the prompt limit allows.

    A whole company's record index outgrows the provider's prompt limit (measured: 1.19M characters
    for one world against a 1,048,576 ceiling, which failed every mining call). Trimming costs the
    designer the records it reasons about, so this trims only as far as it must: the cap halves
    until the payload fits BUDGET, and a collection that is cut says how many records it left out.
    Validation keeps the complete snapshot, so a task may still cite any record that exists.

    The halving used to stop at 25 and then return that cap unchecked, so the budget was a
    preference rather than a bound; it now halves to 1, which serializes at most one record per
    collection and is the smallest index this function can produce. The keys in LOCAL_ONLY are
    dropped: the 6,357-9,268 characters of file hashes are for the tamper check, and the full app
    states reach the designer through the read_seed tool, not the prompt.

    The budget bounds the whole returned payload, not ``app_index`` inside it. Measured at HEAD over
    the 12 companies whose mining was refused, the canonical world is 109,316-133,695 characters and
    the record counts another 1,427-2,671, and neither was inside the old bound: 2 of the 12 still
    come back over 600,000. Capping the index to one record per collection and still not fitting
    means the world is the rest of the payload, so it is sampled with the same ``bound_states`` the
    reviewers and the grader authors use.
    """
    index, cap = snapshot["app_index"], COLLECTION_CAP
    rest = {k: v for k, v in snapshot.items() if k not in LOCAL_ONLY and k != "app_index"}
    while True:
        out = rest | {"app_index": _capped(index, cap)}
        if len(json.dumps(out, ensure_ascii=False)) <= budget or cap <= 1:
            break
        cap //= 2
    if len(json.dumps(out, ensure_ascii=False)) > budget and out.get("world"):
        spare = budget - len(json.dumps({**out, "world": {}}, ensure_ascii=False))
        out["world"] = bound_states({"world": out["world"]}, limit=max(20_000, spare), seed="mine:world")[
            "world"
        ]
    return out


def seeded_snapshot(folder, *, root=None):
    """Index every ID-bearing nested record, retaining only short identifying scalars.

    Full states are read locally but never enter model payloads: they are returned under
    ``states``, which bounded_snapshot drops, and reach the designer only through read_seed.
    Native numeric IDs are compared as strings. Collection keys remain present even when empty.
    """
    folder = Path(folder).resolve()  # the indexed paths resolve, so the folder must too
    staged = (folder / "world/CORE.json").exists() or (folder / "world/POPULATION.json").exists()
    if staged:
        from .world_acceptance import accepted_world

        if root is None:
            raise ValueError("A staged world requires a repository root and current freeze proof")
        if not (folder / "world/FROZEN.json").is_file():
            raise ValueError("Stage 4 must freeze this world before task discovery")
        accepted_world(root, folder)
        population = read(folder / "world/POPULATION.json")
        world_path = _local(folder, population["effective_world"])
    else:
        world_path = folder / "world/world.json"
    apps = read(folder / "apps.json")
    files = [folder / "company.json", folder / "apps.json", world_path]
    index, states = {}, {}
    for app in apps["apps"]:
        path = _local(folder, app["state_file"])
        state = states[app["app_id"]] = read(path)
        files.append(path)
        if set(state) != set(app["top_level_keys"]):
            raise ValueError(f"seeded state keys differ from app contract: {app['app_id']}")
        index[app["app_id"]] = {
            key: [
                {
                    "id": rid,
                    **{
                        k: v[:240] if isinstance(v, str) and k != "id" else v
                        for k, v in record.items()
                        if INDEX_FIELD.fullmatch(k) and isinstance(v, (str, int, float, bool))
                    },
                }
                for rid, record in indexed_records(key, value)
            ]
            for key, value in state.items()
        }
    # Freeze identity/access/material evidence as well as the indexed record bytes.
    if staged:
        files.extend(_local(folder, p) for p in population["artifacts"])
        files.extend(
            folder / "world" / p
            for p in (
                "CORE.json",
                "FROZEN.json",
                "POPULATION.json",
                "identities.json",
                "worker_apps.json",
                "acceptance/REVIEW.json",
                "acceptance/RUNTIME.json",
            )
        )
    else:
        files.extend(p for p in (folder / "world").rglob("*") if p.is_file() and "calls" not in p.parts)
    hashes = {str(p.relative_to(folder)): digest(p.read_bytes()) for p in sorted(set(files))}
    return {
        "world": read(world_path),
        **(
            {
                "accepted_world": True,
                "worker_apps": read(folder / "world/worker_apps.json"),
                "identities": read(folder / "world/identities.json"),
            }
            if staged
            else {}
        ),
        "app_index": index,
        "standard_apps": [a["app_id"] for a in apps["apps"] if a.get("role") == "standard"],
        # The signed-in account of each app. A decisive collection may not be one of these: the
        # grader author refuses a scored state check on an identity collection, so a task whose
        # feature cell named one could never be graded.
        "identity_keys": {a["app_id"]: a["identity_key"] for a in apps["apps"] if a.get("identity_key")},
        # What each collection really holds, whatever shape the app keeps it in. app_index above
        # counts only dicts carrying an "id"; the feature matrix and the designer need the truth.
        "record_counts": {
            f"{app}.{key}": collection_records(value)
            for app, state in states.items()
            for key, value in state.items()
        },
        "provenance": hashes,
        "states": states,
    }


def collection_records(value):
    """How many records a top-level collection holds, whatever shape the app keeps it in.

    Shape-aware on purpose: ``records()`` counts only dicts carrying an ``id``, which reads
    ``slack_mock.users`` (33 records keyed by ``userId``), ``slack_mock.messages`` (a map of lists)
    and ``amazon_mock.wishlist`` (bare product ids) as empty. Measured across the 60 seeded worlds
    on disk: 581 of 3,695 collections hold content and index as zero records, 389 of them in
    slack_mock, whose users/channels/messages/threads read as empty in every single world. A gate
    built on the blind count would reject grounded tasks and pass empty ones.
    """
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        if not value:
            return 0
        if all(isinstance(row, list) for row in value.values()):
            return sum(len(row) for row in value.values())  # a map of lists, keyed by channel
        if all(isinstance(row, dict) for row in value.values()):
            return len(value)  # records keyed by their own id
        return 1  # one keyed object: gmail_mock.user, adp_mock.employee
    return 0


def feature_cell_problems(collections, states, identity_keys=None):
    """Why a task's decisive collections could not carry or verify its work, as reasons.

    ``feature_cell`` was never checked against the world the task runs in, and each of these
    three is terminal far downstream, after a world, a golden and a grader pass have been paid
    for. Measured over the 108 accepted tasks in the 60 seeded worlds: three decisive collections
    in three tasks hold no records at all -- both of one company's tasks turn on
    ``booking_com_mock.bookings``, which is literally ``[]``, and that company is retired. The
    interface-state and identity rules are the grader author's own refusals ("isDragging is
    interface state", "proxy identity cannot establish task progress") brought forward to design
    time, where a redesign is cheap; both are latent today (0 of 216 decisive collections).
    """
    identity_keys = identity_keys or {}
    problems = []
    for collection in collections:
        app, _, key = str(collection).partition(".")
        if app not in states or key not in states[app]:
            problems.append(f"{collection} is not a seeded collection of {app or 'any app'}")
        elif key == identity_keys.get(app):
            # Named before the interface rule: currentUser is both, and "the signed-in account" is
            # the diagnosis a designer can act on.
            problems.append(
                f"{collection} is {app}'s signed-in account; a scored state check may not select it, "
                "so no grader can verify decisive work that lands there"
            )
        elif key in INTERFACE_STATE:
            problems.append(
                f"{collection} is interface state (a sort order, an open view), not task progress; "
                "a grader cannot score a decisive collection the worker sets without doing the work"
            )
        elif collection_records(states[app][key]) == 0:
            problems.append(
                f"{collection} holds no records, so there is nothing for the team to decide among"
            )
    return problems


def feature_cell_report(folder):
    """Each accepted task whose decisive collections the seeded world cannot carry. Read-only.

    The check stage's one line. A task listed here cannot be graded, and the cheapest moment to
    say so is before its golden, grader and calibration calls are paid for; for a company whose
    every task is listed it is the difference between a retirement and a rewrite. Returns
    ``{task_id: [reason, ...]}``, empty when nothing is seeded yet.
    """
    folder = Path(folder)
    apps = read(folder / "apps.json")["apps"]
    states, identity = {}, {}
    for app in apps:
        path = _local(folder, app.get("state_file") or f"world/{app['app_id']}.state.json")
        if path.is_file():
            states[app["app_id"]] = read(path)
            if app.get("identity_key"):
                identity[app["app_id"]] = app["identity_key"]
    if not states:
        return {}
    report = {}
    for path in sorted((folder / "tasks").glob("*/workflow.json")):
        if path.parent.name.startswith("_"):
            continue
        workflow = read(path)
        cell = (workflow.get("feature_cell") or {}).get("collections") or []
        problems = feature_cell_problems(cell, states, identity)
        if problems:
            report[workflow.get("id", path.parent.name)] = problems
    return report


def validate_seeded_records(task, snapshot):
    """Every input must resolve in its named collection; every feature cell needs an input."""
    index = snapshot["app_index"]
    missing, covered = [], set()
    for mention in task.initial_materials:
        match = REFERENCE.fullmatch(mention)
        if match is None:
            missing.append(mention)
            continue
        app, collection, rid = match.group("app", "collection", "id")
        hits = [r for r in index.get(app, {}).get(collection, []) if str(r["id"]) == rid]
        if len(hits) != 1:
            missing.append(mention)
        else:
            covered.add(f"{app}.{collection}")
    cell = task.feature_cell.model_dump() if hasattr(task.feature_cell, "model_dump") else task.feature_cell
    for collection in cell["collections"]:
        if collection not in covered:
            missing.append(f"{collection}#<decisive record required>")
    if missing:
        raise ValueError(
            f"{task.id}: missing or ambiguous seeded record ids: {', '.join(sorted(set(missing)))}"
        )
    # The grader author's and the golden author's own refusals, applied here instead of five
    # stages later: a rejected design costs one review, a rejected grader costs a world.
    problems = feature_cell_problems(
        cell["collections"], snapshot["states"], snapshot.get("identity_keys") or {}
    )
    if problems:
        raise ValueError(f"{task.id}: decisive collections cannot carry this task: {'; '.join(problems)}")


def _review_batch(
    models, review_instructions, bounded, company, tasks, evidence, excerpts, existing, receipt
):
    """The batch review, exactly as it was; extracted so the refusal above has one thing to guard."""
    return review.review(
        models,
        review_instructions
        + "\nFrozen seeded workspace (verify feasibility against these records):\n"
        + json.dumps(bounded, ensure_ascii=False),
        company,
        tasks,
        evidence,
        excerpts,
        {t.id: [{"workflow_id": w["id"], "workflow": w} for w in existing] for t in tasks},
        [receipt["model"]],
        policy="fresh_session",
        receipts=[receipt],
    )


def _nothing_mined(skill, review_skill, reason):
    """The report of a run that mined nothing for a reason no retry can change.

    Shaped like the report of a run that mined something, so the caller reads one shape, with
    ``oversized`` saying why it is empty. It writes no author-*.json receipt and does not touch
    MANIFEST.json: nothing was published, and re-dating the manifest would make the stages below it
    stale for a run that changed nothing.

    The shape it was identical to is the one that matters: ``MINE.json`` recorded
    ``requested: 2, accepted: []`` with no reason, exactly what a designer that simply accepted
    nothing writes. ``outcome: refused`` with the ceiling's own words is the difference, and it is
    the shared word every other stage's receipt now carries, so the driver does not need to know
    that this stage spells its refusal ``oversized``.
    """
    return {
        "authored_at": now(),
        **Receipt.refused(reason).body,
        "accepted": [],
        "rejected": [],
        "receipt": None,
        "skill": skill,
        "review_skill": review_skill,
        "decision": None,
        "oversized": reason,
    }


def oversized_mining(folder, what, exc):
    """Record a mining call the provider refused, and hand the caller a reason instead of a raise.

    ``PromptTooLarge`` is raised before dispatch and nothing on this path caught it, so the driver's
    mine step ran the same payload again every loop: **12 call ids across 12 companies reached 234
    attempts**, each failing in under a second, none of which could ever have succeeded -- the other
    59% of the 396 attempts the levers document attributed to the app-state author. A refusal the
    caller cannot act on has to be a receipt.

    Returning rather than raising is what ends the retry: ``author-tasks`` exits 0 with nothing
    accepted, the driver's mine step sees no new task and no failure, and writes its MINE.json --
    the marker that keeps a world to one attempt. The diagnostic lands in tasks/_rejected beside the
    ``invalid-*`` drafts, and the reason goes in the report so an empty run cannot read as a full one.
    """
    reason = (
        f"{what}: {exc}. The seeded-world payload is bounded to {PAYLOAD_BUDGET:,} characters, so a "
        "prompt past the ceiling means the dossier, the source excerpts or the skill grew; nothing "
        "this stage can shrink, and no number of retries changes it."
    )
    write(
        folder / "tasks" / "_rejected" / f"oversized-{digest(reason)}.json",
        {"at": now(), "call": what, "size": getattr(exc, "size", None), "error": str(exc), "reason": reason},
    )
    print(f"warning: author-tasks mined nothing: {reason}", file=sys.stderr)
    return reason


def author_tasks(root, folder, count, models=None):
    """Design and review a batch before publishing; never change seeded records.

    Invalid batches raise and retain diagnostic drafts under tasks/_rejected.
    Semantic rejections are returned as IDs and retained there with their review.
    Existing accepted tasks reserve both their outline and feature cell.
    """
    root, folder = Path(root), Path(folder)
    if type(count) is not int or count < 1:
        raise ValueError("count must be a positive integer")
    manifest = read(folder / "MANIFEST.json")
    snapshot = seeded_snapshot(folder, root=root)
    company = Company.model_validate(read(folder / "company.json"))
    existing = [
        read(p)
        for p in sorted((folder / "tasks").glob("*/workflow.json"))
        if not p.parent.name.startswith("_")
    ]
    used = {w["outline_id"] for w in existing}
    eligible = [o.id for o in company.outlines if o.id not in used]
    if count > len(eligible):
        raise ValueError(f"requested {count} tasks but only {len(eligible)} unused outlines remain")
    config = load_config(root)
    models = models if models is not None else Models(config, folder / "tasks")
    instructions, skill = load_skill(root, "company-workflows")
    review_instructions, review_skill = load_skill(root, "company-review")
    if snapshot.get("accepted_world"):
        from .task_assessment import TASK_RULES

        instructions += "\n" + TASK_RULES
        review_instructions += "\n" + TASK_RULES
    evidence, excerpts, _ = research_inputs(root, folder, company)
    # Limit eligibility without mutating the frozen dossier presented to the model.
    selected = eligible[:count]
    # Built once: the designer and the reviewer are shown the same bounded payload, and re-deriving
    # it re-walks and re-serialises up to PAYLOAD_BUDGET characters of record index for no gain.
    bounded = bounded_snapshot(snapshot)
    try:
        _, tasks, receipt, decision = workflows.design(
            models,
            instructions,
            company,
            evidence,
            Catalogs(root / "catalogs"),
            excerpts,
            count,
            selected=selected,
            # The designer already has these tasks' cells reserved, but not their titles, so it
            # could spend a design and a review proposing the same kind of day the reviewer then
            # turns down as a variant. avoid_task_themes is the field the skill reads for this.
            coverage={"accepted_task_titles": [w["title"] for w in existing if w.get("title")]},
            allow_amendment=False,
            # The designer needs the same schema access Stage 1 had. Without it, it cannot confirm
            # an outline's apps support the work and correctly refuses to design anything, which is
            # what stopped every mining call: "neither schema nor a callable read tool was supplied".
            read_only_tools=True,
            workflow_version="2",
            seeded_world=bounded,
            # The index carries ids, titles and dates; read_seed serves the record bodies the
            # designer needs to tell whether a collection can carry the decision.
            seed_states=snapshot["states"],
            reserved_cells=[w["feature_cell"] for w in existing if w.get("feature_cell")],
        )
    except ModelOutputInvalid as exc:
        write(
            folder / "tasks" / "_rejected" / f"invalid-{digest(exc.raw)}.json",
            {"error": str(exc), "draft": exc.raw, "receipt": exc.receipt},
        )
        raise
    except PromptTooLarge as exc:
        # Terminal, recorded, and not raised: see ``oversized_mining``. Nothing has been written, so
        # the world keeps the tasks it already has and the stage ends having mined none.
        return _nothing_mined(skill, review_skill, oversized_mining(folder, "design", exc))
    try:
        for task in tasks:
            validate_seeded_records(task, snapshot)
            if snapshot.get("accepted_world"):
                from .task_assessment import validate_assessment

                validate_assessment(task, snapshot)
            required = {
                a
                for s in company.software
                if s.id in task.software_requirement_ids
                for a in s.catalog_app_ids
            }
            if required - snapshot["app_index"].keys():
                raise ValueError(
                    f"{task.id}: required apps are not seeded: {sorted(required - snapshot['app_index'].keys())}"
                )
    except ValueError as exc:
        for task in tasks:
            write(folder / "tasks" / "_rejected" / task.id / "workflow.json", task.model_dump())
            write(folder / "tasks" / "_rejected" / task.id / "rejection.json", {"error": str(exc)})
        raise
    review_snapshot = bounded
    if snapshot.get("accepted_world"):
        from .task_assessment import assessment_sources

        review_snapshot = {
            **bounded,
            "task_sources": {
                task.id: assessment_sources(
                    task.assessment.model_dump(), snapshot["states"], task.initial_materials
                )
                for task in tasks
            },
        }
    try:
        reviewed = _review_batch(
            models,
            review_instructions,
            review_snapshot,
            company,
            tasks,
            evidence,
            excerpts,
            existing,
            receipt,
        )
    except PromptTooLarge as exc:
        # The designs exist but cannot be reviewed, and an unreviewed task is never published. They
        # are kept as rejected drafts so the work is visible, and the stage ends having mined none.
        for task in tasks:
            write(folder / "tasks" / "_rejected" / task.id / "workflow.json", task.model_dump())
        return _nothing_mined(skill, review_skill, oversized_mining(folder, "review", exc))
    for relative, sha in snapshot["provenance"].items():
        if digest(_local(folder, relative).read_bytes()) != sha:
            raise ValueError(f"seeded world changed during authoring: {relative}")
    if snapshot.get("accepted_world"):
        from .world_acceptance import accepted_world

        accepted_world(root, folder)
    verdicts = {t["workflow_id"]: t for t in reviewed["review"]["tasks"]}
    accepted, rejected = [], []
    for task in tasks:
        ok = reviewed["review"]["company_verdict"] == "accept" and verdicts[task.id]["verdict"] == "accept"
        directory = folder / "tasks" / task.id if ok else folder / "tasks" / "_rejected" / task.id
        if ok and directory.exists():
            raise ValueError(f"task already exists: {task.id}")
        write(directory / "workflow.json", task.model_dump())
        if ok:
            write(directory / "assignment.json", task.assignment())
            if snapshot.get("accepted_world") and task.assessment is not None:
                write(directory / "assessment.json", task.assessment.model_dump())
                write(directory / "WORLD.json", {"frozen_hash": digest(read(folder / "world/FROZEN.json"))})
        write(directory / "review.json", reviewed)
        (accepted if ok else rejected).append(task.id)
    # A run that published nothing is a verdict about these designs, not a finished mining pass:
    # the reviewer looked and turned every one of them down. Saying which of the four this was is
    # what tells that apart from the over-ceiling refusal above, which no retry can change, and from
    # a provider outage, which says nothing about the designs at all.
    outcome = (
        Receipt.passed()
        if accepted
        else Receipt.refused(
            f"the review accepted none of {len(tasks)} designs: {reviewed['review'].get('company_verdict')}"
        )
    )
    report = {
        "authored_at": now(),
        **outcome.body,
        "accepted": accepted,
        "rejected": rejected,
        "receipt": receipt,
        "skill": skill,
        "review_skill": review_skill,
        "decision": decision,
        "seeded_provenance": snapshot["provenance"],
    }
    write(folder / "tasks" / f"author-{digest(report)}.json", report)
    manifest["tasks"] = sorted({w["id"] for w in existing} | set(accepted))
    manifest["stages"]["stage1_design"] = "selected" if manifest["tasks"] else "review_failed"
    manifest["hashes"] = {
        str(p.relative_to(folder)): digest(p.read_bytes())
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p != folder / "MANIFEST.json"
    }
    write(folder / "MANIFEST.json", manifest)
    return report


def review_candidate(root, folder, candidate, *, models=None):
    """Review a supplied task repair against the same frozen world; never reauthor it."""
    from company_envs.schemas import validate_workflow

    from .task_assessment import TASK_RULES, assessment_sources, validate_assessment

    root, folder = Path(root).resolve(), Path(folder).resolve()
    snapshot = seeded_snapshot(folder, root=root)
    if not snapshot.get("accepted_world"):
        raise ValueError("Candidate review requires a frozen staged world")
    task = workflows.Workflow.model_validate(read(candidate))
    company = Company.model_validate(read(folder / "company.json"))
    target = _local(folder, f"tasks/{task.id}")
    if target.exists():
        raise ValueError("Accepted tasks cannot be replaced by candidate review")
    config = load_config(root)
    workflows.validate_execution_support(company, task, config)
    validate_workflow(company, task, version="2", minimum_workers=3)
    workflows.check_plain_english(task)
    workflows.check_contribution_apps(company, task, worker_apps=snapshot["worker_apps"])
    workflows.check_criteria_are_about_records(task)
    validate_seeded_records(task, snapshot)
    validate_assessment(task, snapshot)
    existing = [
        read(p)
        for p in sorted((folder / "tasks").glob("*/workflow.json"))
        if not p.parent.name.startswith("_")
    ]
    if task.outline_id in {w["outline_id"] for w in existing}:
        raise ValueError("The outline already has an accepted task")
    packet = {
        **bounded_snapshot(snapshot),
        "task_sources": {
            task.id: assessment_sources(
                task.assessment.model_dump(), snapshot["states"], task.initial_materials
            )
        },
    }
    instructions, skill = load_skill(root, "company-review")
    evidence, excerpts, _ = research_inputs(root, folder, company)
    models = models or Models(config, folder / "tasks")
    # Preserve the actual original author session. A local field repair is a
    # recorded revision, not a fabricated provider call or session identifier.
    authors = [read(p) for p in sorted((folder / "tasks").glob("author-*.json"))]
    original = next((r for r in authors if task.id in r.get("rejected", [])), None)
    if original is None:
        raise ValueError("Candidate repair requires a previously rejected task author receipt")
    supplied = {
        **original["receipt"],
        "candidate_hash": digest(task.model_dump()),
        "revision_source": "supplied_local_repair",
    }
    result = _review_batch(
        models,
        instructions + "\n" + TASK_RULES,
        packet,
        company,
        [task],
        evidence,
        excerpts,
        existing,
        supplied,
    )
    if seeded_snapshot(folder, root=root)["provenance"] != snapshot["provenance"]:
        raise ValueError("Frozen world changed during candidate review")
    accepted = (
        result["review"]["company_verdict"] == "accept"
        and result["review"]["tasks"][0]["verdict"] == "accept"
    )
    checkpoint = folder / "tasks/_candidates" / task.id / digest(task.model_dump())
    write(checkpoint / "workflow.json", task.model_dump())
    write(checkpoint / "review.json", result)
    report = {
        "at": now(),
        "accepted": accepted,
        "task_id": task.id,
        "candidate_hash": digest(task.model_dump()),
        "review_skill": skill,
    }
    write(checkpoint / "RESULT.json", report)
    if accepted:
        for name, value in {
            "workflow.json": task.model_dump(),
            "assignment.json": task.assignment(),
            "assessment.json": task.assessment.model_dump(),
            "WORLD.json": {"frozen_hash": digest(read(folder / "world/FROZEN.json"))},
            "review.json": result,
        }.items():
            write(target / name, value)
        manifest = read(folder / "MANIFEST.json")
        manifest["tasks"] = sorted({w["id"] for w in existing} | {task.id})
        manifest["stages"]["stage5_tasks"] = "reviewed"
        manifest["hashes"].update(
            {str(p.relative_to(folder)): digest(p.read_bytes()) for p in target.iterdir() if p.is_file()}
        )
        write(folder / "MANIFEST.json", manifest)
    return report
