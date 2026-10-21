"""Step 3: evidence. Finds published fact-checks of a claim with the Google Fact
Check Tools API and keeps only reviews from trusted sources.

Each returned item: {"domain", "publisher", "url", "title", "rating",
"claim_reviewed", "weight"}. Weights come from trusted_sources.json.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FACT_CHECK_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
TIMEOUT_SECONDS = 8

_SOURCES = json.loads((Path(__file__).with_name("trusted_sources.json")).read_text(encoding="utf-8"))
_WEIGHTS = {d: 1.0 for d in _SOURCES["fact_checkers_and_wire_services"]}
_WEIGHTS.update({d: 0.6 for d in _SOURCES["established_news"]})


class EvidenceError(Exception):
    """The search could not be completed (network, quota or API error)."""


def normalize_domain(site_or_url):
    """'https://www.Snopes.com/x' -> 'snopes.com'; 'factcheck.afp.com' stays as is."""
    value = (site_or_url or "").strip().lower()
    if "://" in value:
        value = urllib.parse.urlparse(value).hostname or ""
    value = value.split("/")[0]
    return value[4:] if value.startswith("www.") else value


def weight_for(domain):
    """Trust weight for a domain, also matching it as a subdomain of a listed site."""
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in _WEIGHTS:
            return _WEIGHTS[candidate]
    return 0.0


def _http_get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Never include the URL in the message: it carries the API key.
        raise EvidenceError(f"Fact Check API returned HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise EvidenceError(f"Fact Check API request failed: {type(e).__name__}") from None


def search_fact_checks(query, api_key, max_results):
    """Up to max_results trusted fact-check reviews for the query, best first."""
    params = urllib.parse.urlencode({"query": query, "languageCode": "en", "pageSize": 10, "key": api_key})
    data = _http_get_json(f"{FACT_CHECK_URL}?{params}")
    items, seen = [], set()
    for claim in data.get("claims", []):
        for review in claim.get("claimReview", []):
            url = review.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            publisher = review.get("publisher") or {}
            domain = normalize_domain(publisher.get("site") or url)
            weight = weight_for(domain)
            if weight == 0:
                continue  # unknown sites are ignored (Milestone 2 scoring rules)
            items.append({
                "domain": domain,
                "publisher": publisher.get("name") or domain,
                "url": url,
                "title": review.get("title", ""),
                "rating": review.get("textualRating", ""),
                "claim_reviewed": claim.get("text", ""),
                "weight": weight,
            })
    # Fact-checkers and wire services first, keeping the API's relevance order within each group.
    items.sort(key=lambda item: -item["weight"])
    return items[:max_results]
