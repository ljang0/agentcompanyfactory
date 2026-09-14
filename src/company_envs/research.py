"""One expensive research session per firm, producing a reusable dossier."""

import json
import re

from pydantic import Field

from .app_affinity import (
    STANDARD_APPS,
    assign_apps,
    candidate_app_evidence,
    mentions_app,
    under_used_app_categories,
)
from .models import ModelOutputInvalid, strict_schema
from .schemas import Candidate, Claim, Company, Record, Text, minimum_workers
from .sources import Sources, normalize
from .storage import digest
from .world.capabilities import runtime_mounting

# The coordinator's ledger that the dossier author is never asked to read: app
# reservations and their decline reasons, the design stage's feature-cell planning,
# and discovery's occupation gaps. Measured at 16% of a 230 KB research prompt.
# Expansion still receives the whole coverage payload; only this call is narrowed.
COORDINATOR_LEDGER = (
    "assigned_app_history",
    "cell_histogram",
    "company_feature_cells",
    "in_flight_app_usage",
    "uncovered_priority_occupations",
)


def research_view(coverage):
    """Coverage as the dossier author is told to use it, without coordinator bookkeeping."""
    return {key: value for key, value in coverage.items() if key not in COORDINATOR_LEDGER}


# A roster numbered instead of named. Worker ids are not internal keys: they become the login in
# worker_apps.json, the key of identities.json, the desktop directory every material is delivered
# to, and the actor on every record, so "w3" ships a world whose staff directory says w3. Two of the
# 99 exported companies have one (brixmor-property-group, cort, both w1..w6, 70 model hours between
# them), and cort's set-aside world has materials/w1 .. materials/w6 on disk to show for it. Of 415
# distinct worker ids across every dossier written so far, those six are the only ones with a digit
# in them, which is why this pattern can be this blunt. It belongs here and not in schemas.Id: the
# schema is applied to frozen history too, so tightening it would make Company.model_validate reject
# the dossiers already published -- portfolio.freeze_history quarantines the run and
# report.verified_entries drops its entries, which is how a company folder stops being listed in any
# dataset.json and becomes one no driver advances.
PLACEHOLDER_WORKER_ID = re.compile(r"(?:w|wkr|worker|emp|employee|staff|member|person|user|u|p)[-_ ]?\d+")


def placeholder_worker_ids(company):
    """Worker ids that are a position in a list rather than the role the person does."""
    return [worker.id for worker in company.workers if PLACEHOLDER_WORKER_ID.fullmatch(worker.id)]


class DeclinedApp(Record):
    app_id: Text
    reason: Text


class ResearchCompany(Company):
    """Call-only selection explanation; published Company stays schema compatible."""

    declined_apps: list[DeclinedApp] = Field(default_factory=list)


class AppEvidence(Record):
    app_id: Text
    source_url: str = ""
    quote: str = ""


class AppCandidate(Candidate):
    app_evidence: list[AppEvidence] = Field(default_factory=list)


class AppDiscovery(Record):
    candidates: list[AppCandidate]


def capture_app_evidence(sources, candidate, catalogs, surface=None):
    """Capture discovery citations before the dossier author decides what they support."""
    pages = {}
    evidence = candidate_app_evidence(catalogs, candidate, surface)
    for row in evidence:
        if row.source_url not in pages:
            page = sources.capture(row.source_url)
            pages[row.source_url] = {
                "url": row.source_url,
                "excerpt": page.get("text", ""),
                "capture_status": page.get("status", "unreadable"),
                "capture_error": page.get("error", ""),
            }
    for url, page in pages.items():
        quotes = [row.quote for row in evidence if row.source_url == url]
        quotes += [catalogs.apps[row.app_id]["name"] for row in evidence if row.source_url == url]
        page["excerpt"], _ = Sources.bounded_text(
            page["excerpt"], quotes, min(Sources.PAGE_BUDGET, Sources.TOTAL_BUDGET // len(pages))
        )
    return list(pages.values())


def apply_app_evidence(company, candidate, catalogs, excerpts, surface=None):
    """Link each cited app to captured support or an explicit unverified inference."""
    pages = {page["url"]: page for page in excerpts if page.get("url")}
    retained = set()
    evidence = candidate_app_evidence(catalogs, candidate, surface)
    operation_ids = {
        claim_id for item in [*company.workers, *company.outlines] for claim_id in item.evidence_ids
    }
    for row in evidence:
        software = [item for item in company.software if row.app_id in item.catalog_app_ids]
        if not software:
            raise ValueError(f"Keep the candidate's evidenced app {row.app_id} in software.")
        page = pages.get(row.source_url, {})
        text = page.get("excerpt", "")
        captured = page.get("capture_status") == "captured" and not page.get("capture_error")
        mentioned = captured and mentions_app(catalogs, row.app_id, text)
        linked_ids = {claim_id for item in software for claim_id in item.evidence_ids}
        linked = [
            claim
            for claim in company.evidence
            if claim.id in linked_ids
            and claim.source_url == row.source_url
            and claim.id not in operation_ids | {"core-identity"}
            and mentions_app(catalogs, row.app_id, claim.text)
            and not any(
                other.app_id != row.app_id and mentions_app(catalogs, other.app_id, claim.text)
                for other in evidence
            )
        ]
        # A dossier repair can correct a discovery quotation using the captured
        # page. It cannot turn an absent tool mention into sourced adoption.
        quote = next(
            (
                claim.quote
                for claim in linked
                if mentions_app(catalogs, row.app_id, claim.quote)
                and normalize(claim.quote) in normalize(text)
            ),
            row.quote,
        )
        if mentioned and normalize(quote) not in normalize(text):
            raise ValueError(
                f"Correct the discovery quote for {row.app_id}; it does not match the captured page."
            )
        claim_id = linked[0].id if linked else "app-use-" + digest([row.app_id, row.source_url])[:16]
        reason = (
            "The captured page does not mention the tool."
            if captured
            else f"Capture failed ({page.get('capture_status', 'not_attempted')}: {page.get('capture_error', '')})."
        )
        claim = Claim(
            id=claim_id,
            kind="sourced" if mentioned else "inferred",
            text=f"{candidate.real_firm} uses {catalogs.apps[row.app_id]['name']}.",
            source_url=row.source_url,
            quote=quote if mentioned else "",
            basis=""
            if mentioned
            else f"Discovery proposed this adoption. {reason} Actual use is unverified.",
        )
        company.evidence = [old for old in company.evidence if old.id != claim_id] + [claim]
        if not mentioned:
            for old in linked[1:]:
                old.kind, old.quote, old.basis = "inferred", "", claim.basis
        for item in software:
            item.evidence_ids = list(dict.fromkeys([*item.evidence_ids, claim_id]))
        retained.add(row.app_id)
    return retained


def validate_app_assignment(company, assigned, standard_apps, evidenced=()):
    standard_apps = set(standard_apps) | {f"{app}_mock" for app in STANDARD_APPS}
    included = {app for software in company.software for app in software.catalog_app_ids}
    domain = included - set(standard_apps)
    if domain == {"google_sheets_mock"}:
        raise ValueError(
            "Sheets-only domain software is insufficient. Repair with a real domain capability; "
            f"inspect assigned apps: {', '.join(assigned)}."
        )
    adopted = {
        app
        for software in company.software
        if software.capability.strip() and software.rationale.strip()
        for app in software.catalog_app_ids
        if app in assigned
        and app not in standard_apps
        and (software.status == "substitution" or app in evidenced)
    }
    declines = getattr(company, "declined_apps", [])
    declined = {row.app_id: row.reason.strip() for row in declines}
    if len(declined) != len(declines) or set(declined) - set(assigned) or set(declined) & included:
        raise ValueError("declined_apps must name distinct assigned apps that are not included in software")
    if any(not reason for reason in declined.values()):
        raise ValueError("Every declined app needs a nonblank one-sentence reason")
    if assigned and not adopted and set(declined) != set(assigned):
        raise ValueError(
            f"Adopt at least one assigned app ({', '.join(assigned)}) as a domain substitution "
            "with a firm-specific capability and rationale, or decline every assigned app with a reason."
        )
    return {"assigned_apps": assigned, "adopted_apps": sorted(adopted), "declined_apps": declined}


def discover(models, catalogs, prompt, sector, existing, coverage, count):
    economic_prior = catalogs.economic_context(sector)
    economic_prior.pop("occupation_codes")  # Discovery selects firms, not worker SOC IDs.
    payload = {
        "sector": sector,
        "candidate_count": count,
        "existing_firms": existing,
        "coverage": coverage,
        "target_apps": coverage.get("target_apps", [])[:12],
        "target_app_instruction": "Find real firms because they demonstrably use target_apps, within "
        "the requested sector. Never invent companies. For each app candidate return app_evidence as "
        "[{app_id, source_url, quote}], with a public HTTP(S) URL and a short literal quote showing "
        "the firm uses that tool. Use a careers page listing it, an engineering blog post, a vendor "
        "case study or a public integration page. Research will capture the URL. Without this evidence "
        "return app_evidence=[]: the firm is an ordinary sector candidate, not an app candidate. "
        "Consumer and social apps (twitter, instagram, pinterest, reddit, weibo, wechat, xiaohongshu, "
        "zhihu, taobao_seller, 12306, uber_eats, instacart, coinbase, robinhood, ebay, facebook, "
        "google_flights, booking_com, amazon) are legitimate targets only through firms whose business "
        "runs on them, such as a social media agency, marketplace seller, travel desk or restaurant "
        "group. Personal employee use is insufficient. Do not blacklist these apps.",
        "under_used_app_categories": coverage.get("under_used_app_categories")
        or under_used_app_categories(
            catalogs,
            coverage,
            getattr(models, "config", {}).get("design", {}).get("available_runtime_apps"),
            getattr(models, "config", {}).get("design", {}).get("standard_apps", []),
        ),
        "sector_counts": coverage.get("sector_counts", coverage.get("sectors", {})),
        "sector_deficits": coverage.get("sector_deficits", {}),
        "sector_selection_instruction": "Prefer candidates in under-covered sectors with positive "
        "sector_deficits, within the requested sector. Deficits account for queued work; preserve "
        "real business fit and seek complementary operating decisions. Favor firms whose captured "
        "operations need under_used_app_categories; never infer software adoption from this prior.",
        "economic_prior": economic_prior,
        "minimum_workers_per_workflow": minimum_workers(getattr(models, "config", {})),
        "execution_mode": getattr(models, "config", {}).get("design", {}).get("execution_mode", "scheduled"),
        "available_runtime_apps": getattr(models, "config", {})
        .get("design", {})
        .get("available_runtime_apps"),
    }
    if mounting := runtime_mounting(getattr(models, "config", {})):
        payload["runtime_mounting"] = mounting
    found, receipt = models.call(
        "discover", prompt + "\n" + json.dumps(payload, ensure_ascii=False), AppDiscovery
    )
    found = AppDiscovery.model_validate(found.model_dump())
    for candidate in found.candidates:
        candidate.app_evidence = candidate_app_evidence(catalogs, candidate, payload["target_apps"])
    return found, receipt


def research(
    models,
    catalogs,
    prompt,
    candidate,
    coverage,
    feedback="",
    previous=None,
    excerpts=None,
    needs_sources=False,
):
    design = getattr(models, "config", {}).get("design", {})
    standard_apps = design.get("standard_apps", [])
    surface = design.get("available_runtime_apps")
    app_usage = coverage.get("app_usage", coverage.get("portfolio", {}).get("app_usage", {}))
    available = sorted(set(catalogs.apps) if surface is None else set(surface) & set(catalogs.apps))
    lowest_usage = min((app_usage.get(app_id, 0) for app_id in available), default=0)
    under_used_apps = [app_id for app_id in available if app_usage.get(app_id, 0) == lowest_usage]
    if surface is not None and (unsupported := sorted(set(standard_apps) - set(surface))):
        raise ValueError(
            "design.standard_apps must be included in design.available_runtime_apps; "
            f"outside the runtime surface: {', '.join(unsupported)}"
        )
    # Preserve historical research without a declared runtime surface.
    assigned = coverage.get("assigned_apps")
    if assigned is None:
        assigned = (
            assign_apps(
                catalogs,
                candidate,
                coverage,
                seed=getattr(models, "config", {}).get("generation", {}).get("seed", 0),
                surface=surface,
                standard_apps=standard_apps,
                excerpts=excerpts or [],
            )
            if surface is not None
            else []
        )
    app_evidence = candidate_app_evidence(catalogs, candidate, surface)
    infrastructure = set(standard_apps) | {f"{app}_mock" for app in STANDARD_APPS}
    assigned = list(
        dict.fromkeys([*(row.app_id for row in app_evidence if row.app_id not in infrastructure), *assigned])
    )
    response_type = ResearchCompany if assigned else Company
    schema = strict_schema(response_type)
    worker_floor = minimum_workers(getattr(models, "config", {}))
    schema["properties"]["workers"]["minItems"] = worker_floor
    if worker_floor > 2:
        schema["$defs"]["Outline"]["properties"]["worker_ids"]["minItems"] = worker_floor
    for field, value in (
        ("id", candidate.id),
        ("real_firm", candidate.real_firm),
        ("sector", candidate.sector),
    ):
        schema["properties"][field]["enum"] = [value]
    # SOCs plus apps exceed the provider's 1,000-enum-value schema limit.
    schema["$defs"]["Worker"]["properties"]["soc"]["pattern"] = (
        "^(?:" + "|".join(sorted(catalogs.occupations)) + ")$"
    )
    # Constrain the author to the run's declared runtime surface when there is one, so no
    # research or design call is spent on apps the runtime cannot host.
    schema["$defs"]["Software"]["properties"]["catalog_app_ids"]["items"]["enum"] = sorted(
        set(surface) & set(catalogs.apps) if surface is not None else catalogs.apps
    )
    payload = {
        "candidate": candidate.model_dump(),
        "coverage": research_view(coverage),
        "economic_prior": catalogs.economic_context(candidate.sector),
        "software_catalog": catalogs.software_context(surface),
        "standard_apps": standard_apps,
        "under_used_apps": under_used_apps,
        "assigned_apps": assigned,
        "target_apps": coverage.get("target_apps", []),
        "candidate_app_evidence": [row.model_dump() for row in app_evidence],
        "captured_app_pages": [
            page for page in excerpts or [] if page.get("url") in {row.source_url for row in app_evidence}
        ],
        "candidate_app_instruction": "Keep every app in candidate_app_evidence in software. Link its "
        "adoption claim through evidence_ids with kind=sourced only when captured_app_pages supports "
        "the firm's use of the tool. Software.status describes the catalog mapping, not evidence: "
        "use catalog_candidate or substitution as appropriate. A failed capture or a page with no "
        "mention of the tool means kind=inferred, retaining the attempted URL and explaining the "
        "uncertainty. It does not reject the firm. Keep other under-used apps where plausible. "
        "Evidence-backed assigned apps satisfy the assignment rule without a synthetic substitution.",
        "assigned_app_instruction": "For assignments without candidate app evidence, adopt at least "
        "one assigned app as a domain software requirement "
        "with status=substitution and a firm-specific use in capability and rationale. Adoption of a "
        "plausible app is preferred to declining. Otherwise return declined_apps as [{app_id, reason}], "
        "with a one-sentence reason for every assigned app. Do not claim actual product adoption without "
        "captured evidence. A dossier whose only domain app is google_sheets_mock will be rejected. "
        "Regional consumer apps require captured evidence of operations in that market.",
        "app_selection_instruction": "Prefer under_used_apps when their documented capabilities fit "
        "the firm's work. Never invent adoption: label proposed tools as synthetic substitutions "
        "unless captured evidence establishes real adoption. The standard_apps bundle remains mandatory.",
        "evidence_instruction": "Ground the real firm name, sector and operations summary in at least "
        "one captured public page, using a sourced claim with id core-identity. Start with capturable "
        "homepage, about, careers or press pages. Uncapturable sources are demoted to inferred, retaining "
        "the URL as an attempted source with capture status in the evidence report. This does not establish "
        "facts; explain inference bases and avoid mostly inferred grounding. Quotes on captured pages "
        "must match their text; do not demote a quote mismatch to evade correction.",
        "revision_feedback": feedback,
        "minimum_workers_per_workflow": worker_floor,
        # Capture failures caused 70 of 82 recorded research repairs. The author cannot
        # see our capture results from its own search, so name the hosts already known
        # to refuse us rather than paying another research call to rediscover them. The
        # frozen skill says what to do with them; this key is only the list.
        "unreadable_hosts": coverage.get("unreadable_hosts", []),
        "execution_mode": getattr(models, "config", {}).get("design", {}).get("execution_mode", "scheduled"),
        "available_runtime_apps": getattr(models, "config", {})
        .get("design", {})
        .get("available_runtime_apps"),
    }
    if mounting := runtime_mounting(getattr(models, "config", {})):
        payload["runtime_mounting"] = mounting
    use_tools = getattr(models, "config", {}).get("design", {}).get("read_only_tools", False)
    context = catalogs.tool_context(excerpts or [], surface) if use_tools else None
    if use_tools:
        # read_source serves only the frozen packet. 84% of jobs start with an empty
        # packet, and advertising the tool anyway spent 94 of 162 first-call read_source
        # turns on URLs that were never there. Name what is readable instead.
        readable = [
            page["url"] for page in excerpts or [] if page.get("url") and (page.get("excerpt") or "").strip()
        ]
        payload["reference_access"] = {
            "tools": [
                "search_catalog",
                "read_app_schema",
                "calculate",
                *(["read_source"] if readable else []),
            ],
            "readable_source_urls": readable,
            "scope": "Read-only frozen catalog documentation and supplied captured pages. "
            "read_source serves only readable_source_urls; it cannot fetch a URL you found by "
            "searching. App descriptions and schemas do not establish deployed behavior.",
        }
    job = "research"
    if previous is not None:
        try:
            source_status = json.loads(feedback).get("source_status", [])
        except (ValueError, AttributeError):
            source_status = []
        failed_pages = {
            row.get("attempted_source_url") or row.get("source_url") or row.get("url"): {
                "url": row.get("attempted_source_url") or row.get("source_url") or row.get("url"),
                "capture_status": row.get("capture_status", "unreadable"),
                "capture_error": row.get("capture_error", ""),
            }
            for row in [*source_status, *(excerpts or [])]
            if row.get("capture_error")
            or row.get("capture_status", "captured") not in ("captured", "unknown")
        }
        payload["failed_source_pages"] = list(failed_pages.values())
        # Demotion can leave only software support or mostly inferred grounding;
        # these repairs still need web access even if another page was captured.
        needs_sources = needs_sources or bool(failed_pages)
        job = "research" if needs_sources else "research_repair"
        payload.update(
            previous_dossier=previous.model_dump() if isinstance(previous, Company) else previous,
            captured_source_excerpts=excerpts or [],
        )
        if needs_sources:
            prompt += "\nThis is a bounded source repair. failed_source_pages lists the pages that failed and their capture status. Prefer capturable homepage, about, careers and press pages before relying on inference. Find alternative public pages instead of repeating failed URLs. Keep at least one captured page grounding the core identity and operations; an attempted source is not evidence. Preserve useful inferred organization and synthetic outlines.\n"
        else:
            prompt += "\nThis is a bounded dossier repair without new web research. Reuse the supplied dossier and captured evidence for unchanged facts. Correct the specified role, organization, software or quote issues. Do not introduce unsourced new real-firm facts.\n"
    kwargs = {"schema": schema}
    if context is not None:
        kwargs["context"] = context
    company, receipt = models.call(
        job, prompt + "\n" + json.dumps(payload, ensure_ascii=False), response_type, **kwargs
    )
    try:
        evidenced = apply_app_evidence(company, candidate, catalogs, excerpts or [], surface)
    except ValueError as exc:
        raise ModelOutputInvalid(str(exc), company.model_dump(), receipt) from exc
    included_apps = {app_id for software in company.software for app_id in software.catalog_app_ids}
    if missing := sorted(set(standard_apps) - included_apps):
        raise ModelOutputInvalid(
            f"company software is missing mandatory standard apps: {', '.join(missing)}",
            company.model_dump(),
            receipt,
        )
    if worker_floor > 2 and (
        len(company.workers) < worker_floor or any(len(o.worker_ids) < worker_floor for o in company.outlines)
    ):
        raise ModelOutputInvalid(
            f"company and each outline require at least {worker_floor} participating workers",
            company.model_dump(),
            receipt,
        )
    if placeholders := placeholder_worker_ids(company):
        raise ModelOutputInvalid(
            f"worker ids name the role, not a position in a list: rename {', '.join(placeholders)} "
            "to the work each person does (lease-administration-specialist, inventory-controller), "
            "and update every team, outline and evidence reference to the new id",
            company.model_dump(),
            receipt,
        )
    if assigned or surface is not None:
        try:
            decision = validate_app_assignment(company, assigned, standard_apps, evidenced)
        except ValueError as exc:
            raise ModelOutputInvalid(str(exc), company.model_dump(), receipt) from exc
        receipt = {**receipt, "app_assignment": {"company_id": candidate.id, **decision}}
    # Cached model output keeps declines; author lineage keeps the selection decision.
    if isinstance(company, ResearchCompany):
        company = Company.model_validate(company.model_dump(exclude={"declined_apps"}))
    return company, receipt
