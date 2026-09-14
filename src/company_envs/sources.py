"""Capture actual public pages; source failures remain visible evidence gaps."""

import io
import ipaddress
import re
import socket
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .storage import digest, now, read, write


def normalize(text):
    typography = {ord(c): "-" for c in "‐‑‒–—−"}
    typography.update(
        {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"', ord("\u00ad"): None, ord("\u200b"): None}
    )
    return " ".join(unicodedata.normalize("NFKC", text).translate(typography).casefold().split())


def public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source must be a public HTTP(S) URL without credentials")
    for entry in socket.getaddrinfo(parsed.hostname, parsed.port or 443):
        if not ipaddress.ip_address(entry[4][0]).is_global:
            raise ValueError("private-network URLs are not research sources")


class Sources:
    def __init__(self, root, *, cache_path=None):
        self.path = Path(cache_path) if cache_path is not None else Path(root) / "data" / "sources"

    def capture(self, url):
        key = digest(url)
        path = self.path / f"{key}.json"
        if path.exists():
            cached = read(path)
            if cached.get("status") == "captured" and digest(cached.get("text", "")) == cached.get(
                "text_hash"
            ):
                return cached
        record = {
            "url": url,
            "captured_at": now(),
            "status": "unreadable",
            "text": "",
            "text_hash": "",
            "error": "",
        }
        try:
            current = url
            for _ in range(6):
                public_url(current)
                with requests.get(
                    current,
                    timeout=(10, 35),
                    allow_redirects=False,
                    stream=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; CompanyEnvsResearch/0.1)"},
                ) as response:
                    if response.is_redirect:
                        current = urljoin(current, response.headers["Location"])
                        continue
                    response.raise_for_status()
                    parts, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 20_000_000:
                            raise ValueError("source exceeds 20 MB capture limit")
                        parts.append(chunk)
                    body = b"".join(parts)
                    if body.startswith(b"%PDF"):
                        text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(body)).pages)
                    else:
                        soup = BeautifulSoup(body, "html.parser")
                        for tag in soup(["script", "style", "noscript"]):
                            tag.decompose()
                        text = soup.get_text(" ", strip=True)
                    if len(text.strip()) < 80 or re.search(
                        r"(?i)^(just a moment|access denied|attention required)", text
                    ):
                        raise ValueError("empty page or access challenge")
                    record.update(status="captured", resolved_url=current, text=text, text_hash=digest(text))
                    break
            else:
                raise ValueError("too many redirects")
        except Exception as exc:  # noqa: BLE001 -- third-party decoders fail as explicit evidence gaps
            record["error"] = f"{type(exc).__name__}: {exc}"
        write(path, record)
        return record

    def evidence(self, company):
        """Capture claims and demote unavailable sources in the supplied dossier.

        Callers persist the normalized dossier alongside this evidence manifest;
        source_url remains the audit locator for inferred attempted sources.
        """
        pages = {
            url: self.capture(url)
            for url in dict.fromkeys(e.source_url for e in company.evidence if e.source_url)
        }
        result = []
        for claim in company.evidence:
            page = pages.get(claim.source_url, {})
            capture_status = page.get("status", "not_attempted")
            if claim.kind == "sourced" and claim.source_url and capture_status != "captured":
                # Keep the URL in the schema's existing source_url field so this
                # inference and its audit trail survive dossier serialization.
                claim.kind = "inferred"
                claim.basis = " ".join(
                    part
                    for part in (
                        claim.basis.strip(),
                        (
                            f"Unverified inference; attempted source {claim.source_url} could not be captured "
                            f"({capture_status}: {page.get('error', '')}). This is not captured evidence."
                        ),
                    )
                    if part
                )
            quote = normalize(claim.quote)
            supported = bool(quote) and quote in normalize(page.get("text", ""))
            result.append(
                {
                    "claim_id": claim.id,
                    "kind": claim.kind,
                    "source_url": claim.source_url,
                    "status": "supported"
                    if page.get("status") == "captured" and supported
                    else "unverified"
                    if claim.kind == "sourced" or (capture_status == "captured" and quote)
                    else "declared",
                    "text_hash": page.get("text_hash", ""),
                    "quote": claim.quote,
                    "capture_error": page.get("error", ""),
                    "capture_status": capture_status,
                    **(
                        {"attempted_source_url": claim.source_url}
                        if claim.kind == "inferred" and claim.source_url
                        else {}
                    ),
                }
            )
        return result

    PAGE_BUDGET = 40_000
    TOTAL_BUDGET = 360_000
    QUOTE_WINDOW = 2_500

    @classmethod
    def bounded_text(cls, text, quotes, budget):
        """The page head plus a window around every quote, within ``budget`` characters.

        Codex rejects inputs above about one million characters, and a single long PDF
        can exceed that alone. Reviewers still see every quoted passage in context.
        """
        if len(text) <= budget:
            return text, False
        needles = {normalize(q) for q in quotes if q and normalize(q)}
        spans = set()
        if needles:
            # Normalization changes offsets (whitespace, typography, casefold
            # expansions). Map matches back to the captured source bytes' text.
            chars, offsets, cache = [], [], {}
            for index, char in enumerate(text):
                if char not in cache:
                    cache[char] = " " if char.isspace() else normalize(char)
                for folded in cache[char]:
                    if folded == " " and (not chars or chars[-1] == " "):
                        continue
                    chars.append(folded)
                    offsets.append(index)
            searchable = "".join(chars)
            for needle in needles:
                at = searchable.find(needle)
                while at >= 0:
                    spans.add((offsets[at], offsets[at + len(needle) - 1] + 1))
                    at = searchable.find(needle, at + 1)

        def windows(radius):
            merged = []
            for start, end in sorted(spans):
                start, end = max(0, start - radius), min(len(text), end + radius)
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            return "\n[...]\n".join(text[start:end] for start, end in merged)

        marker = "\n[... captured text truncated for length ...]\n"
        radius = cls.QUOTE_WINDOW
        excerpts = windows(radius)
        # Reduce context before dropping quoted text when windows exceed budget.
        while radius and len(excerpts) + len(marker) > budget:
            radius //= 2
            excerpts = windows(radius)
        head_budget = max(0, budget - len(excerpts) - len(marker))
        return (text[:head_budget] + marker + excerpts)[: max(0, budget)], True

    def excerpts(self, company, legacy=False):
        result = []
        if not legacy:
            # Review must see the captured support, not just a window around one
            # quote that can hide other facts in the same sourced claim. Dedup
            # by URL so shared pages/PDFs are supplied once rather than per claim,
            # and bound the total so the review prompt stays under the provider limit.
            grouped = {}
            quotes_by_url = {}
            for claim in company.evidence:
                path = self.path / f"{digest(claim.source_url)}.json"
                if not claim.source_url or not path.exists():
                    continue
                quotes_by_url.setdefault(claim.source_url, []).append(getattr(claim, "quote", "") or "")
                if claim.source_url not in grouped:
                    page = read(path)
                    grouped[claim.source_url] = {
                        "claim_ids": [],
                        "url": claim.source_url,
                        "excerpt": page.get("text", ""),
                        "scope": "full_captured_text",
                        "text_hash": page.get("text_hash", ""),
                        "capture_status": page.get("status", "unknown"),
                        "capture_error": page.get("error", ""),
                        "captured_chars": len(page.get("text", "")),
                    }
                grouped[claim.source_url]["claim_ids"].append(claim.id)
            rows = list(grouped.values())
            per_page = min(self.PAGE_BUDGET, max(5_000, self.TOTAL_BUDGET // max(1, len(rows))))
            for row in rows:
                text, truncated = self.bounded_text(
                    row["excerpt"], quotes_by_url.get(row["url"], []), per_page
                )
                row["excerpt"] = text
                if truncated:
                    row["scope"] = "captured_text_bounded_quotes_in_context"
            return rows
        for claim in company.evidence:
            path = self.path / f"{digest(claim.source_url)}.json"
            if not claim.source_url or not path.exists():
                continue
            page = read(path)
            text = page.get("text", "")
            at = text.casefold().find(claim.quote.casefold()) if claim.quote else -1
            if at >= 0:
                excerpt = text[max(0, at - 1200) : at + len(claim.quote) + 1800]
            elif not legacy and normalize(claim.quote):
                normalized = normalize(text)
                needle = normalize(claim.quote)
                at = normalized.find(needle)
                excerpt = normalized[max(0, at - 1200) : at + len(needle) + 1800] if at >= 0 else text[:5000]
            else:
                excerpt = text[:5000]
            result.append({"claim_id": claim.id, "url": claim.source_url, "excerpt": excerpt})
        return result
