# Cloud Lens

A Flutter app for capturing, uploading and manipulating images in the cloud,
backed by AWS Amplify (Cognito authentication + S3 storage).

**Target platforms: iOS and Android.**

## Requirements

| Tool | Version |
| --- | --- |
| Flutter | 3.47.4+ (Dart 3.13.3+) |
| Android NDK | 28.2.13676358 (r28c) |
| Android minSdk / compileSdk | 24 / `flutter.compileSdkVersion` |
| Android Gradle Plugin | 8.13.0 |
| Kotlin Gradle Plugin | 2.2.20 |
| iOS deployment target | 13.0 (required by `amplify_auth_cognito`) |

## Setup

1. Install dependencies:

   ```
   flutter pub get
   ```

2. Create `lib/amplifyconfiguration.dart`. **This file is gitignored** because it
   holds your AWS identifiers, so it is not in the repository. It must export an
   `amplifyconfig` string containing your Cognito User Pool and S3 bucket
   details — see the AWS Amplify docs for the full schema.

3. Run on a connected device or emulator:

   ```
   flutter run
   ```

## Project structure

```
lib/
  main.dart                  App shell; resolves the Amplify session and routes
                             to LoginPage or MainPage
  amplifyconfiguration.dart  AWS config (gitignored - create locally)
  database.dart              Local sqflite persistence
  Pages/
    login.dart               Cognito sign-in
    signup.dart              Cognito registration
    main_page.dart           Home / navigation
    camera_page.dart         Live camera capture
    photos_page.dart         S3 upload, listing and download
    editing_page.dart        Image manipulation
    favorites_page.dart      Saved images
android/                     Android host project
ios/                         iOS host project
test/                        Widget tests
```

## Roadmap

Server-side image analysis: submit an uploaded image to a backend service that
determines whether it was AI-generated, and returns supporting details about the
image. The `http` dependency is already in place for this client-side call.

## Notes

- The NDK is required by the Flutter Gradle plugin even though this app ships no
  native code. If a build fails trying to provision it, install it directly:

  ```
  android sdk install "ndk/28.2.13676358"
  ```

- Flutter warns that AGP 8.13.0 / Kotlin 2.2.20 will eventually be unsupported.
  Moving to AGP 9 is currently blocked by the Amplify plugins, which apply the
  Kotlin Gradle Plugin and pin mismatched `compileSdk` values. Upgrading requires
  relaxing the exact `amplify_storage_s3: 2.6.1` pin first.
