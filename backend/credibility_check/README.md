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

1. **Read text:** Amazon Textract reads the screenshot twice in parallel: as it
   is, and as a light-text version (every near-white pixel turned black, the
   rest white). The reading with more confidently read words is used. Meme
   captions, white letters with a black outline over a picture, are read poorly
   or not at all the first way and fully the second way.
2. **Find the claim:** the LLM (NVIDIA Nemotron 3 Ultra, see below) picks the
   checkable statement, a 3 to 8 word search query and a 2 to 4 word topic
   ("moon landing hoax", "Key Bridge collapse"), or says the post is an
   opinion, satire or has nothing to check. Without the LLM, rules in `claim.py`
   drop names, @handles, times and like counts and keep the post's first full
   sentence.
3. **Search evidence:** every provider below runs in parallel; a busy provider
   (HTTP 429 or 5xx) or a timeout is retried twice after a short wait. Only
   results from sites in the trusted-source table are kept, ranked by how
   closely they match the claim or its topic, at most 10, preferring different
   sites. Fact-checks fill at most half of the places and Wikipedia at most 2,
   so news reporting the event is always judged too.
4. **Judge each source:** the LLM gives every source a stance (Supports,
   Refutes, Mixed or Unrelated) and a **strength** from 0 to 1: how directly it
   settles this exact claim. Without the LLM, rules do the same from the
   evidence itself (see [Judging without the LLM](#judging-without-the-llm)).
5. **Score:** see [Scoring](#scoring). A result is cached per image only when
   every step ran without a fallback, so the same screenshot then always gets
   the same answer. Each cached result records `RESULTS_VERSION` (set in
   `deploy.py`); after a change to the claim, judging or scoring logic the
   version is raised, and results from an older checker are run again.

## The LLM

| | |
| --- | --- |
| Main model | `nvidia/nemotron-3-ultra-550b-a55b` (NVIDIA Nemotron 3 Ultra), set by `MODEL_ID` |
| Backup models | `google/gemma-4-31b-it`, then `nvidia/nemotron-3.5-lightning-30b-a3b`, set by `FALLBACK_MODEL_ID` |
| API | `https://integrate.api.nvidia.com/v1/chat/completions` (OpenAI-compatible), key in SSM at `/cloudlens/nvidia-api-key` |
| Settings | temperature 0 and thinking switched off (`chat_template_kwargs.enable_thinking = false`), for repeatable, fast answers |
| Calls per check | 2: find the claim, judge the sources (0 when both answers are already stored) |

Each reply must be one JSON object of the shape given in the prompt. Anything
else or a cut-off reply is asked again once (hosted models sometimes return an
empty reply under load). The model judges only from the sources it is given,
never from its own memory. The judge answers one question per source,
"according to this source, is the claim true?" (TRUE, FALSE, PARTLY or
NOT_ADDRESSED), which models get right far more often than "does it support or
refute the claim", and is shown the publication date of each source.

### Staying within NVIDIA's rate limit

NVIDIA's free tier allows about 30 requests a minute per key, but on this key
Nemotron 3 Ultra accepts a burst of about 6 and then sustains only 3 to 4 a
minute; each model has its own budget. So the AI is never a bottleneck:

1. **Shared budget:** every request takes a slot in a DynamoDB counter
   (`CloudLensLLM` table, `gate.py`) shared by all running checks: at most
   `LLM_RPM` (4) a minute for the main model and `FALLBACK_LLM_RPM` (24) for
   each backup, counted in 10-second windows. A request that finds no free
   slot waits for the next window instead of failing.
2. **Model chain (`llm.py`):** the main model gets 8 seconds to find a slot.
   If NVIDIA answers "busy" (HTTP 429), every check skips it for 60 seconds;
   if a backup model takes over 20 seconds, every check skips it for 2
   minutes. The next model in the chain answers instead. The last one waits
   and retries a 429 after 3, 6 and 10 seconds while the check's 75-second
   deadline allows.
3. **Answer cache:** the answer to an identical request is stored for 30 days
   and reused, so a repeated check costs nothing and always gets the same
   answer. Answers of the last-resort model are not kept.
4. **Last resort:** only if every model fails do the rules judge (see below).

A result judged by a backup model says so ("Checked by a backup AI model while
the main one was busy...") and is not kept in the result cache, so checking
the same image again later gets the main model's answer. Each check records the
time it spent waiting for a slot and which model answered each step.

## Evidence providers

| Provider | Key | What it adds |
| --- | --- | --- |
| Google Fact Check Tools API | `/cloudlens/google-factcheck-api-key` | Rated fact-checks; searched with the keywords, the claim, its topic and its core words, because its matching is picky (a whole sentence often finds nothing) |
| Google News RSS | none | Fact-check articles and news reports from many outlets |
| Bing News RSS | none | The same from Bing's index, with summaries |
| Wikipedia API | none | Opening paragraphs of the articles the claim names and of the best matches for its keywords and topic, for settled facts ("the capital of Australia is Canberra") |
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
4. **Certainty** C = 1 − e^(−(S + R) / K) with K = 0.8, from 0 with no
   evidence towards 1 as strong evidence piles up.
5. **Score** = 50 + 50 × D × C, rounded.

So 50 means no evidence or an even split, and the further a score is from 50,
the more evidence, and the stronger, there is on one side.

**Fact-checks outrank news.** When a trusted fact-check addresses this claim
(Supports, Refutes or Mixed, strength 0.6 or more), only fact-checks vote. A
fact-checker that reviewed the exact claim has already weighed the news, which
may be old stories about a real event that a post presents as new ("the Senate
*just* confirmed..."). A fact-check about pictures of an event ("viral images
of the splashdown are AI-generated") is the exception: it says nothing about
whether the event happened, so it votes with the rest and cannot set the news
aside or decide alone.

**Outdated fact-checks.** Every source carries its publication date. A
fact-check that rated the claim false is set aside (shown as Related) when at
least 2 trusted news reports from different sites, published after it, state the
claim as fact. It reviewed an earlier hoax, not the event the news reports: in
testing, "Another hoax claims Queen Elizabeth II is dead" (2021) and "Pope
Francis is NOT dead" outvoted the BBC and Reuters reports of the real deaths
until this rule was added. A fact-check newer than the news ("Senate did NOT
*just* confirm...", about a confirmation years earlier) is never set aside.

**Verdict:** True at 70 or more, False at 30 or less, Unverified in between. A
True or False verdict also needs 2 independent sites on that side, or one
source that settles the claim by itself (a fact-check of this claim, or a
Wikipedia sentence stating or plainly contradicting it) with strength 0.6 or
more. Without that, the score is held at 69 or 31, so it never looks decided
when the verdict is not.

**Worked examples** (fact-checkers and wire services weigh 1.0, outlets and
Wikipedia 0.6):

| Evidence | S | R | D | C | Score | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Nothing found | 0 | 0 | 0 | 0 | 50 | Unverified |
| 1 fact-check "False", strength 0.9 | 0 | 0.9 | −1 | 0.68 | 16 | False |
| 2 fact-checks "False", strength 0.9 | 0 | 1.8 | −1 | 0.89 | 5 | False |
| 3 fact-checks "False", strength 0.9 | 0 | 2.7 | −1 | 0.97 | 2 | False |
| 1 Wikipedia sentence contradicts it, strength 0.9 | 0 | 0.54 | −1 | 0.49 | 25 | False |
| 1 news outlet backs it, strength 0.9 | 0.54 | 0 | +1 | 0.49 | 75 → 69 | Unverified (needs a 2nd site) |
| 2 news outlets back it, strength 0.9 | 1.08 | 0 | +1 | 0.74 | 87 | True |
| 2 wire services back it, strength 1.0 | 2.0 | 0 | +1 | 0.92 | 96 | True |
| 1 fact-check "False" (0.9) and 1 outlet backs it (0.8) | 0 | 0.9 | −1 | 0.68 | 16 | False (fact-checks outrank news) |
| 1 fact-check "False" about AI images (0.9) and 9 outlets back it (0.8) | 4.64 | 0.9 | +0.68 | 1.0 | 84 | True |
| 1 fact-check "Half true", strength 0.9 | 0.45 | 0.45 | 0 | 0.68 | 50 | Unverified |

**Why 70 and 30, and K = 0.8:** on the test set below, every claim the checker
got right scored 78 or more (true) or 22 or less (false); no correct verdict
fell between 23 and 77. So 70 and 30 sit inside that gap with a margin of 8 on
each side, and 65/35 or 60/40 changed no verdict. K = 0.8 (instead of 1) lets
one strong Wikipedia sentence, weight 0.6 × strength 0.8 or more, reach a
verdict; on the test set it turned 2 Unverified into right answers and no
answer into a wrong one.

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

**The rules never back a claim up.** Shared keywords are not support: on the
test set the rules read "Vaccines do not cause autism" as backing "the MMR
vaccine causes autism", and "Sydney is the capital of New South Wales" as
backing "Sydney is the capital of Australia". So when the model can't judge
(it was busy or its reply was unusable twice), every source the rules would
count as support is shown as related instead. Only fact-check ratings against
the claim count, which can still give False. The reason ends with "The AI judge
was busy...", and the result is not cached, so checking again a minute later
gets the full judgement. A rate-limit reply from NVIDIA (HTTP 429) is not
retried, since that only adds load.

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
| DynamoDB | `CloudLensChecks` (PK `userId`, SK `checkId`), `CloudLensResultCache` (PK `imageHash`), `CloudLensTrustedSources` (PK `domain`), `CloudLensLLM` (PK `key`; rate-limit slots, cool-downs and stored answers, deleted by TTL), on-demand |
| Lambda | `cloudlens-check-api` (10 s, 256 MB), `cloudlens-check-worker` (180 s, 512 MB, with Pillow for the second OCR reading), Python 3.13 |
| IAM | `cloudlens-check-api-role`, `cloudlens-check-worker-role`, least privilege |
| API Gateway | JWT authorizer `cloudlens-cognito` and the three routes above |
| SSM | the four API keys above, as SecureStrings |

Worker settings (Lambda environment variables): `T_TRUE=70`, `T_FALSE=30`,
`MIN_SOURCES=2`, `MAX_RESULTS=10`, `EVIDENCE_ENABLED=true`, `MODEL_ID`, `FALLBACK_MODEL_ID`,
`LLM_TABLE=CloudLensLLM`, `LLM_RPM=4`, `FALLBACK_LLM_RPM=24`, `LLM_DEADLINE_SECONDS=75`,
`RESULTS_VERSION`, and the names of the four key parameters.

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
