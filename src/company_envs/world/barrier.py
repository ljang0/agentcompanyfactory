"""Check whether seeded records preserve the task's division of work.

Mailbox relevance uses capitalized names in the brief, not a language model.
This checks the supplied recipient policy and exact file copies; it does not
prove runtime permissions or that every specialist is necessary.

A finding that could not be taken says so rather than failing: its ``ok`` is None, it carries the
``missing`` files by name, and the report lists them under ``unmeasured``. Everything here reads the
seeded world, so before the seed there is nothing to read -- and a missing file used to come back as
``{"ok": False, "reason": "[Errno 2] No such file or directory: .../identities.json"}``, which is
the receipt a real mailbox defect writes. Measured over the 99 companies on 2026-09-10: of 35 mailbox
findings, 25 passed and **all 10 failures were this**, eight companies with no world yet and not one
true mailbox defect in the cohort. An unmeasured finding is still not a pass -- a world that does not
exist has earned nothing -- so the report's own ``ok`` stays false while the reason stops lying.
"""

import hashlib
import json
import re
from pathlib import Path

from company_envs.receipt import PASSED, Receipt, rollup
from company_envs.storage import read, write

from .state_seed import STANDARD_APPS

# A shared spreadsheet is not a system of record. Research already rejects a dossier whose only
# domain app is this one, so a barrier whose only gate is "who has Sheets" proves nothing about
# the division of work; it is named here so both the assignment check and this report can say so.
SPREADSHEET_APP = "google_sheets_mock"

_NAME = re.compile(r"\b[A-Z][\w&'-]*(?:[ \t]+[A-Z][\w&'-]*)*\b")
_DATES = {
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
}
_OPENERS = {
    "Please",
    "Resolve",
    "Review",
    "Check",
    "Prepare",
    "Confirm",
    "Send",
    "Ask",
    "Fix",
    "The",
    "We",
    "I",
}


def _names(brief):
    """Keep capitalized phrases, omitting dates and common request openers."""
    names = set()
    for match in _NAME.finditer(brief):
        words = match.group().split()
        while words and words[0] in _OPENERS:
            words.pop(0)
        if words and not set(words) & _DATES:
            names.add(" ".join(words))
    return sorted(names)


def _addresses(value):
    """Read strings and native recipient objects without treating names as addresses."""
    if isinstance(value, dict):
        return _addresses(value.get("email", ""))
    if isinstance(value, list):
        return set().union(*(_addresses(item) for item in value))
    if isinstance(value, str):
        return set(re.findall(r"[\w.+%-]+@[\w.-]+\.[a-z]{2,}", value.casefold()))
    return set()


# What each half of the seeded-world check reads, and what only the seed writes. Named here so a
# finding can say which file it wanted instead of quoting an errno, and so a caller can ask first.
MAILBOX_INPUTS = ("world/identities.json", "world/gmail_mock.state.json")
DESKTOP_INPUTS = ("world/materials",)
SEED_RECEIPT = "world/SEED.json"  # the seeder's own receipt: before it there is no world to read


def missing_inputs(folder, relatives=MAILBOX_INPUTS):
    """Which of RELATIVES the folder does not have, in the order given."""
    folder = Path(folder)
    return [relative for relative in relatives if not (folder / relative).exists()]


def _desktops_unmeasured(folder):
    """What the desktop comparison is still waiting for, or nothing when it can be taken.

    Absent materials alone are not the test. A seeded company whose workers happen to carry no
    desktop files has been compared and had nothing to compare, which is a verdict -- the agreement's
    own fixtures are that shape. It is unmeasured only while the world itself is unwritten, so the
    seeder's receipt decides, not the directory. On disk 2026-09-10: all 60 seeded worlds have
    materials and none of the 39 unseeded ones do.
    """
    missing, unwritten = missing_inputs(folder, DESKTOP_INPUTS), missing_inputs(folder, (SEED_RECEIPT,))
    return [*missing, *unwritten] if missing and unwritten else []


def _unmeasured(missing, what):
    """A finding for a check that could not be taken: not a pass, and not a verdict about WHAT.

    ``ok`` is None so nothing reads it as a pass, and so ``all()`` over the findings still refuses the
    report the way it always did. The discipline is the one the controller's skipped steps follow: a
    row that records no work must not be shaped like a row that records work.

    This was the pipeline's best statement of that rule and is now ``Receipt.unmeasured`` in
    ``company_envs.receipt``, which every stage shares: the constructor is what refuses to file an
    unmeasured finding that does not name what it was waiting for. ``unmeasured: True`` stays beside
    the new ``outcome`` because the agreement and the cohort report read this shape on disk.
    """
    return Receipt.unmeasured(
        missing,
        what,
        reason=f"{what} was not measured: the seed has not written {', '.join(missing)}",
        unmeasured=True,
    ).body


def _verdict(ok, reason):
    """A finding the check *did* take: passed or refused, never silent about which.

    The pair to ``_unmeasured``. Both go through ``company_envs.receipt`` so that a reader holding
    one finding from one of a dozen stages can tell which of the four outcomes it is without knowing
    the stage -- ``outcome`` says so in a word, and ``ok`` keeps the meaning every gate already
    reads. A passing finding may still carry a reason: the mailbox barrier passes *because* some
    correspondence sits outside the manager's mailbox, and saying so is the evidence.
    """
    return (Receipt(PASSED, reason=reason) if ok else Receipt.refused(reason)).body


def _mailbox(folder, workflow, held):
    manager = workflow["manager_id"]
    identities = read(folder / "world/identities.json")
    emails = {w: _addresses(identities.get(w, {}).get("gmail_mock", {})) for w in held}
    state = read(folder / "world/gmail_mock.state.json")
    names = _names(workflow.get("brief", ""))
    patterns = [re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE) for name in names]
    relevant = []
    visible = []
    exclusive = {w: [] for w in held if w != manager}
    # Gmail stores drafts in emails; older states also use the drafts collection.
    for collection in ("emails", "drafts"):
        for index, message in enumerate(state.get(collection, [])):
            recipients = _addresses(message.get("to")) | _addresses(message.get("cc"))
            reference = f"gmail_mock.{collection}#{message.get('id', index)}"
            manager_receives = bool(recipients & emails[manager])
            for worker, references in exclusive.items():
                if not manager_receives and recipients & emails[worker]:
                    references.append(reference)
            text = json.dumps(message, ensure_ascii=False)
            if any(pattern.search(text) for pattern in patterns):
                relevant.append(reference)
                if manager_receives:
                    visible.append(reference)
    alone = bool(relevant) and relevant == visible
    complete = bool(relevant) and all(emails.values())
    return {
        **_verdict(
            complete and not alone,
            "All relevant mail reaches the manager."
            if alone
            else (
                "Missing identity emails or no correspondence matches the brief."
                if not complete
                else "Some relevant correspondence is outside the manager's mailbox."
            ),
        ),
        "names_from_brief": names,
        "relevant_emails": relevant,
        "manager_visible_emails": visible,
        "specialist_only_emails": exclusive,
        "manager_could_finish_alone": alone,
    }


def _domain_apps(folder):
    """The company's non-standard apps, or nothing when apps.json is missing or unreadable."""
    try:
        apps = read(folder / "apps.json")["apps"]
    except (OSError, KeyError, TypeError, ValueError):
        return set()
    return {a["app_id"] for a in apps if isinstance(a, dict) and a.get("app_id")} - set(STANDARD_APPS)


def _desktop_hashes(folder, worker):
    hashes = {}
    for path in sorted((folder / "world/materials" / worker).rglob("*")):
        if path.is_file():
            hashes.setdefault(hashlib.sha256(path.read_bytes()).digest(), []).append(
                str(path.relative_to(folder))
            )
    return hashes


def check_barrier(folder):
    """Write and return BARRIER.json; each finding includes its own ok and evidence."""
    report = barrier_report(folder)
    write(Path(folder) / "world/BARRIER.json", report)
    return report


def barrier_report(folder):
    """The same report, computed and not written.

    Reading a company must not write into it -- the lesson REVIEW-SHEET.md taught, where inspecting a
    company advanced its apparent position by leaving a driver marker behind. Before the seed this is
    the entry point to use: it needs no ``world/`` to exist, and writing BARRIER.json there would
    create the directory and leave a half-unmeasured artifact in it. The two findings that read no
    world at all -- ``exclusive_apps`` and ``input``, decided by tasks/*/workflow.json and apps.json --
    are therefore computable before a seed is paid for. Nothing calls it that way yet, and the reason
    is a measurement: 0 of the 98 companies with an ASSIGN.json fail either of them, because
    assign-apps already settles the decisive collections it would re-check (2026-09-10).
    """
    folder = Path(folder)
    findings = []
    records = _domain_apps(folder) - {SPREADSHEET_APP}
    desktops = {}
    tasks = [p for p in sorted(folder.glob("tasks/*/workflow.json")) if not p.parent.name.startswith("_")]
    for task in tasks:
        try:
            workflow = read(task)
            manager = workflow.get("manager_id")
            held = {}
            for contribution in workflow.get("contributions", []):
                held.setdefault(contribution["worker_id"], set(STANDARD_APPS)).update(
                    contribution.get("apps") or []
                )
            cells = (workflow.get("feature_cell") or {}).get("collections", [])
            if manager not in held or len(held) < 2 or not cells:
                raise ValueError("The task needs a manager, specialists and decisive collections.")
            owners = {
                cell: sorted(w for w, apps in held.items() if cell.split(".")[0] in apps)
                for cell in cells
                if cell.split(".")[0] not in STANDARD_APPS
            }
            alone = all(cell.split(".")[0] in held[manager] for cell in cells)
            manager_only = [cell for cell, workers in owners.items() if workers == [manager]]
            # What actually keeps the manager out. When only the spreadsheet does, the task is
            # gated on an app every office already has: a weak gate, reported rather than failed,
            # because nothing downstream of here can move a decisive collection.
            gating = sorted(cell for cell, workers in owners.items() if manager not in workers)
            weak = bool(gating) and not any(cell.split(".")[0] in records for cell in gating)
            findings.append(
                {
                    "task": task.parent.name,
                    "kind": "exclusive_apps",
                    "detail": {
                        **_verdict(
                            not alone and not manager_only,
                            "The manager holds every decisive collection."
                            if alone
                            else f"Only the manager holds {', '.join(manager_only)}."
                            if manager_only
                            else "A specialist holds a decisive collection the manager cannot reach.",
                        ),
                        "workers_by_collection": owners,
                        "manager_only_collections": manager_only,
                        "manager_could_finish_alone": alone,
                        "gating_collections": gating,
                        "spreadsheet_is_the_only_gate": weak,
                        "systems_of_record_unused": sorted(records) if weak else [],
                    },
                }
            )
            # A desktop collision is reported when it is found, so finding nothing used to mean two
            # different things: the desktops differ, or there are no desktops. Before the seed it is
            # always the second, and silence read as a pass -- measured 2026-09-10, **30 of the 39
            # companies with no world reported barrier ok true**, every one of them on exclusive_apps
            # alone, because the desktop comparison compared nothing and no decisive collection of
            # theirs lives in gmail. The agreement asked for worker_apps.json as well and so was not
            # fooled, but the artifact said a division of work held that nobody had looked at.
            if missing := _desktops_unmeasured(folder):
                findings.append(
                    {
                        "task": task.parent.name,
                        "kind": "desktop",
                        "detail": _unmeasured(missing, "the desktop barrier"),
                    }
                )
            else:
                manager_hashes = desktops.setdefault(manager, _desktop_hashes(folder, manager))
                for worker in sorted(set(held) - {manager}):
                    for digest, paths in desktops.setdefault(worker, _desktop_hashes(folder, worker)).items():
                        if digest in manager_hashes:
                            findings.append(
                                {
                                    "task": task.parent.name,
                                    "kind": "desktop",
                                    "detail": Receipt.refused(
                                        f"{worker} and the manager hold the same bytes",
                                        specialist=worker,
                                        manager_paths=manager_hashes[digest],
                                        specialist_paths=paths,
                                    ).body,
                                }
                            )
            if set(cells) & {"gmail_mock.emails", "gmail_mock.drafts"}:
                # Absent and unreadable are different answers. A world the seed has not written yet
                # is nothing to judge; a file that is there and will not parse is a half-written
                # state, which is a defect and stays one.
                if missing := missing_inputs(folder):
                    detail = _unmeasured(missing, "the mailbox barrier")
                else:
                    try:
                        detail = _mailbox(folder, workflow, held)
                    except (OSError, ValueError) as exc:
                        # ``from_exception`` applies the split models.py already encodes: a state
                        # that will not parse is a ValueError and so a refusal about this world,
                        # while an OSError on a file the glob just listed is a fault about the host
                        # and says nothing about the mailbox. Both keep ``ok: False``.
                        detail = Receipt.from_exception(exc, "the mailbox barrier").body
                findings.append({"task": task.parent.name, "kind": "mailbox", "detail": detail})
        except (OSError, ValueError) as exc:
            findings.append(
                {
                    "task": task.parent.name,
                    "kind": "input",
                    "detail": Receipt.from_exception(exc, "the task's own inputs").body,
                }
            )
    # ``ok`` is unchanged and deliberately so: an unmeasured finding carries None, all() refuses it,
    # and a company whose world does not exist goes on reading ok false. What changes is that a
    # reader can tell which of the two it is without parsing an errno out of a reason string.
    #
    # ``outcome`` is the roll-up of the findings in the shared vocabulary, and it is deliberately not
    # the same question as ``ok``: a report with no tasks at all, or one whose only findings were
    # unmeasured, reads ``outcome: unmeasured`` with ``ok: false``. That pair is the point. ``ok``
    # answers "may this world ship" and has to stay false, because a world that does not exist has
    # earned nothing; ``outcome`` answers "what did this run learn", and there the honest answer is
    # nothing. ``rollup`` is where "no findings at all is not a pass" lives for every stage now.
    whole = rollup([f["detail"] for f in findings], "barrier findings") if tasks else rollup([], "tasks")
    return {
        "ok": bool(tasks) and all(f["detail"]["ok"] for f in findings),
        "outcome": whole.outcome,
        **({"reason": whole.reason} if whole.reason else {}),
        "unmeasured": sorted({name for f in findings for name in f["detail"].get("missing") or ()}),
        "findings": findings,
    }
