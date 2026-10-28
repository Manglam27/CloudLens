"""Credibility score and verdict (Milestone 2 slide 7, Milestone 4 FR 1.5).

Each piece of evidence is {"domain", "weight", "stance", "kind", "strength"}:
    weight    how much the site is trusted: 1.0 fact-checkers and wire services,
              0.6 news outlets and Wikipedia, 0 unknown sites (ignored)
    stance    SUPPORTS, REFUTES or MIXED; anything else is ignored
    kind      "fact_check", "reference" or "news"
    strength  0-1, how directly the source settles this exact claim: the LLM's
              judgement, or the keyword match when the rules judge

1. Vote of a source = weight x strength. A site counts once: of several
   articles from one site, only its strongest vote for each side is used.
   A MIXED rating gives half its vote to each side.
2. S = sum of the votes for, R = sum of the votes against.
3. Direction  D = (S - R) / (S + R), from -1 (all against) to +1 (all for).
4. Certainty  C = 1 - e^(-(S + R) / K), from 0 (no evidence) towards 1 as
   evidence piles up. K = 1, so one fact-check of strength 0.9 gives C = 0.59
   and three give C = 0.93.
5. Score = 50 + 50 x D x C, rounded: 50 means no evidence or an even split;
   the further from 50, the more and the stronger the evidence on one side.

Verdict: TRUE at 70 or more, FALSE at 30 or less, UNVERIFIED in between. A
TRUE or FALSE verdict also needs 2 independent sites on that side, or one
source that settles the claim by itself (a fact-check of this claim, or a
Wikipedia sentence stating it) with strength 0.6 or more; without that the
score is held at 69 or 31, so it never looks decided when the verdict is not.
"""

import math

TRUE, FALSE, UNVERIFIED = "TRUE", "FALSE", "UNVERIFIED"
MIN_INDEPENDENT_SOURCES = 2
NEUTRAL_SCORE = 50
CERTAINTY_SCALE = 1.0  # K: the evidence weight at which certainty reaches 63%
DECISIVE_STRENGTH = 0.6  # how directly one source must address the claim to decide alone
DECISIVE_KINDS = {"SUPPORTS": ("fact_check", "reference"), "REFUTES": ("fact_check",)}


def _votes(trusted):
    """(votes for, votes against), counting each site once per side."""
    best = {}
    for e in trusted:
        vote = e["weight"] * max(0.0, min(1.0, e.get("strength", 1.0)))
        sides = {"SUPPORTS": (("for", vote),), "REFUTES": (("against", vote),),
                 "MIXED": (("for", vote / 2), ("against", vote / 2))}.get(e["stance"], ())
        for side, v in sides:
            key = (e["domain"], side)
            best[key] = max(best.get(key, 0.0), v)
    s = sum(v for (_, side), v in best.items() if side == "for")
    r = sum(v for (_, side), v in best.items() if side == "against")
    return s, r


def score_evidence(evidence, t_true=70, t_false=30, min_sources=MIN_INDEPENDENT_SOURCES):
    """Returns {"verdict", "score", "direction", "certainty", "for", "against", "supporters", "refuters"}."""
    trusted = [e for e in evidence if e.get("weight", 0) > 0]
    s, r = _votes(trusted)
    supporters = len({e["domain"] for e in trusted if e["stance"] == "SUPPORTS"})
    refuters = len({e["domain"] for e in trusted if e["stance"] == "REFUTES"})
    direction = (s - r) / (s + r) if s + r else 0.0
    certainty = 1 - math.exp(-(s + r) / CERTAINTY_SCALE)
    score = round(NEUTRAL_SCORE + 50 * direction * certainty)

    def settled_by_one(stance):
        return any(e["stance"] == stance and e.get("kind") in DECISIVE_KINDS[stance]
                   and e.get("strength", 1.0) >= DECISIVE_STRENGTH for e in trusted)

    verdict = UNVERIFIED
    if score >= t_true:
        if supporters >= min_sources or settled_by_one("SUPPORTS"):
            verdict = TRUE
        else:
            score = t_true - 1
    elif score <= t_false:
        if refuters >= min_sources or settled_by_one("REFUTES"):
            verdict = FALSE
        else:
            score = t_false + 1
    return {"verdict": verdict, "score": score, "direction": round(direction, 2), "certainty": round(certainty, 2),
            "for": round(s, 2), "against": round(r, 2), "supporters": supporters, "refuters": refuters}
