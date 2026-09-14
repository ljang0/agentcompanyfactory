"""Turn selected Stage 1 companies into seeded, checked, calibrated, VM-verified company folders.

The driver keeps no state of its own. INPUTS and STEPS below declare, for every stage, the
artifacts it reads and the marker file each of its commands writes. One rule decides what runs:

    a step runs when its marker is missing, or older than any input of its stage, or when the
    marker exists but does not record an accepted result (render: RENDER.json ok; calibrate:
    accepted with scope all_checks; vm: every task's controller status done, one checkpoint per
    task under runtime/tasks/<task>/).

An input is an artifact the stage reads -- and, for the cheap deterministic markers, the code that
decides that one marker's verdict: the checker for world/CHECKS.json, the seeder for
world/worker_apps.json, the proxy and hub for RENDER.json and VISUAL.json, the gate's own rules for
REPORT-readability.json, and names, review_sheet and agreement for their own three. deciding_code()
holds that half, per marker rather than per stage, and needed() composes the two. The expensive
markers list no code on purpose and NO_CODE_INPUT says which and why; the comment above PROXY_CODE
argues both halves, why the line falls there, and why the list is not derived from the imports.

Order matters as much as the rule: a check that needs no world is made before the seed pays for one.
Two agreement conditions are decided by the briefs the rewrite stage leaves final -- `readable`
(REPORT-readability's brief category) and the half of `evergreen` that reads a brief for relative
time -- and neither has a repair stage between it and AGREEMENT.json, so briefs_ready decides them
while the repair is still two model calls rather than a re-seed. Everything the mechanical check
judges is a record, so none of world-check can move: it already runs inside the seed, over the world
as it is about to be written, where the author can still act on it.

Terminal outcomes live in the artifacts too, never in driver memory: a company whose briefs read
dense or name a relative time stops at seed before paying for one (briefs_not_plain; a seeded world
is never stopped here, since by then the repair costs the world); a world whose review verdict
is not accept gets one `review-repair` (the reviewers' findings go back to the apps owning the
cited records, world/REVIEW-REPAIR.json is its receipt) and stops at seed if it is still not
accepted; a world whose CHECKS.json has errors gets one `world-repair` (its
authors receive the findings, bounded rounds) and a second check, and stops at check if that
still has errors; a task with GOLDEN_FAILED.json, GRADER_FAILED.json or
CALIBRATION_FAILED.json stops at calibrate; a seed-world that fails three times without writing
SEED.json leaves world/SEED_FAILED.json and stops. Each of those receipts has to record a real
attempt to count: one that records none -- zero rounds, no call made, a round that died on a hub
that was not serving, a kill from outside this driver -- is an empty run, and spent() and strikes()
give the stage its attempt back once rather than treating the file's existence as the verdict.
A world whose apps do not render in a browser
(runtime/RENDER.json ok false) stops at render and is retried each loop; a changed state file or
hub patch makes that marker stale. A render that failed only on the content floor -- a first view
showing none of the company's own values -- is one a repair can reach, so it gets one `world-repair`
(runtime/RENDER-REPAIR.json is the receipt) and the next loop serves and renders the rewritten world. Every company is revisited each loop, so changing the artifact
is all it takes to continue. Recipes, none of which needs a driver flag:

    redo the VM stage for a task    rename or delete runtime/tasks/<task>/ (CONTROLLER.json and controller/)
    re-seed a rejected world        rename companies/<c>/world/ (say to world.rejected-<date>/)
    retry a seed after 3 failures   delete world/SEED_FAILED.json
    clear briefs_not_plain          .venv/bin/python -m company_envs rewrite-briefs <folder> --again
                                    (the gate keeps no marker: the next loop reads the new briefs)
    retry a spent repair or golden  delete the receipt (world/REPAIR.json, world/REVIEW-REPAIR.json,
                                    runtime/RENDER-REPAIR.json, tasks/<task>/GOLDEN_FAILED.json)
                                    -- or fix the code behind it, which forgives an empty run itself
    mine a company's outlines again delete tasks/_mined/MINE.json (raise design.tasks_per_company first)
    never mine a company            write its tasks/_mined/MINE.json by hand, with a mined_at field
    fresh cohort                    start a driver with a new NAME on its runs (or --only ids)
    take over from a dead driver    start a new driver on the same runs; a dead pid's locks are taken

Between the seed stage and the check stage, a world the reviewers accepted is topped up to
design.tasks_per_company tasks from the business-cycle outlines its design left unused: a seeded
world costs hundreds of model calls and carries a real rejection risk, while a task authored
against a world that already passed every gate costs two calls and almost none. The mine stage
authors them, gives them the same plain brief and app binding the first tasks got (by task id, so
the company-wide markers keep their dates) and records what it did in tasks/_mined/MINE.json. That
receipt is the marker: a world with no unused outlines writes it saying so and never mines again.
Nothing in the stage touches REWRITE.json, ASSIGN.json or the world, so mining never re-seeds.

Housekeeping, once per loop, on companies this driver owns and is not running: the hardlinked app
work copy under experiments/batch/<name>/work/<company> goes when the company is done or stopped at a
terminal outcome; set-aside controller runtimes (runtime/controller.<tag>/) are reduced to their
evidence files under EVIDENCE/; world archives left by re-seeding (world.old-*, world.rejected-*)
go after seven days unless --keep-archives. One JSON line per loop goes to health.jsonl (rotated at
5 MB). Every action is written to the company log; each is idempotent.

Several drivers may run at once on disjoint --only lists or different runs: a lock file holding the
owner's pid (experiments/batch/locks/<company>) keeps two live drivers off one company.
experiments/batch/<name>/STATUS.json is a view of the artifacts, written once per loop and never
read back; <company>.log records every command with its start time. Commands inherit this process's
environment (CODEX_HOME included). Nothing here calls a model directly.

Usage:
    .venv/bin/python scripts/batch_companies.py NAME --runs RUN_ID [RUN_ID ...] --parallel 6
        [--only ID ...] [--until 2026-09-08T01:00:00Z] [--work DIR] [--once] [--keep-archives]
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from company_envs.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CLI = [str(ROOT / ".venv" / "bin" / "python"), "-m", "company_envs"]
BATCH = ROOT / "experiments" / "batch"
WORLD_SRC = ROOT / "src" / "company_envs" / "world"
HUB_PATCHES = WORLD_SRC / "hub_patches.json"  # a render input for every company

# Which markers a code change makes stale, and which it must never.
#
# Every marker holds a verdict, and the verdict came out of code. When that code changes the
# verdict on disk can be wrong, so the marker has to go stale -- but re-running is not free, and
# the difference between the markers named below and the ones left out is the whole judgement:
#
#   a cheap deterministic marker lists the code that decides its verdict. A re-check, a re-render,
#   a re-read of the briefs, a dedupe, a worker-app sync, a sheet or an agreement costs seconds to
#   minutes of CPU and not one model call, and a wrong verdict left standing costs a company.
#
#   an expensive marker lists none of it. Seeding a world is ~33 hours of calls, a VM episode hours
#   of guest time; making either depend on a source file would re-seed the cohort or re-run the
#   fleet on every edit of the tree. So world/SEED.json, the calibrations and the checkpoints are
#   absent on purpose, and the way to redo one of them is still the recipe in the module docstring.
#   NO_CODE_INPUT names every one of them with the measured reason.
#
#   a gate lists nothing, because a gate keeps no marker and so has nothing that can go stale.
#   seed_not_failed, briefs_ready, review_accepted, checks_ok and not_rejected recompute their
#   verdict from the artifacts on every loop, which is the cheapest answer to this whole class: a
#   changed rule reaches every company on the next pass and there is no marker to delete.
#
# Measured 2026-09-10, before this existed: world_check.py changed four times that night and not
# one of the 60 CHECKS.json was re-dated; hub_app.py carried the browser storage-quota fix and
# none of the 20 RENDER.json or 12 VISUAL.json were. 100% of the verdicts in the cohort predated
# every fix of the night, so the whole sweep would have reached nobody.
#
# Why the code is listed per marker and not derived from the imports. Measured over the 295 commits
# of 2026-09-04..10, a digest of the transitive import closure of the deciding module would restale
# CHECKS.json on 111 of them, REPORT-readability.json on 114, RENDER.json on 115 and VISUAL.json on
# 122 -- against 28, 5, 36 and 6 for the lists below. The closure cannot be made finer, because ten
# modules of the world core are one import cycle -- world_check -> bulk_layer -> world_repair ->
# state_seed -> hub_app -> hub_identity -> world_check, with barrier, bulk, worker_apps and
# world_review inside it -- so each deciding module's closure is 18 to 23 of the package's 71. At
# this cohort that is 224 core-hours of re-checking, over 20,000 judge calls (16 a company, from the
# 234 on disk) and ~345 VM episodes a week, to catch verdict changes the four lists already catch.
# Ignoring docstrings and comments does not save it: one commit of the 295 touched only their prose,
# so a whole-file mtime is as precise as an AST digest here and needs no new marker format.
# What is derived instead is the audit: test_batching derives the module that *writes* each marker
# and fails when it is neither an input of that marker nor written down below as not deciding it.
#
# What an mtime cannot do, measured 2026-09-10. The dates this rule compares are not a record of when
# a verdict was reached. 6,207 of the 6,912 files under companies/ were created inside one three-minute
# window (15:09-15:11 UTC) by an operation outside the pipeline: birth, mtime and ctime are equal on
# every one of them, the directories holding them are two days older, and the only files that kept
# their real dates are the ones no stage writes (runtime/logs, VM traces). So:
#
#   a fix that landed before that window reads as already applied. 93 of the markers on disk carry a
#   date later than the last change to the module that decides them, and nothing can tell whether the
#   verdict was computed by that module or merely re-dated after it. The 42 world/BULK.json are the
#   only markers whose staleness survived, because their freshness is a value recorded in the marker
#   (none of the 42 carries a bulk_version at all, against BULK_VERSION 3). That is the one mechanism
#   here an ordinary copy, restore or archive extraction cannot defeat, and it is why BULK_VERSION
#   stays: a value in the marker, never a sidecar, since this driver keeps no state of its own.
#
#   105 of the 836 artifact comparisons on disk are exact nanosecond ties, which is not ordering at
#   all -- 72 ASSIGN.json against their REWRITE.json, 13 CHECKS.json against BULK.json, 11 VISUAL.json
#   against RENDER.json. fresh() resolves a tie to fresh, with a strict `>`, and it must stay strict:
#   reading a tie as stale rewrites those 72 ASSIGN.json, which then postdate world/SEED.json, and 36
#   seeded worlds re-seed at ~33 hours each, ~1,188 core-hours. The one-character hardening is the
#   most expensive change available here; test_batching pins the comparison for that reason.
#
# So the markers in deciding_code() carry a recorded value as well, which is the half that survives.
# Exactly those eight: a marker with no code list has no digest to record, and the four above the seed
# (company.json, REWRITE.json, ASSIGN.json, world/SEED.json) must never gain one, because an absent
# value read as stale is the re-seed NO_CODE_INPUT exists to prevent. The value is not a hand-bumped
# integer but a digest the writer takes over the same files this table names -- storage.decided_by --
# so it fires on exactly the commits an mtime fires on (28, 5, 36 and 6 in the week to 2026-09-10,
# since a commit that changes a file changes both) while being immune to a re-dating and to a
# checkout's false stale. The contract is bulk_ok's: the deciding module records the digest and
# exposes current(marker), recorded() asks it through the accept slot, and test_batching pins the
# module's DECIDES_THIS equal to this table's entry so the two halves cannot drift.
#
# Seven of the eight are migrated: CHECKS.json, REPORT-readability.json, AGREEMENT.json,
# REVIEW-SHEET.md, NAMES.json, RENDER.json and VISUAL.json. Measured on the cohort: adopting the
# first five restaled 48 markers and nothing else -- 42 dedupe-names at 1-9 s and 6 readability
# reports at 1-8 s, under 7 minutes of CPU, not one model call, and no state rewrite, since a world
# whose names are already claimed renames nothing. The other 199 of the 247 were already stale on
# their code. Doing it during the freeze is why it was nearly free: after the fleet re-runs, the same
# change costs a re-take of all seven, 20 browser renders and ~192 judge calls among them.
#
# The eighth declines one, and the reason generalises. world/worker_apps.json is a bare map whose
# top-level keys *are* the worker ids, read in 27 places across 14 modules, so a decided_by key would
# be enumerated as a worker named decided_by by every one of them. It is also the cheapest marker here
# to retake -- under 1 s, no cascade -- and state_seed.py's 42 commits in the week restale it on nearly
# every loop anyway, so its exposure to a re-dating is the shortest of the eight. The same collision
# between a receipt's field and a data namespace landed the other way one file over: STATUS.json keeps
# the word `outcome` because renaming it breaks its readers, and this marker declines the word
# `decided_by` because adding one breaks its readers. Both answers follow from who reads the keys.
#
# What the accept slot is not. It is a re-run trigger, not a gate: a marker it refuses makes the step
# run again. So a content check belongs here only when re-running is a plausible remedy for what the
# check found, which is true of every one present -- a layer laid by old code is re-laid, a failed
# render re-rendered, a state-only proof re-calibrated, an unfinished checkpoint resumed. Measured
# 2026-09-10, three verdicts that look like obvious additions and are not: receipt.passed on
# world/SEED.json refuses the 50 of 60 worlds whose status is seeded_review_failed and re-seeds them
# at ~33 hours each, ~1,650 core-hours, where review_accepted already stops them for free; `done is
# True` on AGREEMENT.json re-runs a 64 s agreement every loop for a company whose conditions have not
# changed, for ever, and a refused company is a verdict cohort_report already counts; `not blocking`
# on REPORT-readability.json re-reads prose no stage rewrites, which is the standing rule against
# blocking on a defect the pipeline cannot repair, pointed at staleness instead. A verdict whose
# remedy is to stop belongs in a gate row, and all three already have one.
#
# The proxy rewrites what it serves on the way out, so its code decides what a browser sees just
# as much as the recorded app patches do. hub_app.py is here because the storage shim and the
# write-success amendment live in it: a render or a visual verdict recorded before them was taken
# of different pages, 76 of 88 apps having quietly served demo data instead of the seeded world.
PROXY_CODE = (
    WORLD_SRC / "placeholder_images.py",
    WORLD_SRC / "hub_identity.py",
    WORLD_SRC / "hub_app.py",
)
# What decides a world's mechanical verdict: the checker, plus the field contract it reads -- data
# that makes findings appear and disappear exactly the way the code does (87 apps' fields landed in
# it on 2026-09-10, and every CHECKS.json on disk was written against the older contract).
CHECK_CODE = (WORLD_SRC / "world_check.py", ROOT / "catalogs" / "app_record_fields.json")
# The two gates' own logic. A change to what a render or a visual judge counts as a failure changes
# the verdict with no state file and no proxy file moving: the content floor landed on 2026-09-10 and
# reached every company only because placeholder_images.py happened to change in the same round.
RENDER_CODE = (WORLD_SRC / "render_check.py",)
VISUAL_CODE = (WORLD_SRC / "visual_judge.py",)
READABILITY_CODE = (WORLD_SRC / "readability.py",)
# The four markers that had no code input of any kind until 2026-09-10, and cost nothing to retake.
# Measured from the per-company logs: dedupe-names runs in 1 s (max 9), sync-worker-apps in under
# 1 s, review-sheet in 1 s, agreement in 64 s. Their deciding modules took 4, 42, 5 and 13 commits
# in the week to 2026-09-10 -- 56 commits between them -- and not one could re-date a marker, so
# every NAMES.json, worker_apps.json, REVIEW-SHEET.md and AGREEMENT.json on disk is older code's.
NAMES_CODE = (WORLD_SRC / "names.py",)
SYNC_CODE = (WORLD_SRC / "state_seed.py",)  # sync_worker_apps, the other step of the check stage
SHEET_CODE = (WORLD_SRC / "review_sheet.py",)
AGREEMENT_CODE = (WORLD_SRC / "agreement.py",)
# The code behind a receipt that may record no attempt at all; see spent().
REPAIR_CODE = (WORLD_SRC / "world_repair.py", ROOT / "src" / "company_envs" / "models.py")
SERVE_CODE = (WORLD_SRC / "hub_app.py",)
HOUR = 3600
SEED_FAILURE_CAP = 3  # a seed that fails the same way three times will not pass by retrying
TASKS_PER_COMPANY = 4  # what the mine stage tops a company up to when config.toml does not say
TERMINAL = (
    "review_not_accepted",
    "check_failed",
    "calibration_rejected",
    "grader_not_authored",
    "golden_not_authored",
    "seed_failed_repeatedly",
    "briefs_not_plain",
)
# What a set-aside controller runtime keeps (plus CONTROLLER*.json): the proofs, not the VM images.
EVIDENCE_FILES = ("TEACHER.json", "report.json", "calibration.json", "judge_bench.json", "events.jsonl", "episode-result.json")  # fmt: skip
ARCHIVE_DAYS = 7  # world.old-* and world.rejected-* live this long after being set aside
GUESTS_PER_SLOT = 5  # what one legacy vm_slots unit was worth: a manager and four workers on one company
HEALTH_CAP = 5_000_000  # health.jsonl rotates to health.jsonl.1 past this size
GUESTS = {}  # company -> guests this process holds for it; read by the health line


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def load(path):
    """The JSON content of PATH, or {} when it is missing, half-written or not JSON."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    """Atomic write: a full disk or a kill mid-write never leaves a truncated file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, path)


RETIRED = ("CALIBRATION_FAILED.json", "GRADER_FAILED.json", "GOLDEN_FAILED.json")
# The artifact that revives a retired task: the thing the refusal said could not be written, written
# after it. GOLDEN_FAILED.json is author-golden's receipt; golden.py:393 used to raise and write
# nothing, so rainbow-shops -- an accepted, all-checks calibration on one of its two tasks -- was
# held out of the VM stage for good by the other task's golden author. The name is the contract with
# golden.py: this driver retires a task on GOLDEN_FAILED.json and revives it on golden.json.
REVIVED_BY = {"GRADER_FAILED.json": "grader.json", "GOLDEN_FAILED.json": "golden.json"}


def retired(folder, task):
    """Whether this one task is finished with, and no later grader, golden or proof revived it."""
    directory = folder / "tasks" / task
    for marker in RETIRED:
        stamp = directory / marker
        if not stamp.is_file():
            continue
        if (revives := REVIVED_BY.get(marker)) and (directory / revives).is_file():
            continue
        newer = folder / "runtime" / "grades" / task / "calibration.json"
        if newer.is_file() and newer.stat().st_mtime > stamp.stat().st_mtime and calibrated(load(newer)):
            continue
        if marker == "CALIBRATION_FAILED.json" and not spent(stamp, calibration_attempted, SERVE_CODE):
            # The last round died on a hub that was not serving, so it was never spent on the task.
            # The serving code is what has to change before the task is allowed another run at it.
            continue
        return True
    return False


def spent(receipt, attempted, code):
    """Whether RECEIPT may stand as the one attempt its stage is allowed.

    A receipt that records an attempt stands for good: that is what "one repair per world" means.
    A receipt that records none is an empty run -- a crash before the first call, a provider call
    that timed out, a prompt the provider refused to read -- and an empty run must not be spent
    like a real one. Six worlds are terminal tonight on a review repair that ran zero rounds (three
    on an AttributeError since fixed, three on a 1800 s call timeout), macerich on a world repair
    that made no call at all, matthews on a third and final calibration attempt whose golden replay
    hit a hub that was not serving.

    So an empty run is forgiven exactly once, and the forgiveness is tied to a fix landing: while
    the receipt is older than the code that writes it, the stage gets its attempt back; the receipt
    the retry leaves behind is newer than that code, so a second empty run is final until someone
    changes the code again. That is the mtime rule the whole table already runs on, pointed at a
    receipt instead of a marker -- no counter, no state in the driver, and no way to loop.
    """
    if not receipt.is_file():
        return False
    return attempted(load(receipt)) or fresh(receipt, code)


# Faults that say the environment, not the artifact, ended an attempt: the hub was not up, or the
# connection to it died. Neither is an answer about the task, and both were caused by defects fixed
# on 2026-09-09 (two servers sharing one build directory, a driver racing an orphan calibration).
ENVIRONMENT_FAULTS = (
    "Connection refused",
    "urlopen error",
    "Connection reset",
    "Max retries exceeded",
    "RemoteDisconnected",
    "Remote end closed connection",
)


def environment_fault(text):
    return any(sign in str(text) for sign in ENVIRONMENT_FAULTS)


def calibration_attempted(report):
    """Whether a CALIBRATION_FAILED.json records a verdict, i.e. its last round reached the grader.

    The earlier rounds are repair rounds and are meant to fail; the last one is the one that retires
    the task. matthews' third and final attempt was "golden replay: <urlopen error [Errno 111]
    Connection refused>" -- the hub was not serving, so the round was never spent on the task.
    """
    attempts = report.get("attempts") or []
    return bool(attempts) and not environment_fault(attempts[-1])


def review_repair_attempted(report):
    """Whether world/REVIEW-REPAIR.json records a repair the reviewers actually read."""
    return bool(report.get("rounds"))


def world_repair_attempted(report):
    """Whether world/REPAIR.json records a repair that reached an author.

    rounds is not the test: macerich's says one round and its receipts are empty, because the only
    prompt it built was 3,859,614 characters against a 1,048,576 ceiling and was never sent.
    """
    receipts = report.get("receipts") or {}
    return bool(receipts.get("world")) or any((receipts.get("apps") or {}).values())


def tasks(folder):
    """Task ids still in play, in a fixed order.

    A task that cannot be graded is finished, but its company is not: worlds average close to two
    tasks and three companies have already been retired whole because one of a pair failed
    calibration while the other was fine. Both reference pipelines flag the bad artifact and keep
    the rest; only a company with nothing left is terminal.
    """
    return [t for t in all_tasks(folder) if not retired(folder, t)]


def all_tasks(folder):
    """Every task id including the retired ones, in a fixed order."""
    return sorted(p.name for p in (folder / "tasks").glob("*") if (p / "workflow.json").is_file())


def run(cmd, log, timeout):
    """Run one command with its output appended to LOG; return its exit code or "timeout".

    A step that outran its budget always reports ``"timeout"``, never the -2 the interrupt leaves
    behind. That keeps the two ways a command can die apart: this driver stopped it, or something
    outside did. A negative code reaching a caller therefore means an outside signal, which says
    nothing about the work and must not be counted against it.
    """
    with open(log, "a") as stream:
        stream.write(f"\n$ {' '.join(map(str, cmd))}  [{now()}]\n")
        stream.flush()
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Interrupt first: the CLI's KeyboardInterrupt handling stops its own model calls,
            # which run in their own sessions and would otherwise outlive it and duplicate work.
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=90)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return "timeout"


def cli(command, timeout):
    """A step that runs one CLI subcommand on the company folder."""

    def step(ctx, folder, log):
        return run(CLI + [command, str(folder)], log, timeout)

    step.command = command  # read by the freshness audit in test_batching, which derives the
    return step  # deciding module from the CLI dispatch rather than trusting the list


# Steps take (ctx, folder, log) and return 0, a non-zero exit code, or a reason string. Gates
# (steps without a marker) read artifacts and return 0 or a reason; they run a command only to
# recover a failed gate, once, and never while inspecting (ctx None).


def export(ctx, folder, log):
    return run(CLI + ["export-company", ctx["run"], folder.name], log, 300)


def strikes(folder):
    """What world/SEED_FAILED.json really counts against this world.

    Two kinds of marker count nothing, and 22 of the 37 on disk are one of them.

    A marker whose rc is negative records a kill from outside this driver. The step stopped writing
    those, but 13 written before it already count 15 strikes, and two of them hold littler-mendelson
    and nucor at 2 of 3 on nothing but signals.

    A marker older than the world's own SEED.json was overtaken by a seeding that worked (9 on disk).
    Only a seed step that runs unlinks it, and a company whose world exists never runs one, so the
    strike sat there for good -- three of those and a company with a finished world is terminal.
    """
    world = folder / "world"
    marker, seed_json = world / "SEED_FAILED.json", world / "SEED.json"
    record = load(marker)
    if isinstance(record.get("rc"), int) and record["rc"] < 0:
        return 0
    if seed_json.is_file() and marker.is_file() and seed_json.stat().st_mtime > marker.stat().st_mtime:
        return 0
    return record.get("count", 0)


def seed_not_failed(ctx, folder, log):
    """The cap counts real failures of this world only; a marker counting none is dropped on sight."""
    failures = strikes(folder)
    marker = folder / "world" / "SEED_FAILED.json"
    if not failures and ctx is not None and marker.is_file():
        marker.unlink(missing_ok=True)  # never its world's verdict: an outside signal, or overtaken
        note(log, f"dropped {marker.name}: it counted no failure of this world")
    return "seed_failed_repeatedly" if failures >= SEED_FAILURE_CAP else 0


def brief_problems(folder):
    """Why the agreement stage will refuse this company for its own briefs, and what it could not read.

    Two agreement conditions are decided by text the rewrite stage leaves final, before a single
    record is authored: ``readable`` is the brief category of REPORT-readability (the one category
    the report marks blocking, because material and message prose has no repair path), and
    ``evergreen`` fails on relative time in a task's brief, since a world is a snapshot at its
    reference date and must read the same on any day it is run. Neither has a stage between it and
    AGREEMENT.json that repairs it. ``evergreen``'s other half is the reference date, which the seed
    writes; that one cannot move, and nor can anything the mechanical check judges.

    The verdicts come from the same code those gates use rather than from a restatement of their
    rules here, so a pre-seed answer cannot drift from the one that decides delivery. Measured
    2026-09-10: run with only the artifacts that exist before a seed -- no materials, no states,
    proper names from company.json alone -- the brief measurement agreed with the post-seed report
    on **60 of 60** seeded worlds, so nothing is lost by taking it early.

    A measurement that could not be taken is not a verdict (an unreadable assignment, a brief that
    is not a string): it is returned as the second value and reported, never counted as a failure.
    """
    from company_envs.workflows import RELATIVE_TIME
    from company_envs.world.readability import readability_report

    problems, unmeasured = [], None
    try:
        report = readability_report(folder)
    except (OSError, TypeError, ValueError) as exc:
        report, unmeasured = {}, clip(repr(exc))
    category = (report.get("categories") or {}).get("brief") or {}
    # "unmeasured" is the verdict of an empty category, which says a company has no briefs to read,
    # not that they read badly. Blocking on it would make every folder without a published task
    # terminal, and an empty run must not write a real verdict's receipt.
    if category.get("verdict") not in (None, "plain", "unmeasured"):
        flagged = [i["source"] for i in category.get("items") or [] if i.get("verdict") != "plain"]
        problems.append(f"briefs read {category['verdict']}: {', '.join(flagged[:3]) or 'no item named'}")
    for task in tasks(folder):
        brief = str(load(folder / "tasks" / task / "workflow.json").get("brief", ""))
        if hits := sorted({hit.lower() for hit in RELATIVE_TIME.findall(brief)}):
            problems.append(f"{task}: brief uses relative time ({', '.join(hits[:4])})")
    return problems, unmeasured


def briefs_ready(ctx, folder, log):
    """Hold a company out of the ~33-hour seed when its own briefs are what AGREEMENT will refuse.

    The expensive stage used to run first: every check of a company was computed after its world
    existed, and the two conditions that need no world at all were computed last of all, in the
    agreement stage, after the seed, the mechanical check, the render, the visual judge, the
    calibrations and the VM fleet. Measured over the 99 companies on 2026-09-10, two of them were
    already refused by their own briefs and had spent 92 of the cohort's 2,927 recorded core-hours
    between them: clearscale says "describe today" (601 calls, 53.7 hours, and still no world) and
    childrens-aid's briefs read ``dense`` (604 calls, 38.2 hours, seeded).

    A seeded world is never blocked here, and that is the point rather than an exception. Before the
    seed the repair is ``rewrite-briefs --again``, two model calls on an 8 KB payload; after it, the
    same repair re-dates REWRITE.json, which re-dates ASSIGN.json, which re-seeds the world -- so a
    gate that fired late would be asking for 33 hours to fix a sentence, and the standing rule is
    never to block on a defect the pipeline cannot repair. The later gates still refuse the company
    exactly as they do today; all this decides is whether a seed gets paid for first.

    The gate keeps no marker on purpose. Seven defects tonight were a fix behind a fresh marker, and
    the surest way not to add an eighth is a verdict that is recomputed from the briefs every loop:
    it cannot be stale, it runs for all 99 existing companies on the next pass, and a brief that is
    reworded un-blocks the company with no marker to delete.
    """
    if (folder / "world" / "SEED.json").is_file():
        return 0  # the seed is already paid for; checks/review/readability/agreement decide from here
    problems, unmeasured = brief_problems(folder)
    if ctx is not None and unmeasured:
        note(log, f"briefs could not be measured before seeding: {unmeasured}")
    if not problems:
        return 0
    if ctx is not None:
        note(log, "briefs_not_plain (no seed paid for): " + "; ".join(problems[:6]))
    return "briefs_not_plain"


# The exception lines failure_reason() reads out of a company log. Exhausted is here for
# CallBudgetExhausted, the seed's own cumulative call budget: it is raised pre-dispatch, the way
# PromptTooLarge is, and was the one pre-dispatch refusal this pattern did not match -- so the 12
# companies already past design.seed_call_budget on 2026-09-10 (9 of them with no world yet, holding
# 14,124 of the 32,471 recorded seed-call attempts) would each have written three SEED_FAILED.json
# saying {at, count, rc} and nothing about the wall they hit.
FAILURE_LINE = re.compile(
    r"^(?:[\w.]*\b)?\w*(?:Error|Exception|Unavailable|Invalid|Interrupt|TooLarge|Expired|Exhausted|Refused)\b.*"
)


def clip(text, limit=400):
    """TEXT on one line, short enough for a receipt, keeping both ends.

    The reason a codex call failed is at the end of a line whose middle is a repr of the whole
    invocation -- thousands of characters of skill paths, with "timed out after 1800 seconds" last.
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit // 2] + " … " + text[-limit // 2 :]


def failure_reason(log, world, since=0):
    """Why a seeding left no world: the last exception its log printed, and the call that failed.

    All 37 SEED_FAILED.json on disk are the bare {at, count, rc}, so nothing on disk says whether a
    world failed on an identity mismatch, a refused prompt or a provider that stopped answering --
    and the step reads the tail of the log for outage() anyway. The failing attempt directory is
    named because that is where its prompt, stderr and receipt are; only attempts from this run
    count, since a world that failed tonight has hundreds of call directories from earlier ones.
    """
    lines = [line.strip() for line in tail(log).splitlines() if FAILURE_LINE.match(line.strip())]
    reason = {"reason": clip(lines[-1])} if lines else {}
    attempts = [*world.glob("calls/*/attempt-*/receipt.json"), *world.glob("calls/*/receipt.json")]
    recent = sorted((p for p in attempts if p.stat().st_mtime >= since), key=lambda p: p.stat().st_mtime)
    for receipt in reversed(recent):
        record = load(receipt)
        if record.get("status") == "error":
            return reason | {"call": str(receipt.parent.relative_to(world.parent)), "job": record.get("job")}
    return reason


def seed(ctx, folder, log):
    """seed-world with verification. A failure that leaves no SEED.json is counted in SEED_FAILED.json."""
    world = folder / "world"
    failures = strikes(folder)
    if seeding_elsewhere(folder):
        return "seeding_elsewhere"  # an orphan of an earlier driver is seeding; wait for its world
    work = str(prepare_work(ctx, folder, log))
    options = ["--timeout", "2400", "--verify", "--host", "127.0.0.1", "--work", work, "--review-rounds", "2"]
    started = time.time()
    rc = run(CLI + ["seed-world", str(folder), *options], log, 5 * HOUR)
    if (world / "SEED.json").is_file():
        (world / "SEED_FAILED.json").unlink(missing_ok=True)
        return 0  # the world exists; its review verdict gates everything after it
    if outage(log):
        return "model_unavailable"  # an account or transport outage says nothing about this world; retry
    if isinstance(rc, int) and rc < 0:
        # Killed by a signal from outside this driver: a restart, an operator, the OOM killer. The
        # seeder was stopped, it did not decide it could not build this world, so it keeps its
        # attempts. Counting these strikes retired fourteen companies that had never really failed.
        return "seed_interrupted"
    write_json(
        world / "SEED_FAILED.json",
        {"count": failures + 1, "rc": rc, "at": now(), **failure_reason(log, world, started)},
    )
    return rc


OUTAGE_SIGNS = ("hit your usage limit", "provider process failed")
# ModelUnavailable is raised for two unrelated things: a provider that is not answering anyone, and
# one codex call that outran its own 1800/3600 s budget. The second is this call's answer.
UNAVAILABLE = "ModelUnavailable: "  # the exception line, not the "raise ModelUnavailable(" frame
CALL_TIMEOUT = re.compile(r"timed out after [\d.]+ seconds")
# The task designer answered and refused: it read the world and would not build on what is left.
DECLINED = "return the required number of distinct eligible outlines"
NO_OUTLINES = "unused outlines remain"  # author-tasks' ValueError when the design is exhausted


def tail(log, size=20_000):
    """The last SIZE bytes of a company log, or "" when there is nothing to read."""
    try:
        with open(log, "rb") as handle:
            handle.seek(max(0, handle.seek(0, 2) - size))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def outage(log):
    """Whether the tail of a company log shows a model provider outage rather than a world problem.

    An outage is the provider not answering anyone: an exhausted account, a dead transport. It says
    nothing about this world, so the step returns without a receipt and the next loop retries.

    A codex call that ran past its own timeout is not that. It reaches the driver as the same
    ModelUnavailable -- models.py wraps subprocess.TimeoutExpired in one -- and the sign used to be
    the class name, so a timed-out call was read as an outage and the step wrote no receipt at all.
    Eleven companies had bought 2,800 seeding call directories that way and had neither SEED.json
    nor SEED_FAILED.json to show for them: national-jewish-health 536 calls, sprouts-farmers-market
    521, the-home-depot 519, down to catalent's 10. A per-call timeout is this call's answer and has
    to be counted as a failure with a reason, which is what the ceiling measurements already said:
    no shorter timeout wins, so a timeout has to be recorded rather than retried for ever.
    """
    text = tail(log)
    if any(sign in text for sign in OUTAGE_SIGNS):
        return True
    raised = [line for line in text.splitlines() if UNAVAILABLE in line]
    return bool(raised) and not all(CALL_TIMEOUT.search(line) for line in raised)


def seeding_elsewhere(folder):
    """A seed-world process for this folder is already running (an orphan of an earlier driver)."""
    return elsewhere(f"seed-world {folder} ")


def calibrating_elsewhere(folder):
    """A hub-serve, render-check, author-golden or calibrate process for this folder is already
    running (an orphan of an earlier driver); a second one would race it on the same files."""
    return elsewhere(f"company_envs (hub-serve|render-check|author-golden|calibrate) {folder}( |$)")


def running_elsewhere(folder, task):
    """A run-company process for this task is already running (an orphan of an earlier driver)."""
    return elsewhere(f"run-company {folder} {task} ")


def elsewhere(pattern):
    probe = subprocess.run(["pgrep", "-f", pattern], capture_output=True, check=False)
    return probe.returncode == 0


def review_accepted(ctx, folder, log):
    """The reviewers' final verdict decides; mechanical problems are the check stage's business.

    A rejected world is not thrown away on one reading: with no world/REVIEW-REPAIR.json yet, one
    `review-repair` sends the blocking findings back to the apps that own the cited records and has
    the reviewers read the repaired world, and the verdict is read again. That receipt is written
    whatever happens, so a world gets one attempt and never loops -- but a receipt that records zero
    rounds is not an attempt, and spent() gives those six worlds their one repair back. Inspection
    (ctx None) reads the verdict and runs nothing.
    """
    outcome = review_verdict(folder)
    receipt = folder / "world/REVIEW-REPAIR.json"
    if outcome == 0 or ctx is None or spent(receipt, review_repair_attempted, REPAIR_CODE):
        return outcome
    run(CLI + ["review-repair", str(folder)], log, 3 * HOUR)
    return review_verdict(folder)


def review_verdict(folder):
    """SEED.json's status folds the mechanical check into review, so a world the reviewers accepted
    but an old rule failed would look rejected forever. Newer seeds record the verdict on its own
    as SEED.json review_verdict; older ones leave it in REVIEW.json; status is the last resort."""
    seed = load(folder / "world/SEED.json")
    if "review_verdict" in seed:
        return 0 if seed["review_verdict"] == "accept" else "review_not_accepted"
    rounds = load(folder / "world/REVIEW.json").get("rounds") or []
    if rounds:
        verdict = (rounds[-1].get("verdict") or {}).get("verdict")
        return 0 if verdict == "accept" else "review_not_accepted"
    return 0 if seed.get("status") == "seeded_reviewed" else "review_not_accepted"


def outline_ids(folder):
    """The business-cycle outlines the company was designed with, in design order."""
    return [o["id"] for o in load(folder / "company.json").get("outlines", []) if o.get("id")]


def unused_outlines(folder):
    """Designed outlines no published task was authored from: what the mine stage may still use."""
    used = {load(folder / "tasks" / t / "workflow.json").get("outline_id") for t in tasks(folder)}
    return [o for o in outline_ids(folder) if o not in used]


def rejected_tasks(folder):
    """Ids the reviewers turned down; author-tasks retains them under tasks/_rejected/."""
    directory = folder / "tasks" / "_rejected"
    return sorted(p.name for p in directory.glob("*") if (p / "workflow.json").is_file())


def unprepared(folder):
    """Tasks that missed a company-wide preparation pass, listed per command that has to see them.

    rewrite-briefs and assign-apps run once per company and then skip on their own marker, so a
    task authored after them carries no plain brief and no app binding. Those two markers are the
    inputs of the apps and seed stages: they are left exactly as they are and only the new ids are
    named, which is what --only is for. A company whose pass never ran is not this stage's work.
    """
    fields = {"rewrite-briefs": ("plain_brief", REWRITE), "assign-apps": ("worker_apps", ASSIGN)}
    todo = {command: [] for command in fields}
    for task in tasks(folder):
        workflow = load(folder / "tasks" / task / "workflow.json")
        for command, (field, marker) in fields.items():
            if (folder / marker).is_file() and field not in workflow:
                todo[command].append(task)
    return todo


def preparation_gaps(folder):
    """What would leave a mined task short of the ones the world was seeded with: no plain brief
    (so the readability gate and the worker's assignment have nothing to read), or a worker the
    seeded world's worker_apps.json does not give any apps to."""
    covered = set(load(folder / "world" / "worker_apps.json"))
    gaps = []
    for task in tasks(folder):
        if "plain_brief" not in load(folder / "tasks" / task / "workflow.json"):
            gaps.append(f"{task}: no plain brief")
        if missing := [w for w in roster(folder, task) if w not in covered]:
            gaps.append(f"{task}: worker_apps.json does not cover {missing}")
    return gaps


def mined_ok(report):
    """A receipt that records a finished pass; a missing or half-written one does not."""
    return "mined_at" in report


def mine_outcome(receipt, want):
    """The shared word for what this mine run learned, from company_envs.receipt.

    The marker recorded ``requested: 2, accepted: []`` for three different things: a designer that
    looked and refused, a payload over the provider's ceiling, and a world already at target with
    nothing asked for. The first two are verdicts and the third is a pass, and a reader could not
    tell them apart without the note -- which two of the three did not write. ``outcome`` is one
    word every stage's receipt now carries; ``mined_at`` still decides whether the step re-runs, so
    this changes what a reader learns and not what the driver does.
    """
    from company_envs.receipt import Receipt

    if receipt["accepted"] or not want:
        return Receipt.passed()
    if note_ := receipt.get("note"):
        return Receipt.refused(note_)
    if receipt["rejected"]:
        return Receipt.refused(f"the review rejected {len(receipt['rejected'])} designs")
    return Receipt.refused("author-tasks accepted nothing and recorded no reason")


def oversized_refusal(folder, since):
    """Why an author-tasks call in this run refused an over-ceiling payload, or None.

    ``task_author.oversized_mining`` returns instead of raising, and that is the right shape: a prompt
    past the provider's ceiling is terminal, and raising had the step rebuild the same payload every
    loop -- 12 call ids across 12 companies reached 234 attempts none of which could succeed. The
    command therefore exits 0 having accepted nothing, which the mine step reads as "no failure" and
    banks in its receipt.

    That is also exactly what a designer which simply accepted nothing looks like, and MINE.json
    recorded ``requested: 2, accepted: []`` for both, while the two other ways to mine nothing each
    write a note. The reason is read from the refusal's own receipt beside the rejected drafts rather
    than from a marker in the log: the driver only ever reads the last 20 KB of a company log and
    author-tasks prints its whole report into it, so a log marker is the more fragile of the two.
    Scoped by mtime to this run, the way a failed seed's call receipts are.
    """
    written = [p for p in folder.glob("tasks/_rejected/oversized-*.json") if p.stat().st_mtime >= since]
    if not written:
        return None
    return clip(load(max(written, key=lambda p: p.stat().st_mtime)).get("reason") or "refused: over ceiling")


def mine(ctx, folder, log):
    """Top a reviewed world up to design.tasks_per_company tasks from its unused outlines.

    Companies are designed with three to five business-cycle outlines and publish about two tasks
    each, so most accepted worlds have outlines left over. Authoring against a world that already
    passed every gate costs a design and a review call and cannot fail the world; seeding another
    world costs hundreds of calls and can be rejected outright.

    The new tasks then get the same preparation the first ones had, by id: a plain brief and an app
    binding, and a worker_apps.json synced from the enlarged task contract so their workers hold
    the apps they need. Not one of those commands rewrites REWRITE.json or ASSIGN.json -- making
    REWRITE.json stale would re-run assign-apps, whose newer ASSIGN.json would re-seed the very
    world this stage exists to mine.

    The receipt is the marker. A world with nothing to mine writes it and never runs again; a
    reviewers' rejection of the batch is recorded there too and not retried.
    """
    design = load_toml(ROOT / "config.toml").get("design", {})
    target = int(design.get("tasks_per_company", TASKS_PER_COMPANY))
    have, spare = tasks(folder), unused_outlines(folder)
    want = max(0, min(target, len(have) + len(spare)) - len(have))
    receipt = {
        "mined_at": now(),
        "target": target,
        "requested": want,
        "existing": have,
        "unused_outlines": spare,
        "accepted": [],
        "rejected": [],
    }
    if want:
        # One task per call. The authoring contract is exact -- the batch must hold precisely the
        # requested number of distinct unused outlines -- and asking for three at once fails whole
        # where asking for one at a time banks each task that lands.
        published, refused = set(have), set(rejected_tasks(folder))
        rc, started, oversized = 0, time.time(), None
        for _ in range(want):
            rc = run(CLI + ["author-tasks", str(folder), "--count", "1"], log, 2 * HOUR)
            if rc:
                break
            if oversized := oversized_refusal(folder, started):
                break  # the refusal's own words: nothing this stage can shrink, and no retry changes it
        receipt["accepted"] = sorted(set(tasks(folder)) - published)
        receipt["rejected"] = sorted(set(rejected_tasks(folder)) - refused)
        if oversized:
            # Whatever landed before it, the rest are missing for a reason the receipt has to carry.
            receipt["note"] = f"author-tasks refused an over-ceiling payload: {oversized}"
        if rc and not receipt["accepted"]:
            if outage(log):
                return "model_unavailable"  # says nothing about this world; retry next loop
            if DECLINED in tail(log):
                # The designer looked and refused, usually because the records were seeded for the
                # tasks that were published rather than for every outline. Record it and move on:
                # retrying holds a slot on the companies furthest along, which are the ones with
                # stages left that actually deliver a task.
                receipt["note"] = "author-tasks declined the spare outlines"
                write_json(folder / MINE, {**receipt, **mine_outcome(receipt, want).body})
                return 0
            if NO_OUTLINES not in tail(log):
                return "mine_failed"
            # author-tasks counts the outlines too: it found none, so there is nothing to mine.
            receipt["note"] = "author-tasks found no unused outlines"
    todo = unprepared(folder)
    receipt["prepared"] = todo
    for command, ids in todo.items():
        if ids and run(CLI + [command, str(folder), "--only", *ids], log, 1800):
            return "mine_failed"
    if receipt["accepted"] or any(todo.values()):
        if run(CLI + ["sync-worker-apps", str(folder)], log, 120):
            return "mine_failed"
        if gaps := preparation_gaps(folder):
            note(log, f"mined tasks are not ready to run: {'; '.join(gaps[:6])}")
            return "mine_unprepared"
    write_json(folder / MINE, {**receipt, **mine_outcome(receipt, want).body})
    return 0


def check(ctx, folder, log):
    """world-check; on errors one world-repair (the authors get the findings, bounded rounds, the
    bulk laid again) and a second world-check, whose CHECKS.json is the marker. One repair per
    check run: the marker is then fresh, and the gate after it says check_failed until an input
    of the stage changes."""
    rc = run(CLI + ["world-check", str(folder)], log, 600)
    if rc == 0:
        return 0
    run(CLI + ["world-repair", str(folder)], log, 2 * HOUR)
    return run(CLI + ["world-check", str(folder)], log, 600)


def checks_ok(ctx, folder, log):
    """A failing check earns one repair before the world is given up on.

    The repair lives inside the check step, which only runs when CHECKS.json is stale. Worlds
    whose check failed before the repair existed sat on a fresh failing marker, so the step never
    ran again and the repair never got its attempt. Trying it from the gate, the way a rejected
    review is repaired, reaches them. world/REPAIR.json is the receipt that keeps it to one try --
    unless it records no try at all, which is macerich: one round, no receipts, and the only prompt
    it built refused for being 3.7x the provider's ceiling.
    """
    if load(folder / "world/CHECKS.json").get("ok"):
        return 0
    if ctx is None or spent(folder / "world/REPAIR.json", world_repair_attempted, REPAIR_CODE):
        return "check_failed"
    run(CLI + ["world-repair", str(folder)], log, 2 * HOUR)
    return 0 if load(folder / "world/CHECKS.json").get("ok") else "check_failed"


def not_rejected(ctx, folder, log):
    """Terminal only when every task is retired; a company outlives one ungradeable task.

    The three markers say different things and keep separate outcomes: GOLDEN_FAILED means no
    reference trajectory could be written, GRADER_FAILED means no check meeting the contract could
    be written, CALIBRATION_FAILED means checks were written and did not separate a finished task
    from an untouched one. Conflating them hides which wall was hit.
    """
    remaining = tasks(folder)
    if remaining:
        return 0
    everything = all_tasks(folder)
    if not everything:
        return 0
    for marker, outcome in (
        ("GRADER_FAILED.json", "grader_not_authored"),
        ("GOLDEN_FAILED.json", "golden_not_authored"),
    ):
        if all((folder / "tasks" / t / marker).is_file() for t in everything):
            return outcome
    return "calibration_rejected"


def calibrations(folder):
    return [folder / "runtime" / "grades" / t / "calibration.json" for t in tasks(folder)]


def calibrated(report):
    """A state-only proof predates full calibration; the agreement rejects it, so it does not count."""
    return report.get("accepted") is True and report.get("scope") == "all_checks"


def world_states(folder):
    """The seeded app states: the world itself, as every stage after the seed reads it.

    Everything that really changes a world rewrites these -- the seeder, add-bulk, world-repair,
    dedupe-names. Nothing else does, which is what makes them the honest input for a stage whose
    verdict is about the records rather than about the code that judged them.
    """
    return sorted(folder.glob("world/*.state.json"))


def render_inputs(folder):
    """The artifacts a browser render depends on: the seeded states and the recorded app patches.

    The code that decides what a browser is served is an input too -- the proxy rewrites what it
    serves, so a change there changes every rendered page while every state file stays untouched --
    but it belongs to RENDER.json rather than to the stage, so deciding_code() carries it and
    needed() composes the two. Without it a world sits on a stale failing report that the fix has
    already made wrong, and nothing re-renders it.
    """
    return [*world_states(folder), HUB_PATCHES]


def check_inputs(folder):
    """The artifact a mechanical verdict depends on: the finished world.

    The bulk layer is what finishes a world, so it stands for the world here the way it always has.
    The checker that judges it is an input of world/CHECKS.json, not of the stage: the stage's other
    step, sync-worker-apps, is decided by state_seed.py instead, and re-dating its marker for an
    edit to world_check.py ran 60 syncs for nothing. deciding_code() keeps the two apart.
    """
    return [folder / "world/BULK.json"]


def readability_inputs(folder):
    """What the report says it measured; the gate's own rules are deciding_code()'s half.

    It used to be BULK.json and the mine receipt -- neither of which the report reads, and one of
    which six companies do not have, so their marker could never go stale. The report now records its
    own inventory; an older one without it falls back to the files the gate is known to read.
    """
    report = load(folder / "REPORT-readability.json")
    measured = [folder / name for name in (report.get("inputs") or {})]
    return measured or [folder / "world/BULK.json", folder / MINE]


def render_ok(report):
    """Whether a RENDER.json lets a company go on. The rule lives in render_check.admissible.

    It used to be ``ok is True``, and a skipped check read as ok because the skip wrote ``ok: True``
    -- so did a render of zero apps, which is a company nothing was served from reading exactly like
    one whose every app rendered. The two empty renders are now different outcomes: a host that
    cannot render is ``faulted`` and admitted (no stage installs a browser, and the VM stage takes
    the measurement again in a real browser), while a render that measured nothing is refused.

    Asking render_check rather than restating the rule here is the point: the same predicate decides
    the CLI's exit code, so this gate and that exit cannot drift apart.
    """
    from company_envs.world.render_check import admissible

    return admissible(report)


# The content floor's own words, from render_check.content_floor. An app fails it when its first
# view shows fewer than five of the company's seeded values in under 1,500 characters of its own
# text. test_batching pins this phrase to that function, so a rewording breaks loudly here.
RENDER_FLOOR = "of this company's own values in"


def render_floor_only(report):
    """Whether every app this render failed failed on the content floor, and on nothing else.

    The floor is the one render failure a repair can reach: a first view showing none of the
    company's values is a seed that put the records outside the visible window. Measured on
    2026-09-10, the floor failed 39 of 132 passing renders on `/` and 0 of 38 once the real entry
    route was opened; the residual two are calendars whose seeded week is not the reference week,
    and world-repair rewrites records. The other failures in a report -- an image the browser
    cannot draw, a page with nothing to click, a read-only visit that lost seeded data -- belong to
    the app or the proxy, and a repair of the world would be two hours of model calls spent on a
    defect it cannot touch. So the repair is offered only when the whole failure is the floor.
    """
    apps = report.get("apps") or {}
    failed = [result for result in apps.values() if not result.get("ok")]
    return bool(failed) and all(RENDER_FLOOR in (result.get("error") or "") for result in failed)


def render_repair_called(receipt):
    """Whether the one repair a floor failure earns actually reached an author."""
    return bool(receipt.get("called"))


def render_repair(ctx, folder, log):
    """One world-repair for a render that failed only on the content floor, then a fresh render.

    render_failed is not terminal and is retried every loop, so without this the two companies whose
    calendars show an empty current week would pay a build and an eight-page render for ever and
    never advance. The repair rewrites records, which re-dates world/*.state.json and so makes
    RENDER.json stale: the next loop serves the repaired world and renders it again. The receipt
    keeps it to one attempt, the way world/REPAIR.json does for a failed check -- and a repair that
    never reached an author is an empty run, so spent() hands that one back once.
    """
    report = load(folder / RENDER)
    receipt = folder / RENDER_REPAIR
    if ctx is None or not render_floor_only(report):
        return "render_failed"  # RENDER.json names the app and why; retried each loop
    if spent(receipt, render_repair_called, REPAIR_CODE):
        return "render_failed"
    rc = run(CLI + ["world-repair", str(folder)], log, 2 * HOUR)
    failed = {app: result.get("error") for app, result in (report.get("apps") or {}).items() if not result.get("ok")}  # fmt: skip
    write_json(
        receipt,
        {
            "at": now(),
            "rc": rc,
            "called": world_repair_attempted(load(folder / "world/REPAIR.json")),
            "apps": failed,
        },
    )
    return "render_repaired"  # the records changed; the next loop re-serves and re-renders them


def render_step(ctx, folder, log):
    """Serve and render (with the calibrations the same session can reach), then repair a floor miss.

    The repair runs here rather than inside calibrate() so that it sees no hub of ours serving the
    world it is about to rewrite, which is the same order checks_ok repairs a failed check in.
    """
    outcome = calibrate(ctx, folder, log)
    return render_repair(ctx, folder, log) if outcome == "render_failed" else outcome


def calibrate(ctx, folder, log):
    """Serve the seeded world, render-check it when RENDER.json is stale, then author a golden and
    calibrate each task lacking an accepted report. The render and calibrate stages share this step
    so the hub is served once; each part runs only when its own marker is not fresh."""
    if calibrating_elsewhere(folder):
        return "calibrating_elsewhere"  # an orphan of an earlier driver is serving or calibrating this world
    world = needed(folder, "calibrate", calibrations)
    pending = [
        t
        for t, p in zip(tasks(folder), calibrations(folder))
        if not fresh(p, world, accept_for(calibrations))
    ]
    render = folder / RENDER
    rendering = not fresh(render, needed(folder, "render", RENDER), accept_for(RENDER))
    judging = not fresh(folder / VISUAL, needed(folder, "visual", VISUAL), accept_for(VISUAL))
    work = str(prepare_work(ctx, folder, log))  # restores a pruned or never-made work copy from the cache
    with open(log, "a") as stream:
        serve = subprocess.Popen(
            CLI + ["hub-serve", str(folder), "--host", "127.0.0.1", "--work", work],
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    try:
        started, deadline = time.time(), time.monotonic() + 900
        endpoints = folder / "runtime" / "endpoints.json"
        # hub-serve rewrites endpoints.json once every app is seeded and proxied.
        while not endpoints.is_file() or endpoints.stat().st_mtime < started:
            if serve.poll() is not None or time.monotonic() > deadline:
                return "serve_failed"
            time.sleep(2)
        time.sleep(3)
        if rendering:
            rc = run(CLI + ["render-check", str(folder), "--host", "127.0.0.1", "--work", work], log, HOUR)
            if rc or not render_ok(load(render)):
                return "render_failed"  # RENDER.json names the app and why; retried each loop
        if judging:
            # The render check reads text; this one looks. It needs the same live hub, because a
            # worker's start page is built from the served endpoints.
            options = ["--work", work, *(["--task", first] if (first := first_task(folder)) else [])]
            rc = run(CLI + ["visual-judge", str(folder), *options], log, HOUR)
            if rc or not visual_ok(load(folder / VISUAL)):
                return "visual_failed"  # VISUAL.json names the screen and the problems
        for task in pending:
            golden = [] if (folder / "tasks" / task / "golden.json").is_file() else [["author-golden", task]]
            for command in [*golden, ["calibrate", task, "--golden"]]:
                if rc := run(CLI + [command[0], str(folder), *command[1:]], log, HOUR):
                    return rc
        return 0
    finally:
        serve.terminate()
        try:
            serve.wait(30)
        except subprocess.TimeoutExpired:
            serve.kill()


def guest_budget(design):
    """The host's guest budget: vm_guests when set, else vm_slots converted (a slot was one company of
    GUESTS_PER_SLOT guests; the shipped 4 slots are 20 guests), default 20."""
    if "vm_guests" in design:
        return int(design["vm_guests"])
    return int(design.get("vm_slots", 4)) * GUESTS_PER_SLOT


def roster(folder, task):
    """The guests a task boots: its manager and workers, each once."""
    flow = load(folder / "tasks" / task / "workflow.json")
    return [w for w in dict.fromkeys([flow.get("manager_id"), *flow.get("worker_ids", [])]) if w]


def vm_slot(guests, budget, slot_dir=BATCH / "vm-slots"):
    """Exclusive flocks on GUESTS of the BUDGET guest slot files, or None when not enough are free.

    Closing the handles releases the guests. A driver from before guest accounting holding a slot
    file counts as one guest here, which only makes this driver more cautious.
    """
    slot_dir.mkdir(parents=True, exist_ok=True)
    held = []
    for index in range(budget):
        handle = open(slot_dir / f"{index}.lock", "w")  # noqa: SIM115 -- the handle is the lock
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            continue
        held.append(handle)
        if len(held) == guests:
            return held
    for handle in held:
        handle.close()
    return None


def controller_state(folder, task):
    """Where run-company keeps this task's checkpoint: runtime/tasks/<task>/CONTROLLER.json, or the
    single-checkpoint layout's runtime/CONTROLLER.json when that file still names this task."""
    legacy = folder / "runtime" / "CONTROLLER.json"
    if load(legacy).get("task_id") == task:
        return legacy
    return folder / "runtime" / "tasks" / task / "CONTROLLER.json"


def controllers(folder):
    """The vm stage's markers: one checkpoint per task."""
    return [controller_state(folder, t) for t in tasks(folder)]


def controller_done(state):
    """A run-company checkpoint that finished its steps, whatever the episode's verdict was.

    Deliberately not "and the policy passed". A finished run whose teacher never passed is a
    negative verdict about the task, recorded for the agreement to read; re-running it because the
    verdict was no is hours of guest time per loop for a task the stage has already answered, and
    nothing in the artifacts would ever stop it. The near-miss extra rollout of commit 0422a19 lives
    inside one run-company call (it raises in the teacher step, and teacher_env_retries in this same
    checkpoint bounds it to one), so it does not need the stage to be re-run from outside.

    Checked on disk 2026-09-10: two live checkpoints, trex-company's at status failed (so its task
    re-runs on its own, near miss included) and vitas-healthcare's done with a graded score of 0.0,
    which is a verdict and not a budget accident. Nothing on disk is a done checkpoint holding a
    near miss. To redo a VM stage on purpose, use the recipe: rename or delete runtime/tasks/<task>/.
    """
    return state.get("status") == "done"


def vm(ctx, folder, log):
    """Every task on real worker VMs: launch, teacher, episode, grade, teardown, each in the task's
    own runtime (one run-company call per task).

    Companies take turns on a fixed number of host slots, since each boots one guest per worker;
    a company holds one slot for the whole stage and runs its tasks one after another on it (a
    three-task company takes three VM stages of slot time). Only tasks whose checkpoint is not
    done run. A task that fails does not stop the others; the outcome then lists every task's
    exit code ("t1=0 t2=1"), and the next loop resumes the ones short of done.

    Guests are the unit of contention: a task boots one guest per roster member (the teacher's
    fleet, then the workers', then one more fleet per extra policy), so it takes that many of the
    host's guest budget for as long as run-company runs and gives them back after.
    A failed or interrupted run leaves CONTROLLER.json short of done, so the next loop resumes it.
    """
    design = load_toml(ROOT / "config.toml").get("design", {})
    settings = [design.get(k) for k in ("vm_base_image", "vm_browser_dir", "vm_host_ip")]
    if not all(settings):
        return "vm_unconfigured"
    budget = guest_budget(design)
    base_image, browser_dir, host_ip = map(str, settings)
    options = ["--backend", "mypcbench", "--no-dry-run", "--base-image", base_image]
    options += ["--browser-dir", browser_dir, "--host-ip", host_ip]
    options += ["--model-calls", str(int(design.get("vm_model_calls", 60))), "--seconds", "1800"]
    inputs = needed(folder, "vm", controllers)
    outcomes = {}
    for task in tasks(folder):
        if fresh(controller_state(folder, task), inputs, controller_done):
            continue
        if running_elsewhere(folder, task):
            outcomes[task] = "running_elsewhere"  # an orphan of an earlier driver; wait for its verdict
            continue
        guests = max(1, len(roster(folder, task)))  # a task with no roster written still boots a manager
        if guests > budget:
            return "vm_over_budget"  # this roster never fits; raise vm_guests or shrink the task
        held = vm_slot(guests, budget)
        if held is None:
            return "vm_slots_busy"  # try again next loop
        GUESTS[folder.name] = guests
        try:
            outcomes[task] = run(CLI + ["run-company", str(folder), task, *options], log, 3 * HOUR)
        finally:
            GUESTS.pop(folder.name, None)
            for handle in held:
                handle.close()
    if all(rc == 0 for rc in outcomes.values()):
        return 0
    return " ".join(f"{task}={rc}" for task, rc in outcomes.items())


def load_toml(path):
    """Use the same inherited config selection as the stage subprocesses."""
    return load_config(path.parent, missing_ok=True)


def agreement_inputs(folder):
    """Everything the agreement reads; the sheet and the agreement are redone when any of it changes."""
    fixed = ("world/SEED.json", "world/CHECKS.json", "world/REVIEW.json", "REPORT-readability.json")
    globs = (
        "runtime/grades/*/calibration.json",
        "runtime/tasks/*/controller/company/runtime/teacher/*/TEACHER.json",
        "runtime/controller/company/runtime/teacher/*/TEACHER.json",  # the single-checkpoint layout
        "tasks/*/workflow.json",
    )
    files = [folder / p for p in fixed] + controllers(folder)
    return files + [p for g in globs for p in folder.glob(g)]


REWRITE, ASSIGN = "tasks/_plain_brief/REWRITE.json", "tasks/_worker_apps/ASSIGN.json"
MINE = "tasks/_mined/MINE.json"
RENDER = "runtime/RENDER.json"
RENDER_REPAIR = "runtime/RENDER-REPAIR.json"  # the driver's receipt for the one repair a floor miss earns
VISUAL = "runtime/VISUAL.json"


def first_task(folder):
    """The folder's first real task; the worker start pages a visual judge renders belong to one."""
    return next(iter(tasks(folder)), None)


def visual_inputs(folder):
    """A visual verdict is stale when the render it judged is; the judge's rules are deciding_code()'s."""
    return [*render_inputs(folder), folder / RENDER]


def visual_ok(report):
    """Whether a VISUAL.json accepts the world; a missing or unreadable report does not.

    Same three answers as render_ok, and the same reason for asking the module that knows what a
    judgement is: a host that cannot render takes no screenshots, so that fault is forwarded from
    RENDER.json and admitted, while a judgement that had screens and reached no verdict -- or had
    none behind a render that succeeded -- is a world nobody looked at and is refused.
    """
    from company_envs.world.visual_judge import admissible

    return bool(report) and admissible(report)


def recorded(module):
    """Whether a marker says which code decided it, asked of that code. deciding_code()'s other half.

    An mtime answers the same question and answers it wrongly: the dates on disk were assigned in
    bulk by something outside the pipeline (see the comment above PROXY_CODE), so 93 markers read as
    newer than the last change to the module that decides them with nothing able to say whether the
    verdict came from it. A digest recorded in the marker is content, and content survives a
    re-dating, a restore and an archive extraction. It also costs nothing: it fires on exactly the
    commits the mtime list fires on, because a commit that changes a file changes both.

    The module that writes a verdict is the only thing that knows which files decide it, so it
    exposes ``current`` and this only asks -- bulk_ok's shape, generalised. The list in
    deciding_code() stays, because it is what restales the markers already on disk that record
    nothing, and because a module whose DECIDES_THIS drifts from it is caught by test_batching.

    It re-reads and re-hashes the deciding files on every evaluation on purpose, since trusting their
    mtimes is the thing this replaces. Measured: 0.40 ms for all five markers' digests, 0.08 s per
    loop across 99 companies and both of the passes that evaluate them, against a 60 s loop. There is
    nothing here to cache, and a cache keyed on an mtime would put the defect back.
    """

    def accept(content):
        from importlib import import_module

        return import_module(f"company_envs.world.{module}").current(content)

    accept.__name__ = f"{module}_decided_it"
    accept.decides = module  # read by the audit, which checks this module's DECIDES_THIS against the list
    return accept


def every(*checks):
    """One content check out of several: the marker is acceptable when every check accepts it.

    Markers answer two independent questions now -- was this a verdict at all, and did it come from
    today's code -- and the accept slot holds one function. Composing here keeps each check one idea.
    """

    def accept(content):
        return all(check(content) for check in checks)

    accept.__name__ = "_and_".join(getattr(c, "__name__", "check") for c in checks)
    accept.decides = next((c.decides for c in checks if hasattr(c, "decides")), None)
    return accept


def bulk_ok(marker):
    """Whether a BULK.json was laid by today's expander, so the step need not run again.

    The step was gated on the marker existing. Nothing else about it could ever make it stale, so a
    world whose bulk was laid by a broken expander carried that layer to delivery no matter how
    often the expander was fixed -- and 42 worlds on disk were laid by code that kept 3 of 273,453
    declared Slack messages and 3 of 124,265 calendar events. The marker now carries the version of
    the code that wrote it and a change to that version is staleness like any other input's.
    """
    from company_envs.world.bulk_layer import current

    return current(marker)


def vm_inputs(folder):
    """The VM stage reads the calibrations, and nothing else.

    RENDER.json and VISUAL.json used to be here too, and that was how the one stage the table says
    must list no code came to list six source files: through render_inputs, every edit of
    render_check.py, hub_app.py, hub_identity.py, placeholder_images.py or visual_judge.py re-dated
    a render, the fresh render re-dated the visual verdict, and both re-ran every episode under them.
    Those five files took 39 of the 295 commits in the week to 2026-09-10; at three checkpoints that
    is 120 re-run episodes a week, and at the 50-company target it is over 2,000 against a fleet of
    eight slots.
    Nothing is lost by dropping them. A re-render recomputes an opinion and moves no record, and
    what a VM episode proves is about records: everything that really changes a world rewrites
    world/*.state.json, which is INPUTS["calibrate"], so a changed world re-dates the calibrations
    and the checkpoints under them anyway. And a render or a visual verdict that now fails stops the
    company at its own step, two steps before vm, so no episode can run on a world that stopped
    rendering.
    """
    return calibrations(folder)


# What each stage reads: paths relative to the company folder, or a function of the folder.
INPUTS = {
    "export": [],
    "rewrite": ["company.json"],
    "apps": [REWRITE],
    "seed": [ASSIGN],
    # The receipt is the whole record of what the outlines were worth: an outline is mined once,
    # and re-seeding a world does not make its published tasks worth authoring again.
    "mine": [],
    "check": check_inputs,
    # The readability gate reads the task briefs, so mined ones make it stale. It is a stage of
    # its own for that: putting the receipt in INPUTS["check"] would re-date CHECKS.json, and the
    # cascade after CHECKS.json used to recalibrate and re-run the VM episodes of tasks that had
    # not changed -- which is also why INPUTS["calibrate"] is the world and no longer its verdict.
    "readability": readability_inputs,
    "render": render_inputs,
    "visual": visual_inputs,
    # The world the grader proved its checks against, not the report that judged it.
    #
    # This was world/CHECKS.json, and with the checker now an input of the check stage that would
    # be a cascade: any edit to world_check.py re-dates 60 CHECKS.json, every calibration under
    # them, and through vm_inputs every VM episode under those -- hours of model calls and guest
    # time spent re-proving tasks whose records never moved. Keeping the cascade and dropping the
    # checker from INPUTS["check"] is worse: the 2026-09-09 measurement was that a world could sit
    # on a stale `ok` from a checker that would now reject it and be sent to a VM on it.
    #
    # Both are answered by asking the right question. A calibration is a proof about records, so
    # what invalidates it is the records changing -- and everything that really changes a world
    # rewrites world/*.state.json, add-bulk and world-repair included. A re-check that only
    # recomputes an opinion moves no state file and invalidates nothing; a world the checker now
    # rejects never reaches a VM anyway, because checks_ok stops the company two steps earlier.
    "calibrate": world_states,
    "vm": vm_inputs,
    "sheet": agreement_inputs,
    "agreement": agreement_inputs,
}


def deciding_code():
    """What decides each marker's own verdict, on top of the artifacts its stage reads.

    Keyed by the marker as STEPS names it, because two steps of one stage can be decided by
    different modules: the check stage writes world/CHECKS.json out of the checker and
    world/worker_apps.json out of the seeder, and one list shared between them ran 60 worker-app
    syncs for every edit of the checker. needed() composes the two halves.

    Built on each call, not once at import, so that replacing one of the tuples above replaces what
    the table says -- which is how the tests pin repo files whose mtime is a checkout time.
    """
    return {
        "world/NAMES.json": NAMES_CODE,
        "world/worker_apps.json": SYNC_CODE,
        "world/CHECKS.json": CHECK_CODE,
        "REPORT-readability.json": READABILITY_CODE,
        RENDER: (*PROXY_CODE, *RENDER_CODE),
        VISUAL: (*PROXY_CODE, *RENDER_CODE, *VISUAL_CODE),
        "REVIEW-SHEET.md": SHEET_CODE,
        "AGREEMENT.json": AGREEMENT_CODE,
    }


# The markers no source file may re-date, and the measured reason for each. A marker belongs in one
# of these two tables or in deciding_code(); test_batching fails on a marker in none of them, which is
# how a step added later cannot quietly lose its staleness again.
NO_CODE_INPUT = {
    "company.json": "the head of the chain: company.json re-dates REWRITE, REWRITE re-dates ASSIGN"
    " and ASSIGN re-dates world/SEED.json, so one 2 s re-export would re-seed the cohort at ~33"
    " hours a world, ~2,000 core-hours. Checked 2026-09-10: 0 of the 60 seeded worlds hold an"
    " upstream marker newer than their SEED.json, and nothing above the seed may put one there.",
    REWRITE: "above the seed; see company.json. The briefs are also read straight from disk by"
    " briefs_ready every loop, so a changed brief rule reaches every company with no marker.",
    ASSIGN: "above the seed; see company.json.",
    "world/SEED.json": "~33 hours of model calls a world. The recipe is to rename world/.",
    "world/BULK.json": "bulk_layer.BULK_VERSION already carries this, and carries it better: laying"
    " the layer again is 243 s a world (p90 364) of model calls and rewrites world/*.state.json,"
    " which re-dates every calibration and episode under it. bulk_layer.py took 12 of the 295"
    " commits in the week to 2026-09-10 against one deliberate version bump, so an mtime input"
    " would have re-laid 42 worlds twelve times for the one time it was meant to. A semantic"
    " version is a different thing from a changed file, and this is the marker that proves it.",
    MINE: "a model stage, and the receipt is the whole record of what the outlines were worth.",
    calibrations: "a proof about records; INPUTS['calibrate'] is world/*.state.json for that reason.",
    controllers: "hours of guest time an episode; see vm_inputs.",
}
# Modules that write a marker without deciding it, so the audit does not demand them as inputs.
WRITES_NOT_DECIDES = {
    # Four modules write world/CHECKS.json because each runs the same checker inline -- the seeder
    # over the world it is about to write, add-bulk and world-repair over the world they rewrote,
    # the controller before an episode. What decides the verdict is the checker, which is listed.
    # world_repair.py reaches the worlds that need it by a narrower path than re-dating all 60
    # CHECKS.json: spent(world/REPAIR.json, REPAIR_CODE) gives a second repair only to a world whose
    # check failed, at 9 commits in the week against 18 core-hours of re-checking for each of them.
    "world/CHECKS.json": (
        WORLD_SRC / "state_seed.py",
        WORLD_SRC / "bulk_layer.py",
        WORLD_SRC / "world_repair.py",
        WORLD_SRC / "controller.py",
    ),
    # world-repair rewrites a world and records the new seed status in the same file.
    "world/SEED.json": (WORLD_SRC / "world_repair.py",),
    "world/BULK.json": (WORLD_SRC / "world_repair.py",),
}
# (stage, marker the command writes, content check for the marker, step). Stages run in this order.
STEPS = (
    ("export", "company.json", None, export),
    ("rewrite", REWRITE, None, cli("rewrite-briefs", 1800)),
    ("apps", ASSIGN, None, cli("assign-apps", 1800)),
    ("seed", None, None, seed_not_failed),
    # The cheap check before the expensive authoring: the brief conditions AGREEMENT refuses a
    # company for need no world, so they are decided here rather than after the seed pays for one.
    ("seed", None, None, briefs_ready),
    ("seed", "world/SEED.json", None, seed),
    ("seed", None, None, review_accepted),
    ("seed", "world/NAMES.json", recorded("names"), cli("dedupe-names", 600)),
    ("seed", "world/BULK.json", bulk_ok, cli("add-bulk", HOUR)),
    ("check", "world/worker_apps.json", None, cli("sync-worker-apps", 120)),
    ("readability", "REPORT-readability.json", recorded("readability"), cli("readability", 600)),
    # world-check, one world-repair on errors, world-check. The marker says which checker decided it;
    # whether that verdict was `ok` is checks_ok's question, two rows down, because the answer to a
    # failed check is a repair and a terminal outcome, not another check.
    ("check", "world/CHECKS.json", recorded("world_check"), check),
    ("check", None, None, checks_ok),
    ("calibrate", None, None, not_rejected),
    # Two questions of each: was this a verdict (render_check.admissible, which admits a host with no
    # browser and refuses a render that measured nothing), and did it come from today's serving stack.
    # The second is why hub_app.py's storage-quota fix can reach a RENDER.json at all.
    ("render", RENDER, every(render_ok, recorded("render_check")), render_step),
    ("visual", VISUAL, every(visual_ok, recorded("visual_judge")), calibrate),
    ("calibrate", calibrations, calibrated, calibrate),
    ("vm", controllers, controller_done, vm),
    ("sheet", "REVIEW-SHEET.md", recorded("review_sheet"), cli("review-sheet", 300)),
    ("agreement", "AGREEMENT.json", recorded("agreement"), cli("agreement", 120)),
    # Mining comes last on purpose. A world's own tasks are worth more than extra ones, and mining
    # ahead of the finishing stages made eleven accepted worlds wait on a task design before they
    # could render, calibrate or reach a VM. A task mined here is picked up by the next loop, which
    # finds its calibration missing and carries it through like any other.
    ("mine", MINE, mined_ok, mine),
)
# The table above is the whole pipeline: it builds a company, proves one task is completable on
# real machines, and stops. Collecting demonstrations is not a stage here and must not become one.
# Decided 2026-09-09. The feasibility proof is the one hint run the pipeline pays for, because
# vm_verified is defined on it. A second hint run exists to harvest trajectories, which is a use of
# a delivered company rather than a check on one: it belongs to company_envs.world.benchmark, which
# runs configured policies against delivered companies on a snapshot copy and already splits train
# from test by family. Keeping it out means a company's verdict never depends on a training harness,
# and the harness can be changed and re-run without making any world's markers stale.
STAGES = tuple(dict.fromkeys(stage for stage, *_ in STEPS))


def paths(folder, spec):
    """Resolve an INPUTS or marker spec to a list of paths."""
    if callable(spec):
        return spec(folder)
    return [folder / s for s in ([spec] if isinstance(spec, str) else spec)]


def accept_for(marker):
    """MARKER's content check, read from STEPS rather than restated.

    This step decides for itself whether each of its three parts has work to do, and it used to name
    the predicates directly -- which is one statement of the rule in the table and another here, the
    shape this whole table exists to stop. When RENDER.json gained a recorded freshness value the two
    disagreed at once: position() found the marker stale on its digest and ran this step, this step
    asked only render_ok, found the verdict fine, served a hub and rendered nothing, and the next loop
    did it again. Twenty companies, every loop, for ever. Asking the table removes the second copy.
    """
    return next(a for _, m, a, _ in STEPS if m is marker or m == marker)


def needed(folder, stage, marker):
    """Every input of MARKER: the artifacts its stage reads, and the code that decides this verdict.

    The two halves are kept apart because they are scoped differently -- the artifacts belong to the
    stage, the code to the one marker -- and because the second half is the half that keeps being
    forgotten. A marker in neither deciding_code() nor NO_CODE_INPUT fails test_batching.
    """
    return [*paths(folder, INPUTS[stage]), *deciding_code().get(marker, ())]


def marker_content(path):
    """A marker's content for an accept function: its JSON, or {"text": ...} when it is not JSON.

    REVIEW-SHEET.md is the one marker that is not JSON, and a recorded freshness value has to live
    inside the marker rather than beside it -- a sidecar would be driver state, and this driver keeps
    none. So the sheet carries its digest in a trailing HTML comment and its accept function is handed
    the text to find it in.
    """
    if path.suffix == ".json":
        return load(path)
    try:
        return {"text": path.read_text()}
    except OSError:
        return {}


def fresh(marker, inputs, accept=None):
    """MARKER exists, is newer than every input that exists, and its content is acceptable.

    Newer is a strict `>`, so a tie reads fresh, and that is load-bearing: the dates on disk were
    assigned in bulk by something outside the pipeline, 105 of the 836 artifact comparisons are exact
    nanosecond ties, and reading a tie as stale re-seeds 36 worlds at ~33 hours each. The comment
    above PROXY_CODE has the measurement; test_batching pins the comparison.
    """
    if not marker.is_file():
        return False
    age = marker.stat().st_mtime
    if any(p.is_file() and p.stat().st_mtime > age for p in inputs):
        return False
    return accept is None or accept(marker_content(marker))


def position(folder, ctx=None, log=None):
    """Walk the steps in order, running each unmet one (or, with ctx None, only inspecting).

    Returns (stage, outcome) where the company stopped; ("done", 0) means every marker is fresh.
    Inspection evaluates gates (they only read artifacts) and reports "pending" for a stale marker.
    """
    for stage, marker, accept, step in STEPS:
        inputs = needed(folder, stage, marker)
        if marker is not None and all(fresh(p, inputs, accept) for p in paths(folder, marker)):
            continue
        outcome = "pending" if ctx is None and marker is not None else step(ctx, folder, log)
        if outcome != 0:
            return stage, outcome
    return "done", 0


def status_view(scope, companies_dir, last):
    """STATUS.json content, read from the artifacts now; LAST holds this process's latest outcomes."""
    view = {"written_at": now(), "pid": os.getpid(), "companies": {}}
    for run_id, company in scope:
        folder = companies_dir / company
        stage, outcome = position(folder)
        row = {"run": run_id, "stage": stage, "outcome": outcome, "steps": {}}
        for name in STAGES:
            markers = [
                (p, a, needed(folder, s, m))
                for s, m, a, _ in STEPS
                if s == name and m is not None
                for p in paths(folder, m)
            ]
            times = [p.stat().st_mtime for p, _, _ in markers if p.is_file()]
            at = datetime.fromtimestamp(max(times), UTC).isoformat(timespec="seconds") if times else None
            ok = bool(markers) and all(fresh(p, inputs, a) for p, a, inputs in markers)
            row["steps"][name] = {"ok": ok, "at": at}
        if company in last:
            row["last"] = last[company]
        if load(folder / "AGREEMENT.json").get("done"):
            row["completed_at"] = load(folder / "AGREEMENT.json").get("checked_at")
        view["companies"][company] = row
    return view


def alive(pid):
    """Whether PID names a running process (one we may not signal counts as running)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_pid(path):
    try:
        return int(path.read_text() or 0)
    except (OSError, ValueError):
        return 0


def claim(company, locks=BATCH / "locks"):
    """Take the company's lock file unless a live process holds it; a dead owner's lock is taken over."""
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / company
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = load_pid(path)
            if pid == os.getpid() or alive(pid):
                return pid == os.getpid()
            path.unlink(missing_ok=True)
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    return False


def progress(folder):
    """How far along a company is, highest first. Slots go to the nearest to delivered.

    There are far more companies than parallel slots, and taking them in name order lets ninety
    seeding attempts -- most of which the reviewers will reject -- crowd out the dozen worlds that
    already passed review and only need the stages that actually produce a delivered task. Reads
    marker files only; it runs for every company on every loop.
    """
    if (folder / "AGREEMENT.json").is_file():
        return 6
    if any(folder.glob("runtime/tasks/*/CONTROLLER.json")):
        return 5
    if any(folder.glob("runtime/grades/*/calibration.json")):
        return 4
    if (folder / "world/SEED.json").is_file() and review_verdict(folder) == 0:
        return 3  # review_verdict returns 0 for accepted, the way the seed gate reads it
    if (folder / "world/SEED.json").is_file():
        return 2
    if (folder / "company.json").is_file():
        return 1
    return 0


def release(companies, locks=BATCH / "locks"):
    """Remove the lock files this process owns."""
    for company in companies:
        if load_pid(locks / company) == os.getpid():
            (locks / company).unlink(missing_ok=True)


def prepare_work(ctx, folder, log):
    """Per-company app build directory, hardlinked from the shared warm cache.

    Concurrent companies must not share one built app directory: HubProcess writes .mock-states
    inside it and removes it on stop. Hardlink copies cost no disk and keep builds isolated. The
    warm cache gets node_modules installed once per app under a lock.
    """
    work, cache = ctx["dir"] / "work" / folder.name, ctx["work"]
    work.mkdir(parents=True, exist_ok=True)
    for app in load(folder / "apps.json").get("apps", []):
        app_id = app["app_id"]
        source, target = cache / app_id, work / app_id
        if target.exists():
            continue
        if not (source / "dist" / "index.html").is_file():
            state = str(ROOT / "experiments/hub-smoke/states" / f"{app_id}.json")
            run(CLI + ["hub-smoke", app_id, "--state", state, "--work", str(cache)], log, 1200)
        with open(cache / f".{app_id}.install.lock", "w") as handle, open(log, "a") as stream:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if not (source / "node_modules" / "vite" / "bin" / "vite.js").is_file():
                install = ["npm", "ci"] if (source / "package-lock.json").is_file() else ["npm", "install"]
                install += ["--ignore-scripts", "--no-audit", "--no-fund"]
                subprocess.run(
                    install, cwd=source, stdout=stream, stderr=subprocess.STDOUT, timeout=900, check=False
                )
            fcntl.flock(handle, fcntl.LOCK_UN)
        subprocess.run(["cp", "-al", str(source), str(target)], check=True)
        for stale in (".mock-states", ".mock-files"):
            subprocess.run(["rm", "-rf", str(target / stale)], check=False)
    return work


def note(log, message):
    with open(log, "a") as stream:
        stream.write(f"{message}  [{now()}]\n")


def remove(path):
    subprocess.run(["rm", "-rf", str(path)], check=False)


def prune_work(ctx, folder, log):
    """Remove the company's hardlinked app build copy; nothing will run on it again."""
    work = ctx["dir"] / "work" / folder.name
    if not work.exists():
        return False
    remove(work)
    note(log, f"pruned work copy {work}")
    return True


def evidence(name):
    return name in EVIDENCE_FILES or (name.startswith("CONTROLLER") and name.endswith(".json"))


def prune_archives(folder, log):
    """Reduce each set-aside controller runtime (runtime/controller.<tag>/) to its evidence.

    The evidence files move under <archive>/EVIDENCE/ at their relative paths; everything else in
    the archive (VM images, npm cache, snapshots of the world) is removed. An archive holding only
    EVIDENCE/ is already pruned and left alone.
    """
    pruned = []
    for archive in sorted(folder.glob("runtime/controller.*")):
        if not archive.is_dir() or archive.is_symlink():
            continue
        entries = [p.name for p in archive.iterdir()]
        if entries in ([], ["EVIDENCE"]):
            continue
        kept = archive / "EVIDENCE"
        for path in [p for p in archive.rglob("*") if p.is_file() and not p.is_symlink()]:
            relative = path.relative_to(archive)
            if relative.parts[0] == "EVIDENCE" or not evidence(path.name):
                continue
            target = kept / relative
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
        for entry in archive.iterdir():
            if entry.name != "EVIDENCE":
                remove(entry)
        kept.mkdir(exist_ok=True)
        pruned.append(archive.name)
        count = sum(1 for p in kept.rglob("*") if p.is_file())
        note(log, f"pruned {archive} to EVIDENCE/ ({count} files kept)")
    return pruned


def prune_worlds(folder, log, days=ARCHIVE_DAYS, clock=time.time):
    """Remove world archives left by re-seeding once DAYS have passed since they were set aside.

    Age is the archive's ctime: renaming world/ to world.rejected-<date>/ sets it, while the mtime
    of a directory is that of its last entry change and would date the archive to its seeding.
    """
    removed = []
    cutoff = clock() - days * 86400
    for archive in sorted([*folder.glob("world.old-*"), *folder.glob("world.rejected-*")]):
        if archive.is_dir() and not archive.is_symlink() and archive.stat().st_ctime < cutoff:
            remove(archive)
            removed.append(archive.name)
            note(log, f"removed world archive {archive} (set aside more than {days} days ago)")
    return removed


def housekeep(ctx, folder, log, stage, outcome, keep_archives=False):
    """Idempotent cleanup for an owned company nothing is running on right now."""
    if stage == "done" or outcome in TERMINAL:
        prune_work(ctx, folder, log)
    prune_archives(folder, log)
    if not keep_archives:
        prune_worlds(folder, log)


def usage_limit_hits(directory, since):
    """Usage-limit messages in the tails of DIRECTORY's company logs written since SINCE."""
    hits = 0
    for log in directory.glob("*.log"):
        try:
            if log.stat().st_mtime < since:
                continue
            with open(log, "rb") as handle:
                handle.seek(max(0, handle.seek(0, 2) - 200_000))
                hits += handle.read().count(b"hit your usage limit")
        except OSError:
            continue
    return hits


def own_seeds():
    """seed-world processes that are children of this driver."""
    probe = subprocess.run(
        ["pgrep", "-P", str(os.getpid()), "-af"], capture_output=True, text=True, check=False
    )
    return sum("seed-world" in line for line in probe.stdout.splitlines())


def health_line(view, directory):
    stages = Counter(row["stage"] for row in view["companies"].values())
    outcomes = Counter(str(row["outcome"]) for row in view["companies"].values())
    return {
        "time": now(),
        "pid": os.getpid(),
        "companies": len(view["companies"]),
        "stages": dict(sorted(stages.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "seeds_running": own_seeds(),
        "guests_held": sum(GUESTS.values()),
        "disk_free_gb": round(shutil.disk_usage(ROOT).free / 1e9, 1),
        "usage_limit_hits_last_hour": usage_limit_hits(directory, time.time() - HOUR),
    }


def append_health(path, line, cap=HEALTH_CAP):
    """One JSON line per loop; the file rotates to <name>.1 once it passes CAP bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > cap:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    with open(path, "a") as stream:
        stream.write(json.dumps(line, sort_keys=True) + "\n")


def selected_companies(run_id):
    return sorted(load(ROOT / "runs" / run_id / "dataset.json").get("companies", []))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("name")
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--parallel", type=int, default=6)
    parser.add_argument("--until", help="ISO time (UTC) after which no company is started")
    parser.add_argument("--work", type=Path, default=ROOT / "experiments" / "hub-cache")
    parser.add_argument("--once", action="store_true", help="one pass over what is selected now, then exit")
    parser.add_argument("--only", nargs="*", default=None, help="restrict this driver to these company ids")
    parser.add_argument(
        "--keep-archives", action="store_true", help="never remove world.old-*/world.rejected-*"
    )
    args = parser.parse_args()
    base = {"dir": BATCH / args.name, "work": args.work.resolve()}
    base["dir"].mkdir(parents=True, exist_ok=True)
    until = datetime.fromisoformat(args.until) if args.until else None
    companies = ROOT / "companies"
    futures, last, owned = {}, {}, set()
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        while True:
            scope = [
                (r, c)
                for r in args.runs
                for c in selected_companies(r)
                if args.only is None or c in args.only
            ]
            # Nearest to delivered first, so a finished world is never queued behind a fresh seed.
            scope.sort(key=lambda pair: -progress(companies / pair[1]))
            expired = until is not None and datetime.now(UTC) > until
            for run_id, company in scope:
                if company in futures.values() or expired or not claim(company):
                    continue
                owned.add(company)
                ctx, log = {**base, "run": run_id}, base["dir"] / f"{company}.log"
                futures[pool.submit(position, companies / company, ctx, log)] = company
            for future in [f for f in futures if f.done()]:
                company = futures.pop(future)
                if exc := future.exception():
                    last[company] = {"stage": "driver", "outcome": repr(exc), "at": now()}
                    with open(base["dir"] / f"{company}.log", "a") as stream:
                        stream.write(f"driver error: {exc!r}  [{now()}]\n")
                else:
                    last[company] = dict(zip(("stage", "outcome"), future.result()), at=now())
            view = status_view(scope, companies, last)
            write_json(base["dir"] / "STATUS.json", view)
            for company, row in view["companies"].items():
                if company not in owned or company in futures.values():
                    continue
                log = base["dir"] / f"{company}.log"
                try:
                    housekeep(
                        base, companies / company, log, row["stage"], row["outcome"], args.keep_archives
                    )
                except OSError as exc:  # a full disk or a vanished path must not stop the driver
                    note(log, f"housekeeping error: {exc!r}")
            append_health(base["dir"] / "health.jsonl", health_line(view, base["dir"]))
            over = ("running", "ready", None)
            runs_over = all(load(ROOT / "runs" / r / "run.json").get("status") not in over for r in args.runs)
            all_done = all(row.get("completed_at") for row in view["companies"].values())
            if not futures and (args.once or expired or (runs_over and all_done)):
                break
            time.sleep(60)
    release(owned)
    print(json.dumps(view, indent=1, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
