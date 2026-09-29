# CloudLens

**Credibility scoring for text in screenshots**
ICSI 499 Capstone Project, University at Albany, SUNY

CloudLens is an iOS and Android app for storing, editing and checking photos in
the cloud. Its core feature is the **credibility check**: the user uploads a
screenshot of a social-media post, and the server reads the text, finds the
claim, compares it with trusted news and fact-check sources, and returns
**True**, **False** or **Unverified** with a **credibility score from 0 to 100**
and a short reason.

## The problem

Screenshots of social-media posts spread false claims, and readers have no quick
way to check the text before sharing it. Existing tools answer different
questions:

- Fact-check sites are accurate but take hours to days, cover only viral claims
  and don't accept screenshots.
- Reverse image search finds where a picture appeared, but ignores the text.
- AI-image detectors judge whether the pixels were generated, not whether the
  claim is true.
- LLM chatbots answer from memory, can hallucinate and give no consistent score.

CloudLens turns the text of a screenshot into an evidence-based verdict, and
answers "Unverified" instead of guessing when the evidence is weak.

**Scope:** CloudLens judges the **text claim only**. It does not decide whether
the image itself is AI-generated or edited. English text only; a standalone app
with no social-media integration.

## Features

| Feature | Requirement | Status |
| --- | --- | --- |
| Sign up with an e-mail verification code | FR 1.1 | Built |
| Log in and log out | FR 1.2 | Built |
| Reset a forgotten password by e-mail code | FR 1.2 | Planned |
| Upload from the camera or gallery to a private cloud library | FR 1.3 | Built |
| Request a credibility check, with live progress | FR 1.4 | Built (skeleton) |
| View the verdict, score and reason | FR 1.5 | Built; every verdict is Unverified until evidence search is added |
| History of past checks, re-opened without running again | FR 1.6 | Built |
| Edit photos with 12 filters running on AWS Lambda | — | Built |
| Favorites, save to gallery, delete | — | Built |
| Show the sources behind each verdict | — | Future, if time allows |

## How a credibility check works

1. **Request:** the user taps **Check** on a cloud photo, or **Upload and Check**
   on a local one. The server saves a job and returns at once; the app polls every
   2 seconds and shows the current step.
2. **Read text:** Amazon Textract extracts the text lines from the screenshot.
3. **Find the claim:** rules remove names, @handles, times and like counts.
   *Planned:* one LLM call keeps the checkable statement, or returns none for
   opinions and jokes.
4. **Find evidence** *(planned)*: the Google Fact Check Tools API and a news search
   limited to trusted sources, at most 5 results.
5. **Judge the sources** *(planned)*: one LLM call labels each source Supports,
   Refutes or Unrelated and writes a short reason.
6. **Score:** the formula below gives the score and verdict, which are saved for
   history.

### Credibility score

- Each source has a weight: **1.0** for fact-checkers and wire services, **0.6**
  for established news outlets. Unknown sites are ignored.
- **Score = 100 × supporting weight ÷ (supporting + refuting weight)**
- **True:** score ≥ 70 and at least 2 independent trusted sources support the claim.
- **False:** score ≤ 30 and at least 2 independent trusted sources refute it.
- **Unverified:** every other case, including too few sources, disagreement,
  opinions, or no evidence.

The limits 70 and 30 are kept in configuration, so they can be tuned without
redeploying.

## Architecture

Serverless on AWS (us-east-1):

```
Flutter app ── login ───────────► Amazon Cognito (accounts, e-mail codes, JWT)
    │
    ├── upload photo ──────────► Amazon S3 (photos)
    │
    └── HTTPS + JWT ───────────► API Gateway (cloudlens-api)
                                   ├─ POST /photoEdits ─► photo-edits Lambda ─► S3
                                   └─ /checks ──────────► Check API Lambda ─► DynamoDB
                                                              │ async
                                                              ▼
                                                        Check worker Lambda
                                                          ├─ Amazon Textract (OCR)
                                                          ├─ Amazon Bedrock, Claude (planned)
                                                          └─ fact-check and news search APIs (planned)
```

Only the extracted claim text is sent to the outside search services; photos
never leave AWS.

## Non-functional requirements

| Requirement | Target |
| --- | --- |
| Response time | Result within 30 s of upload for a screenshot with up to about 100 words |
| OCR accuracy | At least 95% of words read correctly on clear English screenshots |
| Verdict accuracy | At least 75% agreement with published fact-check ratings on a test set of 100+ screenshots |
| No guessing | Never True or False with fewer than 2 independent trusted sources |
| Consistency | The same screenshot always returns the same verdict (cached results, fixed LLM settings) |
| Privacy | Screenshots visible only to the uploader, deleted on request, never used for training |
| Cost control | At most 5 search results and 1 verdict LLM call per check |

**Changes since the requirements were written:**
- The server runs on AWS Lambda behind API Gateway instead of a FastAPI
  container: no idle server cost, and it scales per request.
- Administrator features are out of scope. The team maintains the trusted-source
  list and the score limits as configuration.

## Roadmap

**Done**
- Development environment, toolchain and a working app: sign-up with e-mail
  verification, photo upload, cloud library, and editing on AWS Lambda.
- Credibility check skeleton: Check button, live progress, result screen,
  History tab, Textract OCR, claim rules and the scoring formula.

**Next**
1. Evidence search: Google Fact Check Tools API, and a news search limited to
   trusted sources. API keys are kept in AWS SSM Parameter Store, never in code.
2. Claim extraction and source judging with Claude Haiku 4.5 on Amazon Bedrock.
3. Switch evidence search on, which also starts caching results per screenshot.
4. Password reset by e-mail code (FR 1.2).
5. Private per-user folders (`private/<identity id>/`) enforced by IAM, so S3
   itself blocks access to other users' photos.
6. Require sign-in on `/photoEdits`, like the `/checks` routes.
7. Store favorites as photo keys instead of links that expire.
8. Measure the OCR and verdict accuracy targets on a test set of 100+ screenshots.

**Future, if time allows**
- Show the links to the sources behind each verdict.

## Toolchain

| Tool | Version |
| --- | --- |
| Flutter | 3.47.4+ (Dart 3.13.3+) |
| Android NDK | 28.2.13676358 (r28c) |
| Android minSdk / compileSdk | 24 / `flutter.compileSdkVersion` |
| Android Gradle Plugin | 8.13.0 |
| Kotlin Gradle Plugin | 2.2.20 |
| iOS deployment target | 13.0 (required by `amplify_auth_cognito`) |
| Backend | Python 3.13 on AWS Lambda |

## Setup

1. Install dependencies:

   ```
   flutter pub get
   ```

2. Create `lib/amplifyconfiguration.dart`. **This file is gitignored** because it
   holds your AWS identifiers, so it is not in the repository. It must export an
   `amplifyconfig` string containing your Cognito user pool, identity pool and S3
   bucket details; see the AWS Amplify documentation for the full schema.

3. Run on a connected device or emulator:

   ```
   flutter run
   ```

4. Backend: each Lambda has its own README with test, build and deploy steps,
   in `backend/photo_edits/` and `backend/credibility_check/`.

## Project structure

```
lib/
  main.dart                  App shell; resolves the Amplify session and routes
                             to LoginPage or MainPage
  amplifyconfiguration.dart  AWS config (gitignored - create locally)
  database.dart              Local sqflite persistence
  check_service.dart         Client for the credibility check API
  Pages/
    login.dart               Cognito sign-in
    signup.dart              Cognito registration
    confirm_signup.dart      E-mail verification code
    main_page.dart           Home / navigation (Photos, History, Favorites, Camera)
    camera_page.dart         Live camera capture
    photos_page.dart         Local and cloud library: upload, check, edit, delete
    check_page.dart          Credibility check progress and result
    history_page.dart        Past checks (FR 1.6)
    editing_page.dart        Image editing
    favorites_page.dart      Saved images
backend/
  photo_edits/               AWS Lambda for the 12 editing effects
  credibility_check/         Check API + worker Lambdas for the credibility check
android/                     Android host project
ios/                         iOS host project
test/                        Widget tests
```

## Development notes

- The NDK is required by the Flutter Gradle plugin even though this app ships no
  native code. If a build fails trying to provision it, install it directly:

  ```
  android sdk install "ndk/28.2.13676358"
  ```

- Flutter warns that AGP 8.13.0 / Kotlin 2.2.20 will eventually be unsupported.
  Moving to AGP 9 is currently blocked by the Amplify plugins, which apply the
  Kotlin Gradle Plugin and pin mismatched `compileSdk` values. Upgrading requires
  relaxing the exact `amplify_storage_s3: 2.6.1` pin first.

## License

Copyright (c) 2026 Manglam Patel and the CloudLens Capstone Team.
**All Rights Reserved.**

This project is proprietary. No permission is granted to use, copy, modify or
distribute this software or its source code without prior written consent of the
copyright holders. See [LICENSE](LICENSE) for the full terms.
