# Credibility check backend

Server side of the Check button (FR 1.4), the result screen (FR 1.5) and the
History tab (FR 1.6): reads the text of a screenshot, finds the claim, searches
trusted fact-checkers, news outlets and Wikipedia, and returns True, False or
Unverified with a score from 0 to 100, a short reason and the sources behind it.

## How a check runs

```
app ── POST /checks {imageKey} ──► Check API ── async ──► Check worker
 │                                   │                     │ 1 Textract OCR
 │                                   ▼                     │ 2 find the claim
 └── GET /checks/{id} every 2 s ─► CloudLensChecks ◄───────┤ 3 search evidence
                                                           │ 4 judge each source
                                                           │ 5 score + verdict
                                                           ▼
                                            status: QUEUED → READING_TEXT → FINDING_CLAIM
                                                    → SEARCHING_SOURCES → SCORING → DONE / FAILED
```

1. **Read text:** Amazon Textract returns the text lines of the screenshot.
2. **Find the claim:** the LLM (NVIDIA Nemotron 3 Ultra, see below) picks the
   checkable statement and a 3 to 8 word search query, or says the post is an
   opinion, satire or has nothing to check. Without the LLM, rules in `claim.py`
   drop names, @handles, times and like counts and keep the post's first full
   sentence.
3. **Search evidence:** every provider below runs in parallel. Only results from
   sites in the trusted-source table are kept, ranked by how many of the claim's
   words they share, at most 8, preferring different sites.
4. **Judge each source:** the LLM gives every source a stance (Supports,
   Refutes, Mixed or Unrelated) and a **strength** from 0 to 1: how directly it
   settles this exact claim. Without the LLM, rules do the same from the
   evidence itself (see [Judging without the LLM](#judging-without-the-llm)).
5. **Score:** see [Scoring](#scoring). A result is cached per image only when
   every step ran without a fallback, so the same screenshot then always gets
   the same answer.

## The LLM

| | |
| --- | --- |
| Model | `nvidia/nemotron-3-ultra-550b-a55b` (NVIDIA Nemotron 3 Ultra, the largest model in NVIDIA's API catalog), set by `MODEL_ID` |
| API | `https://integrate.api.nvidia.com/v1/chat/completions` (OpenAI-compatible), key in SSM at `/cloudlens/nvidia-api-key` |
| Settings | temperature 0 and thinking switched off (`chat_template_kwargs.enable_thinking = false`), for repeatable, fast answers |
| Calls per check | 2: find the claim, judge the sources |

Each reply must be one JSON object of the shape given in the prompt; anything
else, a cut-off reply, a missing key or an API error makes the worker fall back
to the rules for that step. The model judges only from the sources it is given,
never from its own memory.

## Evidence providers

| Provider | Key | What it adds |
| --- | --- | --- |
| Google Fact Check Tools API | `/cloudlens/google-factcheck-api-key` | Rated fact-checks; searched with three phrasings because its matching is picky |
| Google News RSS | none | Fact-check articles and news reports from many outlets |
| Bing News RSS | none | The same from Bing's index, with summaries |
| Wikipedia API | none | Opening paragraphs of the articles the claim names, for settled facts ("the capital of Australia is Canberra") |
| NewsAPI.org | `/cloudlens/newsapi-key` (optional) | News from trusted domains; free plan shows articles a day late |
| The Guardian Content API | `/cloudlens/guardian-api-key` (optional) | Guardian reporting |

Keys are SSM Parameter Store SecureStrings, never in code. A provider without a
key is skipped. To add or replace one:

```
aws ssm put-parameter --region us-east-1 --name /cloudlens/nvidia-api-key --type SecureString --overwrite --value YOUR_KEY
```

## Trusted sources

The `CloudLensTrustedSources` table lists the sites CloudLens trusts and how
much (the source's **weight**): 39 fact-checkers and 10 wire services weigh
1.0; 59 news outlets and Wikipedia weigh 0.6. Any other site weighs 0 and is
ignored. `deploy.py` seeds missing entries from
`check_worker/trusted_sources.json` without overwriting edits. Manage the live
table with `sources.py`; the worker picks up changes within 5 minutes:

```
python backend/credibility_check/sources.py list
python backend/credibility_check/sources.py add example.org "Example News" news_outlet
python backend/credibility_check/sources.py disable example.org
python backend/credibility_check/sources.py set-weight example.org 0.8
```

## Scoring

Every source that takes a side has a **weight** (how much the site is trusted)
and a **strength** (how directly it settles this claim, from the LLM or the
rules).

1. **Vote** of a source = weight × strength. A site counts once: of several
   articles from one site, only its strongest vote for each side is used. A
   Mixed rating gives half its vote to each side.
2. **S** = sum of the votes for the claim, **R** = sum of the votes against.
3. **Direction** D = (S − R) ÷ (S + R), from −1 (all against) to +1 (all for).
4. **Certainty** C = 1 − e^(−(S + R)), from 0 with no evidence towards 1 as
   strong evidence piles up.
5. **Score** = 50 + 50 × D × C, rounded.

So 50 means no evidence or an even split, and the further a score is from 50,
the more evidence, and the stronger, there is on one side.

**Verdict:** True at 70 or more, False at 30 or less, Unverified in between. A
True or False verdict also needs 2 independent sites on that side, or one
source that settles the claim by itself (a fact-check of this claim, or a
Wikipedia sentence stating it) with strength 0.6 or more. Without that, the
score is held at 69 or 31, so it never looks decided when the verdict is not.

**Worked examples** (fact-checkers and wire services weigh 1.0, outlets 0.6):

| Evidence | S | R | D | C | Score | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Nothing found | 0 | 0 | 0 | 0 | 50 | Unverified |
| 1 fact-check "False", strength 0.9 | 0 | 0.9 | −1 | 0.59 | 20 | False |
| 2 fact-checks "False", strength 0.9 | 0 | 1.8 | −1 | 0.83 | 8 | False |
| 3 fact-checks "False", strength 0.9 | 0 | 2.7 | −1 | 0.93 | 3 | False |
| 1 news outlet backs it, strength 0.9 | 0.54 | 0 | +1 | 0.42 | 71 → 69 | Unverified (needs a 2nd site) |
| 2 news outlets back it, strength 0.9 | 1.08 | 0 | +1 | 0.66 | 83 | True |
| 2 wire services back it, strength 1.0 | 2.0 | 0 | +1 | 0.86 | 93 | True |
| 1 fact-check "False" (0.9) and 1 outlet backs it (0.8) | 0.48 | 0.9 | −0.30 | 0.75 | 39 | Unverified, leans false |
| 1 fact-check "Half true", strength 0.9 | 0.45 | 0.45 | 0 | 0.59 | 50 | Unverified |

The result stores what went into the score: each source's weight and strength
(as percentages), the certainty, and `judgedBy` (the model, or `rules`).

## Judging without the LLM

When the LLM is not available, `evidence.py` judges each source by rules:

- **Fact-checks:** count only when the claim they reviewed is much the same as
  this one (word overlap measured both ways) and names the same people and
  places. The rating decides the stance ("False", "Didn't Happen", "Missing
  context" refute; "True", "Correct" support; "Half true" is mixed); without a
  rating the headline does ("...did NOT remove..."). Strength = the overlap.
- **News:** supports the claim when its headline states most of the claim, with
  the same names, without doubt words ("false", "viral", "claims") and not as a
  question. Strength = the share of the claim's words in the headline. Once a
  fact-check refutes the claim, news stories no longer count as support.
- **Wikipedia:** supports the claim when one sentence of the main article about
  its subject states it, in the present tense. Strength = the share of the
  claim's words in that sentence.

## API

All routes are on `cloudlens-api` behind a Cognito JWT authorizer. The app sends
the user's ID token as `Authorization: Bearer <token>`.

| Route | Result |
| --- | --- |
| `POST /checks` `{"imageKey": "<userId>_<id>.jpg"}` | `202` queued, `200` from cache, `403` not your photo, `404` photo missing |
| `GET /checks/{checkId}` | `200` the check: status, verdict, score, claim, reason, sources, certainty, judgedBy |
| `GET /checks` | `200` `{"checks": [...]}`, the user's last 50 checks, newest first |

A user can only start checks on their own photos (`<their userId>_...`) and can
only read their own checks.

## Layout

```
check_api/lambda_function.py      Check API Lambda
check_worker/lambda_function.py   worker: stages, fallbacks, result
check_worker/claim.py             claim and search query rules
check_worker/llm.py               NVIDIA Nemotron: find the claim, judge the sources
check_worker/evidence.py          providers, trust lookup, relevance and stance rules
check_worker/scoring.py           score formula and verdict rules
check_worker/trusted_sources.json seed list of trusted sites
sources.py                        manage the trusted-source table
fakes.py, test_*.py               unit tests with in-memory AWS, web and LLM fakes
build.py                          builds dist/check_api.zip and dist/check_worker.zip
deploy.py                         creates or updates every AWS resource below
```

## AWS resources (us-east-1)

| Resource | Name |
| --- | --- |
| DynamoDB | `CloudLensChecks` (PK `userId`, SK `checkId`), `CloudLensResultCache` (PK `imageHash`), `CloudLensTrustedSources` (PK `domain`), on-demand |
| Lambda | `cloudlens-check-api` (10 s, 256 MB), `cloudlens-check-worker` (120 s, 512 MB), Python 3.13 |
| IAM | `cloudlens-check-api-role`, `cloudlens-check-worker-role`, least privilege |
| API Gateway | JWT authorizer `cloudlens-cognito` and the three routes above |
| SSM | the four API keys above, as SecureStrings |

Worker settings (Lambda environment variables): `T_TRUE=70`, `T_FALSE=30`,
`MIN_SOURCES=2`, `MAX_RESULTS=8`, `EVIDENCE_ENABLED=true`, `MODEL_ID`, and the
names of the four key parameters.

## Test, build, deploy

Needs Python 3.13, and the AWS CLI signed in to the project account.

```
python -m unittest discover -s backend/credibility_check -p "test_*.py"
python backend/credibility_check/build.py
python backend/credibility_check/deploy.py
```

The tests never touch AWS, the network or the LLM. Both Lambdas use only the
Python standard library and boto3. `deploy.py` reads the Cognito user pool and
app client IDs from `lib/amplifyconfiguration.dart`, and is safe to run again
after any change.

## Known limits

- The rules can't read article text, so without the LLM a claim reported in
  different words may stay Unverified, and a settled claim that is false ("the
  Eiffel Tower is in Berlin") can't be refuted from an encyclopedia.
- Google's fact-check search returns different results between calls, so a claim
  with a single fact-check may be found on one check and missed on the next.
- A claim no trusted source has covered stays at 50, however false it looks.
