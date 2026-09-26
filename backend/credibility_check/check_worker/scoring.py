"""Credibility score and verdict (Milestone 2 slide 7, Milestone 4 FR 1.5).

Each piece of evidence is {"domain": str, "weight": float, "stance": str}, where
weight is 1.0 for fact-checkers and wire services, 0.6 for established news
outlets and 0 for unknown sites, and stance is SUPPORTS, REFUTES or UNRELATED.
"""

TRUE, FALSE, UNVERIFIED = "TRUE", "FALSE", "UNVERIFIED"
MIN_INDEPENDENT_SOURCES = 2


def score_evidence(evidence, t_true=70, t_false=30):
    """Returns {"verdict", "score", "supporters", "refuters"}; score is None without evidence."""
    trusted = [e for e in evidence if e.get("weight", 0) > 0]
    s = sum(e["weight"] for e in trusted if e["stance"] == "SUPPORTS")
    r = sum(e["weight"] for e in trusted if e["stance"] == "REFUTES")
    # Independence: several articles from one site count as one source.
    supporters = len({e["domain"] for e in trusted if e["stance"] == "SUPPORTS"})
    refuters = len({e["domain"] for e in trusted if e["stance"] == "REFUTES"})
    result = {"verdict": UNVERIFIED, "score": None, "supporters": supporters, "refuters": refuters}
    if s + r == 0:
        return result
    score = round(100 * s / (s + r))
    result["score"] = score
    if score >= t_true and supporters >= MIN_INDEPENDENT_SOURCES:
        result["verdict"] = TRUE
    elif score <= t_false and refuters >= MIN_INDEPENDENT_SOURCES:
        result["verdict"] = FALSE
    return result
