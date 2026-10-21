# Credibility check backend

Server side of the Check button (FR 1.4), the result screen (FR 1.5) and the
History tab (FR 1.6): reads the text of a screenshot, finds the claim, searches
trusted fact-checkers and news outlets, and returns True, False or Unverified
with a score from 0 to 100, a short reason and the sources behind it.

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
2. **Find the claim:** Claude Haiku 4.5 on Amazon Bedrock picks the checkable
   statement and a search query, or says the post is an opinion, satire or has
   nothing to check. Without Bedrock, rules in `claim.py` drop names, @handles,
   times and like counts and keep the post's first full sentence.
3. **Search evidence:** every provider below runs in parallel. Only results from
   sites in the trusted-source table are kept, ranked by how many of the claim's
   words they share, at most 8, preferring different sites.
4. **Judge:** Claude labels each source Supports, Refutes or Unrelated and writes
   the reason. Without Bedrock, rules read fact-check ratings ("False", "Didn't
   Happen", "Missing context"...) and headlines ("...did NOT remove..."), and
   count trusted news that reports the same thing, with the same names and no
   doubt words, as support.
5. **Score:** see [Scoring](#scoring). A result is cached per image only when
   every step ran without a fallback.

## Evidence providers

| Provider | Key | What it adds |
| --- | --- | --- |
| Google Fact Check Tools API | `/cloudlens/google-factcheck-api-key` | Rated fact-checks; searched with three phrasings because its matching is picky |
| Google News RSS | none | Fact-check articles and news reports from many outlets |
| Bing News RSS | none | The same from Bing's index, with summaries |
| NewsAPI.org | `/cloudlens/newsapi-key` (optional) | News from trusted domains; free plan shows articles a day late |
| The Guardian Content API | `/cloudlens/guardian-api-key` (optional) | Guardian reporting |

Keys are SSM Parameter Store SecureStrings, never in code. A provider without a
key is skipped. To add or replace one:

```
aws ssm put-parameter --region us-east-1 --name /cloudlens/google-factcheck-api-key --type SecureString --overwrite --value YOUR_KEY
```

## Trusted sources

The `CloudLensTrustedSources` table lists the sites CloudLens trusts: 39
fact-checkers and 10 wire services (weight 1.0) and 59 news outlets (weight 0.6).
Any other site weighs 0 and is ignored. `deploy.py` seeds missing entries from
`check_worker/trusted_sources.json` without overwriting edits. Manage the live
table with `sources.py`; the worker picks up changes within 5 minutes:

```
python backend/credibility_check/sources.py list
python backend/credibility_check/sources.py add example.org "Example News" news_outlet
python backend/credibility_check/sources.py disable example.org
python backend/credibility_check/sources.py set-weight example.org 0.8
```

## Scoring

- **Score = 100 × S ÷ (S + R)**, where S and R add up the weights of supporting
  and refuting sources; a mixed rating ("Half True") adds half to each. With no
  evidence either way the score is a neutral **50**.
- **True:** score ≥ 70, and either 2 independent sites support the claim or one
  published fact-check of this same claim does.
- **False:** score ≤ 30, and either 2 independent sites refute it or one
  published fact-check of this same claim does.
- **Unverified:** everything else.

## API

All routes are on `cloudlens-api` behind a Cognito JWT authorizer. The app sends
the user's ID token as `Authorization: Bearer <token>`.

| Route | Result |
| --- | --- |
| `POST /checks` `{"imageKey": "<userId>_<id>.jpg"}` | `202` queued, `200` from cache, `403` not your photo, `404` photo missing |
| `GET /checks/{checkId}` | `200` the check: status, verdict, score, claim, reason, sources |
| `GET /checks` | `200` `{"checks": [...]}`, the user's last 50 checks, newest first |

A user can only start checks on their own photos (`<their userId>_...`) and can
only read their own checks.

## Layout

```
check_api/lambda_function.py      Check API Lambda
check_worker/lambda_function.py   worker: stages, fallbacks, result
check_worker/claim.py             claim and search query rules
check_worker/llm.py               Claude on Bedrock: find the claim, judge the sources
check_worker/evidence.py          providers, trust lookup, relevance and stance rules
check_worker/scoring.py           score formula and verdict rules
check_worker/trusted_sources.json seed list of trusted sites
check_worker/requirements.txt     anthropic SDK, bundled into the worker zip
sources.py                        manage the trusted-source table
fakes.py, test_*.py               unit tests with in-memory AWS and web fakes
build.py                          builds dist/check_api.zip and dist/check_worker.zip
deploy.py                         creates or updates every AWS resource below
```

## AWS resources (us-east-1)

| Resource | Name |
| --- | --- |
| DynamoDB | `CloudLensChecks` (PK `userId`, SK `checkId`), `CloudLensResultCache` (PK `imageHash`), `CloudLensTrustedSources` (PK `domain`), on-demand |
| Lambda | `cloudlens-check-api` (10 s, 256 MB), `cloudlens-check-worker` (60 s, 512 MB), Python 3.13 |
| IAM | `cloudlens-check-api-role`, `cloudlens-check-worker-role`, least privilege |
| API Gateway | JWT authorizer `cloudlens-cognito` and the three routes above |
| Bedrock | Claude Haiku 4.5 (`MODEL_ID`); the account needs Anthropic model access |

Worker settings (Lambda environment variables): `T_TRUE=70`, `T_FALSE=30`,
`MIN_SOURCES=2`, `MAX_RESULTS=8`, `EVIDENCE_ENABLED=true`, `MODEL_ID`, and the
names of the three key parameters.

## Test, build, deploy

Needs Python 3.13 with pip, and the AWS CLI signed in to the project account.

```
python -m unittest discover -s backend/credibility_check -p "test_*.py"
python backend/credibility_check/build.py
python backend/credibility_check/deploy.py
```

The tests never touch AWS or the network. `build.py` downloads the Linux build
of the anthropic SDK into the worker zip. `deploy.py` reads the Cognito user
pool and app client IDs from `lib/amplifyconfiguration.dart`, and is safe to run
again after any change.

## Known limits

- Until Bedrock access is enabled for the account, every check uses the rule
  fallbacks. They handle fact-checked claims and widely reported news well, but
  can't read article text, so claims reported in different words may stay
  Unverified.
- Google's fact-check search returns different results between calls, so a claim
  with a single fact-check may be found on one check and missed on the next.
