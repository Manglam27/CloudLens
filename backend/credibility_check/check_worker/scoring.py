"""Credibility score and verdict (Milestone 2 slide 7, Milestone 4 FR 1.5).

Each piece of evidence is {"domain", "weight", "stance", "kind", "relevance"}:
weight is 1.0 for fact-checkers and wire services, 0.6 for established news
outlets and encyclopedias, and 0 for unknown sites; stance is SUPPORTS,
REFUTES, MIXED or anything else (ignored); kind is "fact_check", "reference" or
"news"; relevance is the share of the claim's keywords the source matches.

Balance = 100 * S / (S + R), where S and R add up the weights of supporting and
refuting sources (a MIXED rating adds half to each), or 50 with no evidence.

Verdict:
    TRUE        balance >= 70, and either 2 independent sites support the claim
                or one published fact-check or encyclopedia entry states it
    FALSE       balance <= 30, and either 2 independent sites refute it or one
                published fact-check of this same claim does
    UNVERIFIED  everything else

Score shown to the user: the balance for TRUE and FALSE. For UNVERIFIED it is
squeezed into 35-65, so a score never looks decided when the verdict is not:
above 50 the evidence leans true, below 50 it leans false.
"""

TRUE, FALSE, UNVERIFIED = "TRUE", "FALSE", "UNVERIFIED"
MIN_INDEPENDENT_SOURCES = 2
NEUTRAL_SCORE = 50  # no evidence either way
UNVERIFIED_SPREAD = 15  # an Unverified score stays within 50 +/- 15
DECISIVE_RELEVANCE = 0.5  # share of the claim's keywords a single source must match to decide alone
DECISIVE_KINDS = {"SUPPORTS": ("fact_check", "reference"), "REFUTES": ("fact_check",)}


def score_evidence(evidence, t_true=70, t_false=30, min_sources=MIN_INDEPENDENT_SOURCES):
    """Returns {"verdict", "score", "balance", "supporters", "refuters"}."""
    trusted = [e for e in evidence if e.get("weight", 0) > 0]
    # A mixed rating ("Half True") counts half for and half against.
    m = sum(e["weight"] for e in trusted if e["stance"] == "MIXED") / 2
    s = sum(e["weight"] for e in trusted if e["stance"] == "SUPPORTS") + m
    r = sum(e["weight"] for e in trusted if e["stance"] == "REFUTES") + m
    # Independence: several articles from one site count as one source.
    supporters = len({e["domain"] for e in trusted if e["stance"] == "SUPPORTS"})
    refuters = len({e["domain"] for e in trusted if e["stance"] == "REFUTES"})
    balance = round(100 * s / (s + r)) if s + r else NEUTRAL_SCORE

    def decided_by_one(stance):
        # A published fact-check of this same claim (or, for support, an
        # encyclopedia sentence stating it) is a verdict on its own.
        return any(e["stance"] == stance and e.get("kind") in DECISIVE_KINDS[stance]
                   and e.get("relevance", 0) >= DECISIVE_RELEVANCE for e in trusted)

    verdict = UNVERIFIED
    if balance >= t_true and (supporters >= min_sources or decided_by_one("SUPPORTS")):
        verdict = TRUE
    elif balance <= t_false and (refuters >= min_sources or decided_by_one("REFUTES")):
        verdict = FALSE
    score = balance if verdict != UNVERIFIED else round(
        NEUTRAL_SCORE + (balance - NEUTRAL_SCORE) * UNVERIFIED_SPREAD / 50)
    return {"verdict": verdict, "score": score, "balance": balance, "supporters": supporters, "refuters": refuters}
