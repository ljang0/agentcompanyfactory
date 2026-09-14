"""Deterministic catalog affinity and coverage-aware app assignments; no model calls."""

import re
from collections import Counter, defaultdict
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit

from . import schemas
from .holdout import identity_keys
from .storage import digest, read
from .world.hub_app import hub_apps

# Product priors describe plausible synthetic capabilities, never actual adoption.
SECTOR_APPS = {
    "Health Care and Social Assistance": "epic-health PACS-viewer Canvas-LMS",
    "Retail Trade": "shopify_admin woocommerce amazon_seller ebay instacart klaviyo mailchimp amazon uber_eats",
    "Wholesale Trade": "shopify_admin woocommerce amazon_seller ebay instacart klaviyo mailchimp SAP",
    "Finance and Insurance": "quickbooks Expensify stripe_dashboard paypal robinhood coinbase",
    "Information": "github gitlab jira linear sentry circleci vercel datadog aws_console azure cloudflare postman amplitude mixpanel hotjar looker_studio tableau",
    "Professional, Scientific, and Technical Services": "clio docusign contractbook notion miro lucidchart monday trello asana airtable hubspot salesforce Zendesk",
    "Government": "ServiceNow SAP workday docusign tableau Canvas-LMS",
    "Manufacturing": "SAP ServiceNow monday lucidchart tableau",
    "Real Estate and Rental and Leasing": "zillow docusign contractbook quickbooks",
}
SECTOR_KEYWORDS = {
    "Health Care and Social Assistance": (
        "medical",
        "patient",
        "radiology",
        "clinical",
        "learning",
        "course",
    ),
    "Retail Trade": ("store", "shopping", "seller", "commerce", "merchandise", "cart", "retail"),
    "Wholesale Trade": ("inventory", "procurement", "supplier", "commerce", "seller", "warehouse"),
    "Finance and Insurance": (
        "accounting",
        "invoice",
        "payment",
        "expense",
        "trading",
        "banking",
        "insurance",
    ),
    "Information": ("repository", "deployment", "developer", "analytics", "cloud", "monitoring", "api"),
    "Professional, Scientific, and Technical Services": (
        "legal",
        "contract",
        "project",
        "diagram",
        "design",
        "consulting",
    ),
    "Government": ("government", "public sector", "procurement", "service management", "education"),
    "Manufacturing": (
        "manufacturing",
        "production",
        "procurement",
        "inventory",
        "supply chain",
        "engineering",
    ),
    "Real Estate and Rental and Leasing": ("property", "real estate", "rental", "lease", "listing"),
}
HR_APPS = ["workday", "bamboohr", "gusto", "lattice", "greenhouse", "adp"]
COMMS_APPS = ["microsoft_teams", "zoom_web", "outlook_web", "discord", "dingtalk", "feishu", "wechat"]
REGIONAL_APPS = ["12306", "weibo", "xiaohongshu", "taobao_seller", "zhihu", "aliyun"]
MARKETING_APPS = [
    "google_ads",
    "google_analytics",
    "hubspot_marketing",
    "meta_ads",
    "facebook",
    "instagram",
    "linkedin",
    "pinterest",
    "reddit",
    "twitter",
    "youtube",
]
TRAVEL_APPS = ["booking_com", "expedia", "google_flights"]
STANDARD_APPS = sorted(app.removesuffix("_mock") for app in schemas.STANDARD_APPS)


def _base(app_id):
    return app_id.removesuffix("_mock")


def _matches(text, keyword):
    return bool(re.search(r"(?<!\w)" + re.escape(keyword.casefold()) + r"(?!\w)", text.casefold()))


def surface_apps(catalogs, surface=None):
    apps = hub_apps([row for row in catalogs.apps.values() if isinstance(row, dict)])
    return sorted(apps if surface is None else apps.keys() & set(surface))


def app_coverage(root, state, catalogs):
    """Count real firms across published dossiers and this run's app reservations.

    Published versions and repeated jobs for one firm count once per app. Queued
    targets are reservations, not accepted adoption, so report the counts separately.
    """
    surface = state["config"].get("design", {}).get("available_runtime_apps", catalogs.apps)
    available = set(catalogs.apps) if surface is None else set(surface) & set(catalogs.apps)
    accepted, queued = defaultdict(set), defaultdict(set)

    def add(company, destination, apps):
        firm = identity_keys(company)["name"]
        for app in available.intersection(apps):
            destination[app].add(firm)

    for path in sorted((Path(root) / "data" / "companies").glob("*/*.json")):
        company = read(path)
        add(company, accepted, [app for row in company["software"] for app in row["catalog_app_ids"]])
    for job_id, job in state.get("jobs", {}).items():
        if job.get("reuse_dossier") or job.get("status") == "rejected":
            continue
        candidate = job["candidate"]
        apps = set()
        if job.get("status") != "reviewed":
            apps.update(job.get("coverage", {}).get("assigned_apps", []))
            apps.update(row["app_id"] for row in candidate.get("app_evidence", []))
        path = Path(root) / "runs" / state["id"] / "jobs" / job_id / "company.json"
        if path.exists():
            company = read(path)
            apps.update(app for row in company["software"] for app in row["catalog_app_ids"])
        add(candidate, queued, apps)
    return {
        "app_coverage": {
            app: {"accepted_firms": len(accepted[app]), "run_firms": len(queued[app])}
            for app in sorted(available)
        },
        "uncovered_apps": sorted(app for app in available if not accepted[app] and not queued[app]),
    }


def target_apps(uncovered, requests):
    """Ask about the least-requested uncovered apps first, at most twelve per call."""
    return sorted(set(uncovered), key=lambda app: (requests.get(app, 0), app))[:12]


def affinity_table(catalogs, surface=None):
    """Scores use names/notes and only the first 60 schema lines, plus explicit priors.

    Zero means no sector fit. A weak professional-services fallback covers new
    catalog products; it is a proposal to inspect, not evidence of suitability.
    """
    result = {}
    for app_id in surface_apps(catalogs, surface):
        app = catalogs.apps[app_id]
        path = Path(catalogs.path) / "app_schemas" / Path(app["schema"]).name
        with path.open(encoding="utf-8") as stream:
            excerpt = "".join(islice(stream, 60))
        text = " ".join((app.get("name", ""), app.get("notes", ""), excerpt))
        base = _base(app_id)
        scores = {}
        for sector, keywords in SECTOR_KEYWORDS.items():
            score = 100 if base in SECTOR_APPS[sector].split() else 0
            score += min(20, 4 * sum(_matches(text, word) for word in keywords))
            if base in HR_APPS:
                score = max(score, 20)
            if base in MARKETING_APPS and sector in (
                "Information",
                "Retail Trade",
                "Wholesale Trade",
                "Professional, Scientific, and Technical Services",
            ):
                score = max(score, 30)
            if base in TRAVEL_APPS and sector in (
                "Professional, Scientific, and Technical Services",
                "Real Estate and Rental and Leasing",
            ):
                score = max(score, 20)
            if base in STANDARD_APPS or base == "google_sheets":
                score = max(score, 2)
            if base in COMMS_APPS:
                score = 1  # Regional/personal preference is not a US operating requirement.
            if base in REGIONAL_APPS:
                score = 30 if sector in ("Information", "Retail Trade", "Wholesale Trade") else 0
            scores[sector] = score
        if not any(scores.values()):
            scores["Professional, Scientific, and Technical Services"] = 1
        result[app_id] = scores
    return result


def captured_text(excerpts):
    """Only caller-supplied captures count; candidate names/reasons/URLs do not."""
    return "\n".join(
        page.get("excerpt", "")
        for page in excerpts
        if page.get("url")
        and page.get("capture_status", page.get("status")) == "captured"
        and not (page.get("capture_error") or page.get("error"))
    )


# Product names that are ordinary words or numbers: "Monday" in office hours, "12306" in a zip
# code, "Linear" in prose. Evidence for these needs the product's own spelling.
PRODUCT_SPELLINGS = {
    "monday_mock": ("monday.com",),
    "12306_mock": ("12306.cn", "China Railway 12306"),
    "linear_mock": ("linear.app", "Linear issue", "Linear workspace"),
    "notion_mock": ("notion.so", "Notion workspace", "Notion page", "Notion database"),
    "canva_mock": ("canva.com", "Canva design", "Canva Pro"),
    "asana_mock": ("asana.com", "Asana project", "Asana task"),
    "amazon_mock": ("amazon.com", "Amazon storefront", "Amazon marketplace"),
    "reddit_mock": ("reddit.com", "subreddit"),
    "discord_mock": ("discord.com", "Discord server"),
    "zoom_web_mock": ("zoom.us", "Zoom meeting", "Zoom webinar"),
    "facebook_mock": ("facebook.com", "Facebook page", "Facebook ads"),
    "instagram_mock": ("instagram.com", "Instagram account"),
    "twitter_mock": ("twitter.com", "x.com", "Twitter account"),
    "expedia_mock": ("expedia.com", "Expedia Partner"),
    "booking_com_mock": ("booking.com",),
    "uber_eats_mock": ("ubereats.com", "Uber Eats"),
    "aws_console_mock": ("AWS console", "Amazon Web Services", "aws.amazon.com"),
    "azure_mock": ("Microsoft Azure", "portal.azure.com", "Azure subscription"),
}


def mentions_app(catalogs, app_id, text):
    """Match the product as a whole phrase; ambiguous names need the product's own spelling."""
    if app_id in PRODUCT_SPELLINGS:
        return any(_matches(text, word) for word in PRODUCT_SPELLINGS[app_id])
    name = re.sub(r"\s*\(mock\)$", "", catalogs.apps[app_id]["name"], flags=re.IGNORECASE)
    return any(_matches(text, word) for word in (name, _base(app_id).replace("_", " ")))


def candidate_app_evidence(catalogs, candidate, surface=None):
    """Keep cited tool mentions; capture and semantic review still verify adoption."""
    available = set(catalogs.apps) if surface is None else set(surface) & set(catalogs.apps)
    rows = {}
    for row in getattr(candidate, "app_evidence", []):
        try:
            url = urlsplit(row.source_url)
            public = url.scheme in ("http", "https") and url.hostname and not (url.username or url.password)
        except ValueError:
            public = False
        if row.app_id in available and public and mentions_app(catalogs, row.app_id, row.quote):
            rows.setdefault(row.app_id, row)
    return list(rows.values())


def eligible_scores(catalogs, sector, surface=None, excerpts=()):
    evidence = captured_text(excerpts)
    # This deliberately requires a captured market statement, not a Chinese name.
    regional_market = any(_matches(evidence, word) for word in ("China", "Chinese market", "mainland China"))
    result = {}
    for app_id, scores in affinity_table(catalogs, surface).items():
        base = _base(app_id)
        if base in REGIONAL_APPS and not regional_market:
            continue
        score = scores.get(sector, 0)
        if base in COMMS_APPS and _matches(evidence, catalogs.apps[app_id]["name"]):
            score = max(score, 30)
        if score:
            result[app_id] = score
    return result


def assign_apps(catalogs, candidate, coverage, *, seed=0, surface=None, standard_apps=(), excerpts=()):
    """Rank eligible apps by use + in-flight reservations, affinity, seeded tie-break.

    Fewer than two are returned only when a narrowed surface has fewer eligible
    domain apps. Explicit standard bundles and the default infrastructure never compete.
    """
    scores = eligible_scores(catalogs, candidate.sector, surface, excerpts)
    excluded = set(standard_apps) | {f"{app}_mock" for app in STANDARD_APPS}
    usage = Counter(coverage.get("app_usage", coverage.get("portfolio", {}).get("app_usage", {})))
    usage.update(coverage.get("in_flight_app_usage", {}))
    # A cited adoption is worth investigating even outside the sector prior. The
    # research capture decides whether it is sourced or only inferred.
    evidenced = sorted(
        {row.app_id for row in candidate_app_evidence(catalogs, candidate, surface)}
        & set(surface_apps(catalogs, surface)) - excluded
    )
    return (
        evidenced
        + sorted(
            scores.keys() - excluded - set(evidenced),
            key=lambda app: (usage[app], -scores[app], digest([seed, candidate.id, app])),
        )[: max(0, 2 - len(evidenced))]
    )


def under_used_app_categories(catalogs, coverage, surface=None, standard_apps=()):
    """Discovery hints grouped by operating capability, with regional gates explicit."""
    usage = Counter(coverage.get("app_usage", {}))
    usage.update(coverage.get("in_flight_app_usage", {}))
    groups = {
        **SECTOR_APPS,
        "Human resources": " ".join(HR_APPS),
        "Marketing and audience operations": " ".join(MARKETING_APPS),
        "Travel coordination": " ".join(TRAVEL_APPS),
    }
    available = set(surface_apps(catalogs, surface)) - set(standard_apps)
    rows = []
    for category, products in groups.items():
        apps = available & {f"{app}_mock" for app in products.split()}
        if not apps:
            continue
        minimum = min(usage[app] for app in apps)
        rows.append(
            {
                "category": category,
                "app_ids": sorted(app for app in apps if usage[app] == minimum),
                "minimum_usage": minimum,
            }
        )
    return sorted(rows, key=lambda row: (row["minimum_usage"], row["category"]))
