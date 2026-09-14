"""Visual gate: a model looks at what a worker would see and says whether it is real and working.

The render check proves an app hydrated its seeded state and wrote nothing back; it reads text.
Text cannot tell a working inbox from a page whose main pane says "Channel not found", nor spot
the shipped demo rows sitting beside the company's own, a debug button meant for us, or a broken
avatar. This sends the same screenshots the render check saved, plus each worker's start page, to
the model that judges them one at a time, and writes ``runtime/VISUAL.json``.

Every judged screen gets a small number of independent votes; a screen is blocking only when a
majority of its votes call it blocking, so a single harsh reading cannot fail a company.

The judge names *which* fault blocks, from a closed list of things a repair round can act on, and
everything else it sees is reported without blocking. It used to answer a bare yes/no, and the
two answers that most often came back yes were business arithmetic: "the net income figure could
mislead someone about profitability" (bluestone's quickbooks) and "contradictory employee counts
could mislead a manager" (childrens-aid's adp) held 2 of the 12 visually judged companies, and no
stage downstream recomputes a company's books from a screenshot.

It is also handed the numbers the render check already counted on the same page. A judge that can
only see pixels voted ``looks_real: true, works: true`` on a Drive showing 1 of 400 of the
company's own values in about 210 characters, three times over.
"""

import json
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from company_envs.receipt import OUTCOME_OK, PASSED, Receipt, faulted, outcome_of, rollup
from company_envs.storage import decided_by, now, read, write

from . import render_check
from .render_check import SHOT_DIR, find_browser, keep_screenshot, render_dom

VOTES = 3
PROBLEM_LENGTH = 200
NOTE_LENGTH = 300
VISUAL = "runtime/VISUAL.json"


# The faults a repair round has somewhere to send: a world the generator can refill, records the
# seed can replace, an identity the proxy assigns, a screen a clone patch can fix. Anything a
# judge sees that is not one of these is reported and does not block, because a gate that fails a
# world no stage can repair turns into a permanent stall.
BLOCKING_REASONS = ("none", "empty", "foreign_records", "wrong_identity", "broken")


class VisualVerdict(BaseModel):
    """One look at one screen."""

    model_config = ConfigDict(extra="forbid", strict=True)

    looks_real: bool = Field(description="The screen looks like the real product holding this company's data")
    works: bool = Field(description="The screen is in a working state, ready to be used")
    problems: list[str] = Field(
        default_factory=list, description=f"Concrete problems, each under {PROBLEM_LENGTH} characters"
    )
    blocking_reason: Literal["none", "empty", "foreign_records", "wrong_identity", "broken"] = Field(
        description="Which of the listed faults stops someone working here, or 'none'"
    )
    note: str = Field(default="", description=f"One sentence, under {NOTE_LENGTH} characters")


APP_PROMPT = """You are looking at the first screen an employee sees when they open {what} at
{company}, signed in as {person}. This is a working copy of the app, filled with that company's
own records.

Counted from this same page by the check that took the screenshot:
{measured}

Those counts are context, not a verdict. A screen that draws its content, or that opens on
totals rather than rows, can hold the whole company's world and still match few of its values.
A screen that matches almost none of them and has an empty main pane is the empty case below.

Say whether it looks like the real product with this company's data in it, whether it is in a
working state, and list the concrete problems you can see.

Count as problems: error text, "not found", or an empty main pane where records belong; a loading
screen that never finished; sample or demo content that belongs to the app's own template rather
than this company (people, files or rows from somewhere else); a control left in a state nobody
chose, such as a selection or an upload the person did not make; broken images; text that names a
generator rather than a business.

These are working copies of the products, and their own branding is deliberately altered: a name
or logo that is a near-miss of the real product is expected and is not a problem. Judge the
company's records and whether the screen works, not the product's identity.

None of the following is a problem, and none of them blocks: a label or title cut off to fit its
column, which is what every real product does; a period with little or nothing in it, since a
quiet week is a fact about the business and not a fault; a shop, brand, team or product name that
differs from the company's own name; a colour, spacing or density choice you would have made
differently.

Set blocking_reason to the one thing that would stop someone sitting down to work here, and to
"none" when nothing would:

- "empty": the main pane holds no records where this company's records belong, or the screen
  never finished loading.
- "foreign_records": the rows on screen belong to the app's own demo template, or to some other
  company, rather than to this one.
- "wrong_identity": the account signed in is not the person named above.
- "broken": error text, a dead screen, a control that lies about what it will do, a broken
  image, or a raw URL or file path shown where a picture or a name belongs.

Everything else goes in problems with blocking_reason "none". A figure you believe is wrong in
particular -- a total that does not add up, two numbers on the page that disagree, a sign or a
percentage that looks implausible -- is reported and never blocks: nothing downstream recomputes
this company's books from a screenshot, so a screen held back for that is held back forever.
Write what you saw in problems and let the person reading the report decide. Judge only what you
can see."""

DESKTOP_PROMPT = """You are looking at the browser start page on the work computer of {person},
{title} at {company}. Their files are in the Desktop, Documents and Downloads folders:
{files}

Say whether this looks like a real employee's computer at this company, whether it is ready to
work in, and list the concrete problems you can see.

Count as problems: placeholder or sample text; addresses or identifiers shown where a person
would expect names; wording that names a generator rather than a business; anything unfinished
or broken.

People here hold different apps on purpose, the way accounts differ at a real company, and a
colleague does the work their own account covers. A file this person cannot open with their own
apps is normal and is not a problem.

Set blocking_reason the same way as for an app screen: "empty" when the page holds nothing where
this person's apps and files belong, "foreign_records" when it names somebody else's company or
files, "wrong_identity" when it greets the wrong person, "broken" when something on it is broken
or shows a raw path, and "none" otherwise. Anything else goes in problems without blocking. Judge
only what you can see."""


def _bounded(verdict):
    """Trim a verdict's free text so one long answer cannot bloat the report.

    ``blocking`` is derived, not asked for: a screen blocks when the judge named one of the
    repairable faults, and nothing else it reports can hold a company.
    """
    reason = verdict.blocking_reason if verdict.blocking_reason in BLOCKING_REASONS else "none"
    return {
        "looks_real": bool(verdict.looks_real),
        "works": bool(verdict.works),
        "blocking_reason": reason,
        "blocking": reason != "none",
        "problems": [str(p)[:PROBLEM_LENGTH] for p in verdict.problems][:8],
        "note": str(verdict.note)[:NOTE_LENGTH],
    }


def judge_screen(models, image, prompt, votes=VOTES):
    """Ask VOTES times about one screen; the majority decides, and every answer is kept.

    A judge that cannot be reached is not a verdict: the error is recorded and the screen is left
    unjudged (``blocking`` false), so a provider outage never fails a company on looks.
    """
    answers, errors = [], []
    for index in range(max(1, votes)):
        # Each ballot asks a distinguishable question: an identical prompt is one call whose
        # answer the cache hands back, which reads as agreement between judges who never sat.
        ballot = prompt if votes <= 1 else f"{prompt}\n{json.dumps({'vote': index})}"
        try:
            verdict, _receipt = models.call("visual_judge", ballot, VisualVerdict, images=[str(image)])
        except Exception as exc:  # noqa: BLE001 -- any judge failure is reported, never a verdict
            errors.append(f"{type(exc).__name__}: {exc}"[:PROBLEM_LENGTH])
            continue
        answers.append(_bounded(verdict))
    if not answers:
        return {
            "ok": True,
            "judged": False,
            "votes": 0,
            "errors": errors,
            "problems": [],
            "blocking": False,
            "blocking_reason": "none",
        }
    blocking = sum(a["blocking"] for a in answers) * 2 > len(answers)
    problems, seen = [], set()
    for answer in answers:
        for problem in answer["problems"]:
            key = problem.strip().lower()[:80]
            if key not in seen:
                seen.add(key)
                problems.append(problem)
    named = [a["blocking_reason"] for a in answers if a["blocking_reason"] != "none"]
    return {
        "ok": not blocking,
        "judged": True,
        "votes": len(answers),
        "blocking": blocking,
        "blocking_reason": max(set(named), key=named.count) if blocking and named else "none",
        "looks_real": sum(a["looks_real"] for a in answers) * 2 > len(answers),
        "works": sum(a["works"] for a in answers) * 2 > len(answers),
        "problems": problems[:12],
        "note": answers[0]["note"],
        "errors": errors,
    }


def company_name(folder):
    company = read(Path(folder) / "company.json") if (Path(folder) / "company.json").is_file() else {}
    return company.get("name") or Path(folder).name


def worker_titles(folder):
    company = read(Path(folder) / "company.json") if (Path(folder) / "company.json").is_file() else {}
    return {w["id"]: w.get("title", w["id"]) for w in company.get("workers", [])}


def worker_names(folder):
    """Each worker's person name, from the identities the apps were seeded with."""
    path = Path(folder) / "world/identities.json"
    identities = read(path) if path.is_file() else {}
    names = {}
    for worker, apps in identities.items():
        for record in (apps or {}).values():
            if isinstance(record, dict):
                name = record.get("fullName") or record.get("name") or record.get("username")
                if isinstance(name, str) and name.strip():
                    names[worker] = name.strip()
                    break
    return names


def app_screens(folder, report):
    """The app screenshots a render check saved, as (name, image path, its measurements) triples."""
    folder = Path(folder)
    screens = []
    for app_id, result in sorted((report or {}).get("apps", {}).items()):
        shot = result.get("screenshot")
        path = folder / shot if shot else folder / SHOT_DIR / f"{app_id}.jpg"
        if path.is_file():
            screens.append((app_id, path, result))
    return screens


def measured(result):
    """The render check's own numbers for one screen, as lines a judge can read.

    The check counted the visible characters, the clickable controls and how many of the
    company's own values reached the page, on the very render this screenshot came from, and then
    the judge was shown only the picture. Three judges voted a Drive showing 1 of 400 values
    ``looks_real: true, works: true``; told the number, none of them would have.
    """
    lines = []
    seen, _, total = (result or {}).get("seeded_strings_seen", "").partition("/")
    if total.isdigit() and int(total) > 0:
        lines.append(f"- {seen} of {total} of this company's own record values are visible on it")
    if result:
        controls = result.get("controls")
        lines.append(
            f"- {result.get('text_chars', 0)} characters of visible text"
            + (f", {controls} things to click" if controls is not None else "")
        )
        if result.get("route", "/") != "/":
            lines.append(f"- this is the app's {result['route']} page, where a worker lands")
        if result.get("broken_images"):
            lines.append(f"- {len(result['broken_images'])} image(s) the browser could not draw")
        if result.get("empty_images"):
            lines.append(f"- {result['empty_images']} image(s) the app drew from its own empty default")
    return "\n".join(lines) or "- (nothing was measured for this screen)"


def desktop_screens(folder, task_id, *, host_ip="10.0.2.2", browser=None, work=None, limit=2):
    """Render each worker's start page as their browser would open it; (worker, path, files) each.

    The page is built by the same planner the VM launcher uses, so this is the real start page,
    not a copy of it. Only LIMIT workers are rendered: the pages differ by name and app list, and
    a company pays for this on every construction.
    """
    from .hub_vm import _plan

    folder = Path(folder)
    browser = browser or find_browser()
    if browser is None:
        return []
    boss, _assumed, plans = _plan(folder, task_id, host_ip, None)
    order = [boss] + [w for w in sorted(plans) if w != boss]
    shots = folder / SHOT_DIR
    screens = []
    for worker in order[: max(1, limit)]:
        page = plans[worker]["files"].get("Desktop/APPS.html")
        if not page:
            continue
        staging = Path(tempfile.mkdtemp(prefix="desktop-page-", dir=work))
        try:
            source = staging / "APPS.html"
            source.write_bytes(page)
            profile = staging / "profile"
            raw = staging / "screen.png"
            _dom, error = render_dom(browser, source.as_uri(), profile=str(profile), screenshot=raw)
            picture = None if error else keep_screenshot(raw, shots / f"desktop-{worker}.jpg")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if picture:
            screens.append((worker, picture, plans[worker]["materials"]))
    return screens


def judge_folder(folder, models, *, task_id=None, votes=VOTES, host_ip="10.0.2.2", work=None):
    """Judge every app's first view and the workers' start pages; the VISUAL.json content."""
    folder = Path(folder)
    company = company_name(folder)
    names, titles = worker_names(folder), worker_titles(folder)
    render = read(folder / "runtime/RENDER.json") if (folder / "runtime/RENDER.json").is_file() else {}
    report = {
        "ok": True,
        "at": now(),
        # Which code reached this verdict; see render_check for why it is in the report itself rather
        # than in the writer, and why the empty returns below carry it too.
        "decided_by": decided_by(DECIDES_THIS),
        "votes": votes,
        "apps": {},
        "desktops": {},
    }
    # Whether there could have been anything to judge, carried forward from the render rather than
    # inferred here: this module cannot see a browser, but the render could and already said. A host
    # that cannot render takes no screenshots, which is a fault about the environment and not about
    # the world, and it is the one empty report this gate admits -- see ``admissible``.
    if render.get("skipped"):
        report["skipped"] = f"nothing was rendered to judge: {render['skipped']}"
    person = names.get(next(iter(names), ""), "an employee") if names else "an employee"

    from .hub_vm import app_label

    for app_id, image, seen in app_screens(folder, render):
        label, purpose = app_label(app_id)
        prompt = APP_PROMPT.format(
            what=f"{label} ({purpose.lower()})", company=company, person=person, measured=measured(seen)
        )
        result = judge_screen(models, image, prompt, votes)
        result["screenshot"] = str(image.relative_to(folder))
        report["apps"][app_id] = result
        report["ok"] = report["ok"] and result["ok"]

    if task_id:
        try:
            screens = desktop_screens(folder, task_id, host_ip=host_ip, work=work)
        except (OSError, ValueError, KeyError) as exc:
            report["desktops"] = {}
            report["desktop_error"] = f"{type(exc).__name__}: {exc}"[:PROBLEM_LENGTH]
            screens = []
        for worker, image, files in screens:
            listing = "\n".join(f"- {name}" for name in files[:24]) or "- (no files)"
            prompt = DESKTOP_PROMPT.format(
                person=names.get(worker, worker),
                title=titles.get(worker, worker),
                company=company,
                files=listing,
            )
            result = judge_screen(models, image, prompt, max(1, votes - 2))
            result["screenshot"] = str(image.relative_to(folder))
            report["desktops"][worker] = result
            report["ok"] = report["ok"] and result["ok"]
    # A judgement of no screens is not a judgement that passed, and ``ok`` is derived from the
    # outcome so the two cannot disagree. For every judgement that has ever happened this is the
    # same value it was; what changes is the empty case, and which kind of empty it was.
    judged = [*report["apps"].values(), *report["desktops"].values()]
    whole = rollup(judged, "judged screens")
    if not judged and report.get("skipped"):
        # The render could not be taken at all, so neither could this. A fault about the host.
        whole = Receipt.faulted(report["skipped"])
    report.update(whole.body, ok=OUTCOME_OK[whole.outcome], skipped=report.get("skipped"))
    if report["skipped"] is None:
        del report["skipped"]
    return report


# Everything the render's verdict depends on, plus this file: a visual verdict is taken *of* the
# screenshots the render produced, so any change that alters what the browser draws alters what the
# judge sees. Reusing render_check's list rather than restating it is the point -- the two were
# separate hand-copied tuples and neither named hub_app.py, so the storage-quota fix could not
# restale either a RENDER.json or a VISUAL.json.
DECIDES_THIS = (Path(__file__), *render_check.DECIDES_THIS)


def current(marker):
    """Whether a VISUAL.json was decided by the judging rules and the render code as they stand now.

    The driver's accept slot, bulk_layer.current's contract; a marker with no ``decided_by`` is stale.
    Separate from ``admissible`` for the reason given in render_check.current: the accept slot is a
    re-run trigger, so "the code changed" and "the world is not ready" are different answers and the
    driver's row needs both predicates.
    """
    return isinstance(marker, dict) and marker.get("decided_by") == decided_by(DECIDES_THIS)


def admissible(report):
    """Whether a VISUAL.json lets a company go on; the same three answers as the render's.

    A host that cannot render takes no screenshots, so the fault is forwarded from RENDER.json and
    admitted here for the same reason it is admitted there: no stage can install a browser, and the
    VM stage looks at these screens again in a real one. A judgement that had screens and reached no
    verdict, or had none while the render succeeded, is a world nobody has looked at and is refused.
    Measured 2026-09-10: 0 of the 12 VISUAL.json on disk judged zero screens, so this closes a
    latent hole rather than changing any company's standing.
    """
    return outcome_of(report) == PASSED or (faulted(report) and bool(report.get("skipped")))


def run_visual_judge(folder, models, **options):
    """Judge the folder's screens and write ``runtime/VISUAL.json``; returns the report."""
    folder = Path(folder)
    report = judge_folder(folder, models, **options)
    write(folder / VISUAL, report)
    return report


def format_lines(report):
    """One line per judged screen, then the overall verdict."""
    lines = []
    for kind in ("apps", "desktops"):
        for name, result in (report.get(kind) or {}).items():
            if not result.get("judged"):
                lines.append(f"SKIP {name}: judge unavailable ({'; '.join(result.get('errors', []))[:120]})")
                continue
            first = result["problems"][0] if result["problems"] else result.get("note", "")
            lines.append(f"{'PASS' if result['ok'] else 'FAIL'} {name}: {first[:150]}")
    screens = len(report.get("apps") or {}) + len(report.get("desktops") or {})
    failed = [n for k in ("apps", "desktops") for n, r in (report.get(k) or {}).items() if not r["ok"]]
    lines.append(
        f"visual-judge {'ok' if report['ok'] else 'FAILED'}: {screens} screens"
        + (f", failed {', '.join(failed)}" if failed else "")
    )
    return lines


def visual_ok(report):
    """Whether a VISUAL.json report accepts the world; a missing or unreadable report does not."""
    return bool(report) and report.get("ok") is True


__all__ = [
    "BLOCKING_REASONS",
    "VISUAL",
    "VisualVerdict",
    "format_lines",
    "judge_folder",
    "judge_screen",
    "measured",
    "run_visual_judge",
    "visual_ok",
]
