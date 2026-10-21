"""Step 3: evidence. Searches several services in parallel and keeps only
results from trusted sources (the CloudLensTrustedSources table).

Providers:
    google_fact_check  Google Fact Check Tools API (key)  rated fact-checks, the main source
    google_news        Google News RSS (no key)            news and fact-check articles from many outlets
    bing_news          Bing News RSS (no key)              the same from Bing's index, with summaries
    newsapi            NewsAPI.org (key, optional)         news limited to trusted domains
    guardian           The Guardian Content API (key, optional)

Each item: {"kind": "fact_check" | "news", "provider", "domain", "publisher",
"url", "title", "rating", "summary", "claim_reviewed", "weight"}. Articles
from fact-checking sites, or headlined "Fact check", count as fact-checks.
"""

import concurrent.futures
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

TIMEOUT_SECONDS = 8
FEED_ITEMS = 30  # news feed entries read per search; only trusted ones are kept
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

    def category(self, domain):
        entry = self.lookup(domain)
        return entry.get("category", "") if entry else ""

    def domain_named(self, name):
        """The domain of a source by its name ("Snopes" -> "snopes.com"), or None."""
        name = (name or "").strip().lower()
        return next((d for d, e in self.by_domain.items() if (e.get("name") or "").lower() == name), None)


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


def _http_get_xml(url, timeout=TIMEOUT_SECONDS):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return ET.fromstring(response.read())
    except urllib.error.HTTPError as e:
        raise EvidenceError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ET.ParseError) as e:
        raise EvidenceError(f"request failed: {type(e).__name__}") from None


def _run_all(search, queries):
    """Runs search(q) for every query in parallel; returns the results that worked."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(queries)) as pool:
        futures = [pool.submit(search, q) for q in queries]
    results, errors = [], []
    for future in futures:
        try:
            results.append(future.result())
        except EvidenceError as e:
            errors.append(e)
    if not results:
        raise errors[0]
    return results


def _item(kind, provider, url, title, sources, publisher="", rating="", summary="", claim_reviewed="", domain=None):
    domain = normalize_domain(domain or url)
    return {"kind": kind, "provider": provider, "domain": domain,
            "publisher": sources.name(domain, publisher or domain), "url": url, "title": (title or "").strip(),
            "rating": rating or "", "summary": re.sub(r"<[^>]+>", "", summary or "").strip()[:400],
            "claim_reviewed": claim_reviewed or "", "weight": sources.weight(domain)}


SHORT_QUERY_WORDS = 4


def short_query(query):
    """The most distinctive words: names and numbers first, then the longest words, in their original order."""
    words = query.split()
    ranked = sorted(range(len(words)),
                    key=lambda i: (not (words[i][:1].isupper() or any(c.isdigit() for c in words[i])), -len(words[i])))
    return " ".join(words[i] for i in sorted(ranked[:SHORT_QUERY_WORDS]))


def google_fact_check(queries, key, sources, limit):
    """queries: one search, or several phrasings of it; their results are merged."""
    def search(q):
        params = urllib.parse.urlencode({"query": q, "languageCode": "en", "pageSize": 10, "key": key})
        return _http_get_json(f"https://factchecktools.googleapis.com/v1alpha1/claims:search?{params}")

    queries = [queries] if isinstance(queries, str) else list(queries)
    # The search is picky about wording and its results vary between calls, so
    # several phrasings run together, the last being the few most distinctive words.
    results = _run_all(search, list(dict.fromkeys(q for q in queries + [short_query(queries[0])] if q)))
    print(f"fact-check searches: {[len(r.get('claims', [])) for r in results]} results")
    items = []
    for claim in (c for r in results for c in r.get("claims", [])):
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


_FACT_CHECK_HEADLINE = re.compile(r"\bfact[ -]?check", re.I)


def _feed_item(provider, url, title, sources, publisher, domain, summary=""):
    """A news feed entry; articles by fact-checkers or headlined "Fact check" count as fact-checks."""
    domain = normalize_domain(domain or url)
    is_fact_check = sources.category(domain) == "fact_checker" or _FACT_CHECK_HEADLINE.search(title or "")
    return _item("fact_check" if is_fact_check else "news", provider, url, title, sources, publisher=publisher,
                 summary=summary, domain=domain)


def google_news(query, key, sources, limit):
    def search(q):
        params = urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        return _http_get_xml(f"https://news.google.com/rss/search?{params}")

    items = []
    for root in _run_all(search, [query, f"{query} fact check"]):
        for entry in root.findall("./channel/item")[:FEED_ITEMS]:
            source = entry.find("source")
            publisher = (source.text or "") if source is not None else ""
            # Headlines end with " - Publisher".
            title = entry.findtext("title", "").removesuffix(f" - {publisher}")
            items.append(_feed_item("google_news", entry.findtext("link", ""), title, sources, publisher,
                                    source.get("url", "") if source is not None else ""))
    return items


def bing_news(query, key, sources, limit):
    def search(q):
        params = urllib.parse.urlencode({"q": q, "format": "rss", "mkt": "en-US"})
        return _http_get_xml(f"https://www.bing.com/news/search?{params}")

    items = []
    for root in _run_all(search, [query, f"{query} fact check"]):
        for entry in root.findall("./channel/item")[:FEED_ITEMS]:
            link = entry.findtext("link", "")
            # Links go through a Bing redirect that carries the article address.
            url = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
            publisher = next((c.text or "" for c in entry if c.tag.endswith("Source")), "")
            domain = normalize_domain(url)
            if publisher.endswith(" on MSN"):  # a syndicated copy: credit the original publisher
                publisher = publisher.removesuffix(" on MSN")
                domain = sources.domain_named(publisher) or domain
            items.append(_feed_item("bing_news", url, entry.findtext("title", ""), sources, publisher, domain,
                                    summary=entry.findtext("description", "")))
    return items


# (name, search function, needs a key). Google Fact Check is the main source of evidence.
PROVIDERS = [("google_fact_check", google_fact_check, True), ("google_news", google_news, False),
             ("bing_news", bing_news, False), ("newsapi", newsapi, True), ("guardian", guardian, True)]
PRIMARY = "google_fact_check"


def find_evidence(query, keys, sources, limit, enabled=None, also_try=()):
    """Searches every usable provider in parallel.

    keys maps provider name to its API key (missing or empty means skip it).
    also_try: other phrasings for the fact-check search when `query` finds nothing.
    Returns (up to `limit` trusted items, fact-checks first, then by weight;
             {provider: "ok" | "failed" | "skipped"}).
    """
    report, futures = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
        for name, search, needs_key in PROVIDERS:
            if (enabled is not None and name not in enabled) or (needs_key and not keys.get(name)):
                report[name] = "skipped"
                continue
            queries = [query, *also_try] if name == PRIMARY else query
            futures[pool.submit(search, queries, keys.get(name), sources, limit)] = name
        found = []
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                found.extend(future.result())
                report[name] = "ok"
            except EvidenceError as e:
                print(f"evidence provider {name} failed: {e}")
                report[name] = "failed"
    unique = {}
    for item in found:
        if not item["url"] or item["weight"] <= 0:
            continue
        # The same article can come from several providers under different links;
        # keep one copy with whatever details (rating, summary) any of them had.
        title = re.sub(r"\W+", " ", item["title"].lower()).strip()[:80]
        key = (item["domain"], title) if title else item["url"]
        if key in unique:
            kept = unique[key]
            for field in ("rating", "summary", "claim_reviewed"):
                kept[field] = kept[field] or item[field]
        else:
            unique[key] = item
    items, names = [], _names(query)
    for item in unique.values():
        item["relevance"] = relevance(query, item)
        item["core_relevance"] = relevance(short_query(query), item)
        item["names_found"] = relevance(names, item) if names else 1.0
        # News search matches loosely; skip articles that share few of the claim's words.
        # Fact-checks get a lower bar: Claude, or the stance rules, decide whether they fit.
        if item["relevance"] >= (MIN_RELEVANCE / 2 if item["kind"] == "fact_check" else MIN_RELEVANCE):
            items.append(item)
    order = {name: i for i, (name, _, _) in enumerate(PROVIDERS)}
    items.sort(key=lambda i: (i["kind"] != "fact_check", -round(i["relevance"], 1), -i["weight"],
                              order[i["provider"]]))
    # Prefer different sites: a site's second article only gets a slot after every site has one.
    firsts, repeats, sites = [], [], set()
    for item in items:
        (repeats if item["domain"] in sites else firsts).append(item)
        sites.add(item["domain"])
    return (firsts + repeats)[:limit], report


MIN_RELEVANCE = 0.4
_WORD = re.compile(r"[a-z0-9%$]+")
_COMMON = frozenset("the and for with that this are was were has have had from but not you its our their".split())


# Different names fact-checkers use for the same thing.
_SYNONYMS = {"coronavirus": "covid", "sars": "covid", "us": "usa", "america": "usa", "american": "usa"}


def _stem(word):
    """Crude stemming so "ballots", "spreading", "banned" and "approves" match "ballot", "spread", "ban"
    and "approved"."""
    for suffix in ("ing", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[:-len(suffix)]
            if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiouls":
                word = word[:-1]  # "bann" -> "ban"
            break
    return word[:-1] if len(word) > 4 and word.endswith("e") else word


_SHORT_WORDS = {"uk", "eu", "un", "ai", "usa"}  # two-letter words that are names


def _keywords(text):
    words = _WORD.findall(re.sub(r"['’]s\b", "", (text or "").lower()))
    words = (_SYNONYMS.get(w, w) for w in words)
    # Other short words count only when they carry a digit, like "5g".
    return {_SYNONYMS.get(_stem(w), _stem(w)) for w in words
            if (len(w) > 2 or w in _SHORT_WORDS or any(c.isdigit() for c in w)) and w not in _COMMON}


def _names(query):
    """Capitalized words after the first: places, people and organizations ("Ohio", "Stephen Hubbard").
    Empty for all-caps text, where capitals say nothing."""
    words = query.split()
    if query.isupper():
        return ""
    return " ".join(w for w in words[1:] if w[:1].isupper())


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
_MIXED = re.compile(r"half|mix|partly|partially|unproven|unverified|outdated|exaggerat|no evidence either|disputed",
                    re.I)
# Fact-checkers rate claims that are satire, out of context or never happened as not true as stated.
_REFUTES = re.compile(r"false|fake|pants on fire|incorrect|inaccurate|misleading|fabricated|hoax|scam|not true|"
                      r"wrong|debunk|baseless|no evidence|altered|manipulated|doctored|untrue|myth|"
                      r"did ?n[o']t happen|never happened|not real|unfounded|unsupported|satire|parody|"
                      r"missing context|lacks context|out of context|false context|miscaptioned|misattributed|"
                      r"imposter|pinocchio|spoof|not connected|no connection|no link|not linked", re.I)
_SUPPORTS = re.compile(r"\btrue\b|correct|accurate|confirmed|\breal\b|legit", re.I)
# Fact-check headlines that say the claim is wrong: "Spotify Did NOT Remove...", "No, ...".
_TITLE_REFUTES = re.compile(r"\b(did not|didn't|does not|doesn't|is not|isn't|was not|wasn't|has not|hasn't|"
                            r"have not|haven't|are not|aren't|never|no evidence|false\w*|fake|hoax|debunk\w*|myth|"
                            r"misleading|fabricated|altered|doctored|satire|scam|baseless|unfounded|misrepresent\w*|"
                            r"wrong|incorrect|inaccurate|untrue|"
                            r"out of context|not real|not true)\b|^(fact[ -]?check:?\s*)?no\b", re.I)
_TITLE_SUPPORTS = re.compile(r"^(fact[ -]?check:?\s*)?(yes|true)\b|\b(is|are|was) (true|real|accurate|correct)\b", re.I)
# Words that mean a news story questions the claim instead of reporting it as fact.
_DOUBT = re.compile(r"\b(false|fake|hoax|debunk\w*|myths?|rumou?rs?|conspirac\w*|misleading|misinformation|"
                    r"disinformation|no evidence|viral|claims?|claimed|alleged\w*|denie[sd]|deny|not|isn't|fact.?check\w*)\b",
                    re.I)
MIN_FACT_CHECK_RELEVANCE = 0.5
NEWS_SUPPORT_RELEVANCE = 0.7
CORE_SUPPORT_RELEVANCE = 0.75  # share of the claim's few most distinctive words


def fallback_stance(item):
    """Stance from a fact-check's rating, or from how closely a news story matches the claim."""
    relevance = item.get("relevance", 1.0)
    if item["kind"] == "fact_check":
        rating = item.get("rating", "")
        if relevance < MIN_FACT_CHECK_RELEVANCE:
            return "UNRELATED"
        if _MIXED.search(rating):
            return "MIXED"
        if _REFUTES.search(rating):
            return "REFUTES"
        if _SUPPORTS.search(rating):
            return "SUPPORTS"
        # No clear rating: a fact-check's headline usually states its finding, and
        # when the headline is a question ("Did Spotify remove...?") the summary does.
        title = item.get("title", "")
        finding = f"{title} {item.get('summary', '')}" if title.rstrip().endswith("?") else title
        finding = finding.replace("’", "'")  # "hasn’t" -> "hasn't"
        if _TITLE_REFUTES.search(finding):
            return "REFUTES"
        return "SUPPORTS" if _TITLE_SUPPORTS.search(finding) else "RELATED"
    # Trusted outlets reporting the same thing as plain news back the claim up.
    # Headlines are short, so a story that has most of the claim's core words counts too.
    matches = relevance >= NEWS_SUPPORT_RELEVANCE or (
        relevance >= MIN_RELEVANCE + 0.1 and item.get("core_relevance", 0) >= CORE_SUPPORT_RELEVANCE)
    # ...and it must be about the same people and places.
    same_names = item.get("names_found", 1.0) >= 0.5
    if matches and same_names and not _DOUBT.search(f"{item['title']} {item['summary']}"):
        return "SUPPORTS"
    return "RELATED" if relevance >= MIN_RELEVANCE else "UNRELATED"


def fallback_stances(items):
    """fallback_stance for every item. Published fact-checks outrank keyword matching:
    once one refutes the claim, news stories no longer count as support."""
    stances = [fallback_stance(i) for i in items]
    if any(s == "REFUTES" and i["kind"] == "fact_check" for i, s in zip(items, stances)):
        stances = ["RELATED" if s == "SUPPORTS" and i["kind"] == "news" else s for i, s in zip(items, stances)]
    return stances


def summarize(items, stances):
    """A plain one-line reason when Claude is unavailable."""
    refutes = sum(1 for s in stances if s == "REFUTES")
    supports = sum(1 for s in stances if s == "SUPPORTS")
    mixed = sum(1 for s in stances if s == "MIXED")
    others = sum(1 for s in stances if s == "RELATED")
    if supports and refutes:
        return (f"{refutes} trusted source{'s' if refutes > 1 else ''} rated this claim false, while "
                f"{supports} other{'s' if supports > 1 else ''} seem{'' if supports > 1 else 's'} to back it up.")
    if refutes:
        return f"{refutes} trusted fact-check{'s' if refutes > 1 else ''} rated this claim false or misleading."
    if supports:
        return f"{supports} trusted source{'s' if supports > 1 else ''} back{'' if supports > 1 else 's'} up this claim."
    if mixed:
        return f"{mixed} trusted fact-check{'s' if mixed > 1 else ''} rated this claim partly true."
    if others:
        return (f"{others} trusted source{'s' if others > 1 else ''} discuss{'' if others > 1 else 'es'} this topic "
                f"but {'do' if others > 1 else 'does'} not clearly confirm or refute the claim.")
    return ""
