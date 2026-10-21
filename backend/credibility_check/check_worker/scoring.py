"""Credibility score and verdict (Milestone 2 slide 7, Milestone 4 FR 1.5).

Each piece of evidence is {"domain", "weight", "stance", "kind", "relevance"}:
weight is 1.0 for fact-checkers and wire services, 0.6 for established news
outlets and 0 for unknown sites; stance is SUPPORTS, REFUTES, MIXED or anything
else (ignored); kind is "fact_check" or "news"; relevance is the share of the
claim's keywords the source matches.

Score = 100 * S / (S + R), where S and R add up the weights of supporting and
refuting sources (a MIXED rating adds half to each), or 50 with no evidence.
TRUE needs a score of at least 70, FALSE at most 30, and either 2 independent
sites that agree or one published fact-check of this same claim.
"""

TRUE, FALSE, UNVERIFIED = "TRUE", "FALSE", "UNVERIFIED"
MIN_INDEPENDENT_SOURCES = 2
NEUTRAL_SCORE = 50  # no evidence either way
DECISIVE_RELEVANCE = 0.6  # share of the claim's keywords a fact-check must match to decide alone


def score_evidence(evidence, t_true=70, t_false=30, min_sources=MIN_INDEPENDENT_SOURCES):
    """Returns {"verdict", "score", "supporters", "refuters"}; score is 50 without evidence."""
    trusted = [e for e in evidence if e.get("weight", 0) > 0]
    # A mixed rating ("Half True") counts half for and half against.
    m = sum(e["weight"] for e in trusted if e["stance"] == "MIXED") / 2
    s = sum(e["weight"] for e in trusted if e["stance"] == "SUPPORTS") + m
    r = sum(e["weight"] for e in trusted if e["stance"] == "REFUTES") + m
    # Independence: several articles from one site count as one source.
    supporters = len({e["domain"] for e in trusted if e["stance"] == "SUPPORTS"})
    refuters = len({e["domain"] for e in trusted if e["stance"] == "REFUTES"})
    result = {"verdict": UNVERIFIED, "score": NEUTRAL_SCORE, "supporters": supporters, "refuters": refuters}
    if s + r == 0:
        return result
    score = round(100 * s / (s + r))
    result["score"] = score

    def fact_check_says(stance):
        # A published fact-check of this same claim is a verdict on its own.
        return any(e["stance"] == stance and e.get("kind") == "fact_check"
                   and e.get("relevance", 0) >= DECISIVE_RELEVANCE for e in trusted)

    if score >= t_true and (supporters >= min_sources or fact_check_says("SUPPORTS")):
        result["verdict"] = TRUE
    elif score <= t_false and (refuters >= min_sources or fact_check_says("REFUTES")):
        result["verdict"] = FALSE
    return result
