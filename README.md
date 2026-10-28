# CloudLens

**Credibility scoring for text in screenshots**
ICSI 499 Capstone Project, University at Albany, SUNY

TEAM MEMBERS:
Patel Manglam, Patel Dhruvin
Samiyoddin Mohammed , Salman Hossain Khan


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
| Reset a forgotten password by e-mail code | FR 1.2 | Built |
| Upload from the camera or gallery to a private cloud library | FR 1.3 | Built |
| Request a credibility check, with live progress | FR 1.4 | Built |
| View the verdict, score, reason and the sources behind it | FR 1.5 | Built |
| History of past checks, re-opened without running again | FR 1.6 | Built |
| Edit photos with 12 filters running on AWS Lambda | — | Built |
| Favorites, save to gallery, delete | — | Built |

## How a credibility check works

1. **Request:** the user taps **Check** on a cloud photo, or **Upload and Check**
   on a local one. The server saves a job and returns at once; the app polls every
   2 seconds and shows the current step.
2. **Read text:** Amazon Textract extracts the text lines from the screenshot.
3. **Find the claim:** an LLM, NVIDIA Nemotron 3 Ultra, keeps the checkable
   statement, or returns none for opinions and jokes. Rules that remove names,
   @handles, times and like counts are the fallback.
4. **Find evidence:** Google Fact Check Tools, Google News, Bing News,
   Wikipedia, NewsAPI and The Guardian are searched in parallel. Only sites in
   the trusted-source table (fact-checkers, wire services, news outlets and
   Wikipedia, 109 in all) count, at most 8 results.
5. **Judge the sources:** the LLM labels each source Supports, Refutes, Mixed or
   Unrelated, rates how directly it settles the claim (its strength, 0 to 1) and
   writes a short reason. Rules that read fact-check ratings, headlines and
   keyword matches are the fallback.
6. **Score:** the formula below gives the score and verdict, which are saved for
   history.

### Credibility score

- Each source has a **weight**, how much the site is trusted: **1.0** for
  fact-checkers and wire services, **0.6** for established news outlets and
  Wikipedia. Unknown sites are ignored.
- Each source that takes a side also has a **strength**, how directly it
  settles this exact claim, from 0 to 1.
- **Vote** = weight × strength; a site counts once. **S** adds up the votes for
  the claim and **R** the votes against (a "Half True" rating gives half to each).
- **Direction** D = (S − R) ÷ (S + R) and **certainty** C = 1 − e^(−(S + R)).
- **Score = 50 + 50 × D × C.** 50 means no evidence or an even split; more and
  stronger evidence on one side moves the score further from 50. One strong
  fact-check rating a claim false gives about 20, two give 8, three give 3.
- **True:** score ≥ 70; **False:** score ≤ 30; **Unverified:** in between. True
  or False also needs 2 independent trusted sources on that side, or one
  published fact-check (or, for True, a Wikipedia sentence) of this claim;
  otherwise the score is held at 69 or 31.

The backend README has the full rules and worked examples.

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
                                                          ├─ NVIDIA Nemotron 3 Ultra (LLM)
                                                          └─ fact-check, news and Wikipedia searches
```

Only the extracted claim text is sent to the outside search services; photos
never leave AWS.

## Non-functional requirements

| Requirement | Target |
| --- | --- |
| Response time | Result within 30 s of upload for a screenshot with up to about 100 words |
| OCR accuracy | At least 95% of words read correctly on clear English screenshots |
| Verdict accuracy | At least 75% agreement with published fact-check ratings on a test set of 100+ screenshots |
| No guessing | Never True or False without 2 independent trusted sources or a published fact-check of the claim |
| Consistency | The same screenshot always returns the same verdict (cached results, fixed LLM settings) |
| Privacy | Screenshots visible only to the uploader, deleted on request, never used for training |
| Cost control | At most 8 search results and 2 LLM calls (claim, verdict) per check |

**Changes since the requirements were written:**
- The server runs on AWS Lambda behind API Gateway instead of a FastAPI
  container: no idle server cost, and it scales per request.
- Administrator features are out of scope. The team maintains the trusted-source
  list and the score limits as configuration.

## Roadmap

**Done**
- Development environment, toolchain and a working app: sign-up with e-mail
  verification, password reset, photo upload, cloud library, and editing on AWS
  Lambda.
- Credibility check: Check button, live progress, result screen with sources,
  History tab, Textract OCR, evidence search over five providers, a trusted-source
  table, LLM claim and stance steps (NVIDIA Nemotron) with rule fallbacks, and a
  score that grows with the amount and strength of the evidence.

**Next**
1. Measure the OCR and verdict accuracy targets on a test set of 100+ screenshots.
2. Private per-user folders (`private/<identity id>/`) enforced by IAM, so S3
   itself blocks access to other users' photos.
3. Require sign-in on `/photoEdits`, like the `/checks` routes.
4. Store favorites as photo keys instead of links that expire.

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
   in `backend/photo_edits/` and `backend/credibility_check/`. The credibility
   check also needs an NVIDIA API key and a Google Fact Check Tools API key in
   SSM Parameter Store; see its README.

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
    reset_password.dart      Password reset by e-mail code
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
