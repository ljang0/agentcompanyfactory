"""Explicit company identity matching for discovery and exclusions."""

import re
import unicodedata
from urllib.parse import urlsplit


def identity_keys(company):
    name = unicodedata.normalize("NFKC", company["real_firm"]).casefold().replace("&", " and ")
    # Normalize only explicit legal suffixes, not business words (e.g. "Group")
    # or parent/subsidiary relationships that need evidence rather than heuristics.
    name = re.sub(r"[^\w\s]", " ", name)
    name = re.sub(r"(?:\s+(?:incorporated|inc|corporation|corp|llc|limited|ltd|plc))+$", "", name.strip())
    name = re.sub(r"[^\w]", "", name)
    url = company.get("website", "")
    host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").casefold()
    host = host.removeprefix("www.").rstrip(".")
    return {"id": company.get("id", ""), "name": name, "host": host}


def overlap(candidate, excluded):
    keys = identity_keys(candidate)
    return [
        old["real_firm"]
        for old in excluded
        for old_keys in [identity_keys(old)]
        if any(value and value == old_keys[key] for key, value in keys.items())
    ]
