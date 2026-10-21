"""Step 3: evidence. Searches several services in parallel and keeps only
results from trusted sources (the CloudLensTrustedSources table).

Providers:
    google_fact_check  Google Fact Check Tools API (key)  published fact-checks, the main source
    newsapi            NewsAPI.org (key)                   news limited to trusted domains
    guardian           The Guardian Content API (key)      Guardian reporting
    gdelt              GDELT DOC 2.0 API (no key)          news index; heavily rate-limited, best effort

Each item: {"kind": "fact_check" | "news", "provider", "domain", "publisher",
"url", "title", "rating", "summary", "claim_reviewed", "weight"}.
"""

import concurrent.futures
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT_SECONDS = 8
GDELT_TIMEOUT_SECONDS = 4
CATEGORY_WEIGHTS = {"fact_checker": 1.0, "wire_service": 1.0, "news_outlet": 0.6}
NEWS_CATEGORIES = ("wire_service", "news_outlet")
SEED_FILE = Path(__file__).with_name("trusted_sources.json")
USER_AGENT = "CloudLens/1.0 (ICSI 499 capstone project)"


class EvidenceError(Exception):
    """A search could not be completed (network, quota or API error)."""


def normalize_domain(site_or_url):
    """'https://www.Snopes.com/x' -> 'snopes.com'; 'factcheck.afp.com' stays as is."""
    value = (site_or_url or "").strip().lower()
    if "://" in value:
        value = urllib.parse.urlparse(value).hostname or ""
    value = value.split("/")[0]
    return value[4:] if value.startswith("www.") else value


def load_seed():
    """The bundled source list, keyed by domain, in the same shape as table items."""
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    return {s["domain"]: {**s, "weight": CATEGORY_WEIGHTS[s["category"]], "enabled": True} for s in data["sources"]}


class TrustedSources:
    """Trust lookup over table items keyed by domain."""

    def __init__(self, by_domain):
        self.by_domain = by_domain

    def lookup(self, domain):
        parts = (domain or "").split(".")
        for i in range(len(parts) - 1):
            entry = self.by_domain.get(".".join(parts[i:]))
            if entry is not None:
                return entry if entry.get("enabled", True) else None
        return None

    def weight(self, domain):
        entry = self.lookup(domain)
        return float(entry["weight"]) if entry else 0.0

    def name(self, domain, default=""):
        entry = self.lookup(domain)
        return entry.get("name") or default if entry else default

    def domains(self, categories):
        return sorted(d for d, e in self.by_domain.items() if e.get("enabled", True) and e.get("category") in categories)


def _http_get_json(url, timeout=TIMEOUT_SECONDS, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Never include the URL in the message: some providers carry the key in it.
        raise EvidenceError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise EvidenceError(f"request failed: {type(e).__name__}") from None


def _item(kind, provider, url, title, sources, publisher="", rating="", summary="", claim_reviewed="", domain=None):
    domain = normalize_domain(domain or url)
    return {"kind": kind, "provider": provider, "domain": domain,
            "publisher": sources.name(domain, publisher or domain), "url": url, "title": (title or "").strip(),
            "rating": rating or "", "summary": re.sub(r"<[^>]+>", "", summary or "").strip()[:400],
            "claim_reviewed": claim_reviewed or "", "weight": sources.weight(domain)}


def google_fact_check(query, key, sources, limit):
    params = urllib.parse.urlencode({"query": query, "languageCode": "en", "pageSize": 10, "key": key})
    data = _http_get_json(f"https://factchecktools.googleapis.com/v1alpha1/claims:search?{params}")
    items = []
    for claim in data.get("claims", []):
        for review in claim.get("claimReview", []):
            publisher = review.get("publisher") or {}
            items.append(_item("fact_check", "google_fact_check", review.get("url", ""), review.get("title"),
                               sources, publisher=publisher.get("name"), rating=review.get("textualRating"),
                               claim_reviewed=claim.get("text"), domain=publisher.get("site") or review.get("url")))
    return items


def newsapi(query, key, sources, limit):
    params = urllib.parse.urlencode({"q": query, "domains": ",".join(sources.domains(NEWS_CATEGORIES)),
                                     "language": "en", "sortBy": "relevancy", "pageSize": limit})
    # The key goes in a header, so it never appears in a URL or log.
    data = _http_get_json(f"https://newsapi.org/v2/everything?{params}", headers={"X-Api-Key": key})
    if data.get("status") != "ok":
        raise EvidenceError(f"NewsAPI error: {data.get('code')}")
    return [_item("news", "newsapi", a.get("url", ""), a.get("title"), sources,
                  publisher=(a.get("source") or {}).get("name"), summary=a.get("description"))
            for a in data.get("articles", [])]


def guardian(query, key, sources, limit):
    if sources.weight("theguardian.com") == 0:
        return []  # disabled in the trusted-source table
    params = urllib.parse.urlencode({"q": query, "page-size": limit, "order-by": "relevance",
                                     "show-fields": "trailText", "api-key": key})
    data = _http_get_json(f"https://content.guardianapis.com/search?{params}").get("response") or {}
    if data.get("status") != "ok":
        raise EvidenceError("Guardian API error")
    return [_item("news", "guardian", r.get("webUrl", ""), r.get("webTitle"), sources, publisher="The Guardian",
                  summary=(r.get("fields") or {}).get("trailText"))
            for r in data.get("results", [])]


def gdelt(query, key, sources, limit):
    domains = " OR ".join(f"domainis:{d}" for d in sources.domains(NEWS_CATEGORIES))
    params = urllib.parse.urlencode({"query": f"{query} ({domains}) sourcelang:english", "mode": "artlist",
                                     "format": "json", "maxrecords": limit, "sort": "hybridrel"})
    data = _http_get_json(f"https://api.gdeltproject.org/api/v2/doc/doc?{params}", timeout=GDELT_TIMEOUT_SECONDS)
    return [_item("news", "gdelt", a.get("url", ""), a.get("title"), sources, domain=a.get("domain") or a.get("url"))
            for a in data.get("articles", [])]


# (name, search function, needs a key). Google is the main source of evidence.
PROVIDERS = [("google_fact_check", google_fact_check, True), ("newsapi", newsapi, True),
             ("guardian", guardian, True), ("gdelt", gdelt, False)]
PRIMARY = "google_fact_check"


def find_evidence(query, keys, sources, limit, enabled=None):
    """Searches every usable provider in parallel.

    keys maps provider name to its API key (missing or empty means skip it).
    Returns (up to `limit` trusted items, fact-checks first, then by weight;
             {provider: "ok" | "failed" | "skipped"}).
    """
    report, futures = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
        for name, search, needs_key in PROVIDERS:
            if (enabled is not None and name not in enabled) or (needs_key and not keys.get(name)):
                report[name] = "skipped"
                continue
            futures[pool.submit(search, query, keys.get(name), sources, limit)] = name
        found = []
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                found.extend(future.result())
                report[name] = "ok"
            except EvidenceError as e:
                print(f"evidence provider {name} failed: {e}")
                report[name] = "failed"
    items, seen = [], set()
    for item in found:
        if item["url"] and item["url"] not in seen and item["weight"] > 0:
            seen.add(item["url"])
            item["relevance"] = relevance(query, item)
            # News search matches loosely; skip articles that share few of the claim's words.
            if item["kind"] == "fact_check" or item["relevance"] >= MIN_RELEVANCE:
                items.append(item)
    order = {name: i for i, (name, _, _) in enumerate(PROVIDERS)}
    items.sort(key=lambda i: (i["kind"] != "fact_check", -round(i["relevance"], 1), -i["weight"],
                              order[i["provider"]]))
    return items[:limit], report


MIN_RELEVANCE = 0.4
_WORD = re.compile(r"[a-z0-9%$]+")
_COMMON = frozenset("the and for with that this are was were has have had from but not you its our their".split())


def _keywords(text):
    words = _WORD.findall(re.sub(r"['’]s\b", "", (text or "").lower()))
    # Crude stemming so "ballots" matches "ballot".
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w
            for w in words if (len(w) > 2 or w.isdigit()) and w not in _COMMON}


def relevance(query, item):
    """Share of the query's keywords that appear in the item (0 to 1)."""
    wanted = _keywords(query)
    if not wanted:
        return 0.0
    found = _keywords(" ".join((item["title"], item["summary"], item["claim_reviewed"])))
    return len(wanted & found) / len(wanted)


# Rule-based stances, used only when Claude is unavailable. Besides Claude's
# SUPPORTS / REFUTES / UNRELATED they use MIXED (a fact-check with an in-between
# rating) and RELATED (on topic, but no clear side). Only SUPPORTS and REFUTES count.
_MIXED = re.compile(r"half|mix|partly|partially|unproven|unverified|missing context|lacks context|satire|outdated|"
                    r"no evidence either|disputed", re.I)
_REFUTES = re.compile(r"false|fake|pants on fire|incorrect|inaccurate|misleading|fabricated|hoax|scam|not true|"
                      r"wrong|debunk|baseless|no evidence|altered|manipulated|doctored|untrue|myth", re.I)
_SUPPORTS = re.compile(r"\btrue\b|correct|accurate|confirmed|\breal\b|legit", re.I)
# Words that mean a news story questions the claim instead of reporting it as fact.
_DOUBT = re.compile(r"\b(false|fake|hoax|debunk\w*|myths?|rumou?rs?|conspirac\w*|misleading|misinformation|"
                    r"disinformation|no evidence|viral|claims?|claimed|alleged\w*|denie[sd]|deny|not|isn't|fact.?check\w*)\b",
                    re.I)
MIN_FACT_CHECK_RELEVANCE = 0.25
NEWS_SUPPORT_RELEVANCE = 0.7


def fallback_stance(item):
    """Stance from a fact-check's rating, or from how closely a news story matches the claim."""
    relevance = item.get("relevance", 1.0)
    if item["kind"] == "fact_check":
        rating = item.get("rating", "")
        if relevance < MIN_FACT_CHECK_RELEVANCE:
            return "UNRELATED"
        if not rating:
            return "RELATED"
        if _MIXED.search(rating):
            return "MIXED"
        if _REFUTES.search(rating):
            return "REFUTES"
        if _SUPPORTS.search(rating):
            return "SUPPORTS"
        return "RELATED"
    # Trusted outlets reporting the same thing as plain news back the claim up.
    if relevance >= NEWS_SUPPORT_RELEVANCE and not _DOUBT.search(f"{item['title']} {item['summary']}"):
        return "SUPPORTS"
    return "RELATED" if relevance >= MIN_RELEVANCE else "UNRELATED"


def summarize(items, stances):
    """A plain one-line reason when Claude is unavailable."""
    refutes = sum(1 for s in stances if s == "REFUTES")
    supports = sum(1 for s in stances if s == "SUPPORTS")
    others = sum(1 for s in stances if s in ("MIXED", "RELATED"))
    if supports and refutes:
        return "Trusted sources disagree about this claim."
    if refutes:
        return f"{refutes} trusted fact-check{'s' if refutes > 1 else ''} rated this claim false or misleading."
    if supports:
        return f"{supports} trusted source{'s' if supports > 1 else ''} back{'' if supports > 1 else 's'} up this claim."
    if others:
        return (f"{others} trusted source{'s' if others > 1 else ''} discuss{'' if others > 1 else 'es'} this topic "
                f"but {'do' if others > 1 else 'does'} not clearly confirm or refute the claim.")
    return ""
