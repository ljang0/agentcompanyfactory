"""Deterministic readability triage for a company folder, not a language-model verdict.

Words are whitespace-delimited (as in world_check); sentence fragments separated
by newlines or sentence-ending punctuation count as sentences, including CSV rows.
Jargon density uses world_check.CODE_TOKEN: uppercase codes and 3+ letter capitals,
not an estimate of all specialist vocabulary. Definitions do not remove code hits.

An item is ``system-log`` above 6 codes per 100 words, reusing check_plain_language's
threshold. Below its usual 40-word floor we require at least two codes, so short
machine-like messages are caught without condemning a single reference. Otherwise
it is ``dense`` above 25 words per sentence, above 3 codes per 100 words, or with
undefined acronyms; these lower thresholds invite review before prose becomes a
log. All other items are ``plain``. These are transparent editorial heuristics,
not empirically validated reading levels. A category's verdict is the share of it
that reads badly above a floor, with the worst item deciding a small category.
Empty categories say ``unmeasured``, never plain; unreadable items say dense with
null metrics. Only the brief is reported as blocking: material and message prose
has no repair path, so the report names it as advisory and says so in the summary.
Every measured file is recorded under ``inputs`` with its size and modification
time, so ``stale_reasons`` can invalidate a report without recomputing it.

Acronyms are standalone all-caps tokens of 2–6 letters, excluding code components.
Definitions are collected across company materials and task briefs, never messages:
``XYZ (expanded words)`` or an expansion with matching initials before ``(XYZ)``.
There is no silent acronym allowlist. Capitalized proper names declared in name
fields of the company, canonical world or app states are excluded from code and
acronym counts. Undeclared names and headings can still be false positives.
Only UTF-8 text materials are measured; binary/undecodable files remain in the
report as coverage gaps. HTML message markup is stripped before measurement.
prose_texts supplies material texts; a separate traversal preserves every message
field's JSON pointer, including short fields and objects without record IDs that
the existing grouped helper omits. No state or assignment metadata is counted.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from company_envs.receipt import PASSED, REFUSED, UNMEASURED, measured, rollup
from company_envs.storage import decided_by
from company_envs.world.world_check import CODE_TOKEN, check_plain_language, proper_name_tokens

_ACRONYM = re.compile(r"(?<![\w-])[A-Z]{2,6}(?![\w-])")
# Universally understood abbreviations are not jargon a reader needs defined: currencies,
# time zones, clock and file conventions, and titles any office worker knows.
COMMON_ACRONYMS = frozenset(
    [
        "USD",
        "EUR",
        "GBP",
        "CAD",
        "AUD",
        "JPY",
        "UTC",
        "GMT",
        "EST",
        "EDT",
        "CST",
        "CDT",
        "MST",
        "MDT",
        "PST",
        "PDT",
        "AM",
        "PM",
        "PDF",
        "CSV",
        "XLSX",
        "DOCX",
        "PPTX",
        "URL",
        "ID",
        "OK",
        "FAQ",
        "CEO",
        "CFO",
        "COO",
        "CTO",
        "CIO",
        "VP",
        "HR",
        "IT",
        "PTO",
        "RN",
        "MD",
    ]
)
# Tabular and structured files are data, not prose; they can be dense but never a system log.
TABULAR_SUFFIXES = (".csv", ".tsv", ".xlsx", ".xls", ".json")
_PROSE_KEYS = {
    "body",
    "text",
    "message",
    "description",
    "content",
    "comment",
    "note",
    "notes",
    "snippet",
    "htmlbody",
    "bodyhtml",
    "plainbody",
    "bodytext",
    "textbody",
    "plaintext",
    "htmlcontent",
    "emailbody",
    "messagebody",
    "commentbody",
}
# The prose scale, which is a domain judgement and not a receipt's: how badly a piece of text reads.
# The rollup *semantics* over it are shared -- company_envs.receipt.rollup already decides that an
# empty sequence is unmeasured and never a pass, which three other modules had each hand-written --
# so _category_verdict asks rollup that question and keeps this scale for the answer's name.
_RANK = {"unmeasured": -1, "plain": 0, "dense": 1, "system-log": 2}
_OUTCOME = {"plain": PASSED, "dense": REFUSED, "system-log": REFUSED, "unmeasured": UNMEASURED}

# What decides a readability verdict: this gate's own rules. The same file is
# scripts/batch_companies.py's READABILITY_CODE, and the digest recorded in a report is compared
# against this tuple, so the two have to name the same files. Known gap, reported 2026-09-10 and not
# closed here: the four functions this module imports from world_check.py also decide the verdict,
# and neither the driver's list nor this tuple covers them. Adding world_check.py would take the
# re-take rate from 5 commits in the week to ~31, at 48 reports of 1-8 s each -- about 2 core-hours
# a week, affordable but a different decision from this one, which only changes the mechanism.
DECIDES_THIS = (Path(__file__),)


def current(marker):
    """Whether a REPORT-readability.json was decided by this gate's rules as they stand now.

    The driver's accept slot, bulk_layer.current's contract: the module that writes a verdict is the
    only thing that knows which files decide it, so it answers and the driver only asks. A report with
    no ``decided_by`` is stale -- 48 of 48 on disk on 2026-09-10.
    """
    return isinstance(marker, dict) and marker.get("decided_by") == decided_by(DECIDES_THIS)


# A category is judged on how much of it reads badly, not on its single worst item. Worst-item was
# a function of corpus size: applied to ~8,600 messages it made 44 of 48 worlds "system-log", and
# refused 19 of the 21 that had passed review and the mechanical checks. Los Angeles County failed
# on 20 flagged items out of 12,190. One bad message in ten thousand is a message, not a world.
SHARE_DENSE = 0.02
SHARE_SYSTEM_LOG = 0.05
SMALL_CATEGORY = 12  # too few items for a share to mean anything; the worst item still decides


def _category_verdict(items):
    """The verdict for a whole category: how much of it reads badly, above a floor.

    An empty category said "plain", which is the shape of an empty run writing the same receipt a
    real pass writes: cresa's world was moved aside and re-measuring it gives plain briefs, plain
    materials and plain messages over nothing at all. A category with no items is unmeasured, and
    that rule is receipt.rollup's rather than this module's -- four authors had each written their own
    worst-item rollup with their own answer for the empty case, and this is the one place it lives.
    The share thresholds below stay here, because they are the opposite of a worst-item rollup and
    were measured to be: worst-item over ~8,600 messages made 44 of 48 worlds "system-log".
    """
    if not measured(rollup([{"outcome": _OUTCOME[item["verdict"]]} for item in items], "items")):
        return "unmeasured"
    worst = max((item["verdict"] for item in items), key=_RANK.get)
    if len(items) < SMALL_CATEGORY:
        return worst
    logs = sum(item["verdict"] == "system-log" for item in items) / len(items)
    flagged = sum(item["verdict"] != "plain" for item in items) / len(items)
    if logs >= SHARE_SYSTEM_LOG:
        return "system-log"
    if flagged >= SHARE_DENSE:
        return "dense"
    return "plain"


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")


def _plain_text(text: str) -> str:
    parser = _HTMLText()
    parser.feed(text)
    return "".join(parser.parts)


def _definitions(text: str) -> set[str]:
    defined = set()
    for match in re.finditer(r"(?<![\w-])([A-Z]{2,6})\s*\(([^()\n]+)\)", text):
        # A parenthesized reference or date is not an expansion.
        if len(re.findall(r"[A-Za-z]{2,}", match[2])) >= 2:
            defined.add(match[1])
    for match in re.finditer(r"\(([A-Z]{2,6})\)", text):
        prefix = re.split(r"[.!?;:\n()]", text[: match.start()])[-1]
        # Split camel case as well: JavaScript Object Notation -> J S O N.
        words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)", prefix)[-12:]
        for start in range(len(words)):
            suffix = words[start:]
            for parts in (suffix, [w for w in suffix if w.lower() not in {"of", "and", "the", "for"}]):
                if len(parts) >= 2 and "".join(w[0].upper() for w in parts) == match[1]:
                    defined.add(match[1])
    return defined


def _message_fields(node, pointer="", in_prose=False):
    if isinstance(node, dict):
        for key, value in node.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            is_prose = re.sub(r"[^a-z]", "", key.lower()) in _PROSE_KEYS
            yield from _message_fields(value, f"{pointer}/{escaped}", is_prose)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _message_fields(value, f"{pointer}/{index}", in_prose)
    elif isinstance(node, str) and in_prose:
        yield pointer, _plain_text(node)


def _measure(source: str, text: str, defined: set[str], proper_names=()) -> dict:
    words = len(text.split())
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    average = words / len(sentences) if sentences else 0.0
    codes = sum(token not in proper_names for token in CODE_TOKEN.findall(text))
    density = 100 * codes / words if words else 0.0
    undefined = sorted(set(_ACRONYM.findall(text)) - defined - set(proper_names) - COMMON_ACRONYMS)
    findings = check_plain_language("readability", {source: text}, min_words=1, proper_names=proper_names)
    if words < 40 and codes < 2:
        findings = []
    tabular = source.lower().endswith(TABULAR_SUFFIXES)
    if findings and tabular:
        findings = []
        undefined = undefined or []
        reasons_extra = ["Tabular file with many codes; data rather than prose."]
    else:
        reasons_extra = []
    reasons = [f["message"] for f in findings] + reasons_extra
    if average > 25:
        reasons.append("Average sentence length exceeds 25 words.")
    if density > 3 and not findings:
        reasons.append("More than 3 codes per 100 words.")
    if undefined:
        reasons.append(f"Undefined acronyms: {', '.join(undefined)}.")
    # A typical-length sentence is more representative than a title or longest outlier.
    sample = min(sentences, key=lambda s: abs(len(s.split()) - average)) if sentences else ""
    return {
        "source": source,
        "status": "measured",
        "word_count": words,
        "sentences": len(sentences),
        "average_sentence_length": round(average, 2),
        "code_count": codes,
        "jargon_density": round(density, 2),
        "undefined_acronyms": undefined,
        "sample_sentence": sample,
        "verdict": "system-log" if findings else "dense" if reasons else "plain",
        "reasons": reasons,
    }


def _unreadable(source: str, reason: str) -> dict:
    return {
        "source": source,
        "status": "unreadable",
        "word_count": None,
        "sentences": None,
        "average_sentence_length": None,
        "code_count": None,
        "jargon_density": None,
        "undefined_acronyms": None,
        "sample_sentence": "",
        "verdict": "dense",
        "reasons": [reason],
    }


def measured_paths(folder: str | Path):
    """Every file a report measures, in the order the report walks them.

    The report's freshness was computed from ``world/BULK.json``, ``tasks/_mined/MINE.json`` and
    this module's own mtime -- none of which it reads. So cresa's report could go on describing
    8,442 messages in a world that had been moved aside, and 13 of the 48 reports on disk predate a
    file they measured while the driver's rule caught 11. A report has to name its own inputs.
    """
    folder = Path(folder)
    for relative in ("company.json", "world/world.json"):
        if (folder / relative).is_file():
            yield folder / relative
    yield from sorted(folder.glob("tasks/*/assignment.json"))
    yield from sorted(p for p in (folder / "world/materials").rglob("*") if p.is_file())
    yield from sorted((folder / "world").glob("*.state.json"))


def input_inventory(folder: str | Path) -> dict:
    """path -> size and modification time, for every file a report measures."""
    folder = Path(folder)
    inventory = {}
    for path in measured_paths(folder):
        stat = path.stat()
        inventory[path.relative_to(folder).as_posix()] = {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return inventory


def stale_reasons(folder: str | Path, report: dict) -> list[str]:
    """Why this report no longer describes the folder, by name; empty when it still does.

    A stat per measured file, so a reader can invalidate a 4 MB report without recomputing it.
    A report written before this field existed records no inventory and is stale by definition.
    """
    recorded = report.get("inputs")
    if not isinstance(recorded, dict):
        return ["The report does not record which files it measured."]
    current = input_inventory(folder)
    reasons = []
    for path, stamp in sorted(recorded.items()):
        if path not in current:
            reasons.append(f"{path} was measured and is no longer on disk.")
        elif current[path] != stamp:
            reasons.append(f"{path} changed since it was measured.")
    for path in sorted(set(current) - set(recorded)):
        reasons.append(f"{path} is on disk and was never measured.")
    return reasons


# Only the brief blocks. Measured across the 48 reports on disk: the whole cohort carries exactly
# one flagged brief, while 45 of 48 fail the agreement condition as written and 14 of the 17 that
# fail under today's share rule fail on materials alone -- and a materials finding has no repair
# path at all. world_repair calls materials "a worker's desktop file, not an app record" and leaves
# them; check_plain_language only ever emits warnings, so a message finding cannot reach the repair
# loop; and nothing hands this report to any author. A brief is the one prose the pipeline rewrites
# (brief_rewrite), so it is the one category a block can be repaired out of.
BLOCKING_CATEGORIES = ("brief",)


def readability_report(folder: str | Path) -> dict:
    """Inspect tasks/*/assignment.json and world/ without writing or modifying inputs.

    Each category contains individually located items and its worst-item verdict.
    Bad JSON and non-text briefs are errors, not silently omitted coverage. Missing
    task/world trees are allowed and exposed through counts and coverage warnings.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(folder)
    texts = {"brief": {}, "materials": {}, "messages": {}}
    proper_names = set()
    for path in (folder / "company.json", folder / "world/world.json"):
        if path.is_file():
            proper_names.update(proper_name_tokens(json.loads(path.read_text(encoding="utf-8"))))
    unreadable = []
    for path in sorted(folder.glob("tasks/*/assignment.json")):
        brief = json.loads(path.read_text(encoding="utf-8"))["brief"]
        if not isinstance(brief, str):
            raise TypeError(f"{path}: brief must be a string")
        texts["brief"][f"{path.relative_to(folder).as_posix()}#/brief"] = brief
    materials = {}
    for path in sorted((folder / "world/materials").rglob("*")):
        if not path.is_file():
            continue
        source = path.relative_to(folder).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
            if "\x00" in content:
                raise ValueError("binary content")
        except (UnicodeError, OSError, ValueError):
            unreadable.append(_unreadable(source, "Cannot measure this file as UTF-8 text."))
        else:
            materials[source] = _plain_text(content) if path.suffix.lower() in {".html", ".htm"} else content
    # Keep the complete inventory; _measure classifies tabular data separately.
    texts["materials"] = materials
    for path in sorted((folder / "world").glob("*.state.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        proper_names.update(proper_name_tokens(state))
        for pointer, text in _message_fields(state):
            texts["messages"][f"{path.relative_to(folder).as_posix()}#{pointer}"] = text
    defined = set()
    for text in (*texts["brief"].values(), *texts["materials"].values()):
        defined.update(_definitions(text))
    categories = {}
    coverage = []
    for name, entries in texts.items():
        items = [_measure(source, text, defined, proper_names) for source, text in entries.items()]
        if name == "materials":
            items.extend(unreadable)
        items.sort(key=lambda item: item["source"])
        measured = sum(item["status"] == "measured" for item in items)
        categories[name] = {
            "verdict": _category_verdict(items),
            "item_count": len(items),
            "measured_count": measured,
            "flagged_count": sum(item["verdict"] != "plain" for item in items),
            "items": items,
        }
        if not items:
            coverage.append(f"No {name} items found.")
        if measured < len(items):
            coverage.append(f"{len(items) - measured} {name} files could not be measured.")
    counts = "; ".join(
        f"{name}: {category['verdict']} ({category['flagged_count']} of {category['item_count']} flagged)"
        for name, category in categories.items()
    )
    summary = (
        f"Readability review — {counts}. "
        "Flagged passages need a human read: explain unfamiliar abbreviations, shorten long sentences, "
        "and replace strings of codes in messages with the people, items and decisions they refer to. "
        "Technical exports may legitimately contain codes. These counts indicate writing style, "
        "not factual accuracy or whether a task can be completed."
    )
    if coverage:
        summary += " Coverage gaps: " + " ".join(coverage)
    blocking = sorted(
        name for name in BLOCKING_CATEGORIES if categories.get(name, {}).get("verdict") not in (None, "plain")
    )
    advisory = sorted(
        name
        for name, category in categories.items()
        if name not in BLOCKING_CATEGORIES and category["verdict"] not in ("plain", "unmeasured")
    )
    if blocking:
        summary += f" Blocking: {', '.join(blocking)}."
    if advisory:
        summary += (
            f" Advisory only ({', '.join(advisory)}): no stage rewrites material or message prose, "
            "so these are a human's reading list, not a gate."
        )
    return {
        "schema_version": 2,
        # Which code reached this verdict, for the driver's accept slot. In the report rather than in
        # write_readability_report, so that the report a reader computes and the report on disk are
        # the same object -- test_readability pins that they are.
        "decided_by": decided_by(DECIDES_THIS),
        "summary": summary,
        "categories": categories,
        "blocking": blocking,
        "advisory": advisory,
        "defined_acronyms": sorted(defined),
        "coverage_warnings": coverage,
        "inputs": input_inventory(folder),
    }


def _markdown(report: dict) -> str:
    def cell(value):
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("|", "&#124;")
            .replace("\n", " ")
            .replace("\r", " ")
        )

    lines = ["# Readability report", "", report["summary"], ""]
    for name, category in report["categories"].items():
        lines.extend(
            [
                f"## {name.capitalize()}: {category['verdict']}",
                "",
                "| Source | Verdict | Words | Sentences | Words/sentence | Codes/100 words | Undefined acronyms | Sample | Reasons |",
                "| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
            ]
        )
        for item in category["items"]:
            values = [
                item["source"],
                item["verdict"],
                item["word_count"],
                item["sentences"],
                item["average_sentence_length"],
                item["jargon_density"],
                ", ".join(item["undefined_acronyms"] or []),
                item["sample_sentence"],
                " ".join(item["reasons"]),
            ]
            lines.append("| " + " | ".join(cell(v) if v is not None else "unmeasured" for v in values) + " |")
        lines.append("")
    return "\n".join(lines)


def write_readability_report(folder: str | Path) -> dict:
    """Write REPORT-readability.md and REPORT-readability.json; return the report."""
    folder = Path(folder)
    report = readability_report(folder)
    (folder / "REPORT-readability.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (folder / "REPORT-readability.md").write_text(_markdown(report), encoding="utf-8")
    return report


def calibration_examples() -> dict[str, dict[str, str]]:
    """Good/bad register pairs at invented furniture rental company Cedar Chair."""
    return {
        "brief": {
            "good": "Help Cedar Chair's customer extend her sofa rental. Check availability and charges, agree on terms, and update the booking.",
            "bad": "Reconcile EXT-42 against AVL-17 and SLA gates; execute ACK-09 and synchronize AR disposition.",
        },
        "colleague_message": {
            "good": "Hi Jo, can we collect Mira's sofa on Friday afternoon? Please check the truck schedule before I promise her a time.",
            "bad": "REQ-42 ACK; AVL-17 PASS; ROUTE-09 PENDING; ETA TBD. Dispatch reconciliation required.",
        },
        "customer_message": {
            "good": "Hi Mira, we can extend your sofa rental through Friday for $40. Would you like me to update your booking?",
            "bad": "EXT-42 approved per SLA-07. AR delta USD 40; ACK required before DIS-09 execution.",
        },
        "onboarding_note": {
            "good": "Welcome to Cedar Chair. Check the booking calendar before offering a collection time. Ask Jo if two customers need the same truck.",
            "bad": "Validate AVL-17, reconcile CRM and AR, then enforce SOP-04 ACK gates on all DIS-09 transitions.",
        },
    }
