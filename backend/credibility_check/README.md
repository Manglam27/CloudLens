# Credibility check backend

Server side of the Check button (FR 1.4), the result screen (FR 1.5) and the
History tab (FR 1.6).

**Status: skeleton.** The full flow runs: the app starts a check, a worker reads
the screenshot with Amazon Textract, finds the claim, and scores it with the
Milestone 2 formula. Evidence search is not built yet (`EVIDENCE_ENABLED=false`),
so every verdict is Unverified for now.

## How a check runs

```
app ── POST /checks {imageKey} ──► Check API ── async ──► Check worker
 │                                   │                     │ 1 Textract OCR
 │                                   ▼                     │ 2 find claim (rules)
 └── GET /checks/{id} every 2 s ─► CloudLensChecks ◄───────┤ 3 evidence (not yet)
                                                           │ 4 score + verdict
                                                           ▼
                                            status: QUEUED → READING_TEXT → FINDING_CLAIM
                                                    → SEARCHING_SOURCES → SCORING → DONE / FAILED
```

## API

All routes are on `cloudlens-api` behind a Cognito JWT authorizer. The app sends
the user's ID token as `Authorization: Bearer <token>`.

| Route | Result |
| --- | --- |
| `POST /checks` `{"imageKey": "<userId>_<id>.jpg"}` | `202` queued, `200` from cache, `403` not your photo, `404` photo missing |
| `GET /checks/{checkId}` | `200` the check (status, verdict, score, claim, reason) |
| `GET /checks` | `200` `{"checks": [...]}`, the user's last 50 checks, newest first |

A user can only start checks on their own photos (`<their userId>_...`) and can
only read their own checks.

## Layout

```
check_api/lambda_function.py     Check API Lambda
check_worker/lambda_function.py  worker: stages, OCR, result
check_worker/claim.py            removes names, @handles, times and counts; keeps the claim
check_worker/scoring.py          Milestone 2 score formula and verdict rules
fakes.py, test_*.py              unit tests with in-memory AWS fakes
build.py                         builds dist/check_api.zip and dist/check_worker.zip
deploy.py                        creates or updates every AWS resource below
```

## AWS resources (us-east-1)

| Resource | Name |
| --- | --- |
| DynamoDB | `CloudLensChecks` (PK `userId`, SK `checkId`), `CloudLensResultCache` (PK `imageHash`), on-demand |
| Lambda | `cloudlens-check-api` (10 s, 256 MB), `cloudlens-check-worker` (60 s, 512 MB), Python 3.13 |
| IAM | `cloudlens-check-api-role`, `cloudlens-check-worker-role`, least privilege |
| API Gateway | JWT authorizer `cloudlens-cognito` and the three routes above |

Worker settings (Lambda environment variables): `T_TRUE=70`, `T_FALSE=30`,
`MAX_RESULTS=5`, `EVIDENCE_ENABLED=false`, and
`MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0` for the upcoming LLM steps.

## Test, build, deploy

```
python -m unittest discover -s backend/credibility_check -p "test_*.py"
python backend/credibility_check/build.py
python backend/credibility_check/deploy.py
```

`deploy.py` needs the AWS CLI signed in and reads the Cognito user pool and app
client IDs from `lib/amplifyconfiguration.dart`. It is safe to run again after
any code change.

## Next steps

1. Evidence: Google Fact Check Tools API and a news search limited to trusted
   domains, with the API keys in SSM Parameter Store (never in code).
2. Claim and stance with Claude on Amazon Bedrock (`MODEL_ID`), including
   Bedrock permission on the worker role.
3. Turn on `EVIDENCE_ENABLED`; from then on results are also cached per image.
