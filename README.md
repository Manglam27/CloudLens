# Cloud Lens

**ICSI 499 Capstone Project — University at Albany, SUNY**

Cloud Lens is a cross-platform mobile app for iOS and Android that lets you
capture, store and edit your photos in the cloud. Sign in, take a picture or
pick one from your device, and it is uploaded to your own private cloud library
where you can view, edit, favorite and download it from any device you sign in
on.

Authentication and storage are backed by AWS Amplify — Amazon Cognito manages
user accounts, and Amazon S3 holds each user's images.

## Features

- **Account management** — sign up and sign in with Amazon Cognito
- **Capture** — take photos with the in-app camera, or import from your device
- **Cloud library** — images upload to private per-user S3 storage
- **Editing** — manipulate images and save the results back to the cloud
- **Favorites** — mark images to find them again quickly
- **Download** — save any cloud image back to your device gallery
- **AI Detection (FAX Check)** — *new in this version*, see below

## AI Detection (FAX Check)

This release introduces **AI Detection**, also called **FAX Check** — a way to
find out whether a photo is real or AI-generated.

It works like this:

1. The user takes a picture, or selects one already in their cloud library.
2. On the **photo library page**, they press the **AI Detection** button.
3. The image is sent to the Cloud Lens backend server for analysis.
4. The server runs image-processing and detection models against it, and
   determines whether the image was AI-generated or authentically captured.
5. The result is returned to the app and shown to the user, along with
   supporting facts about the image that explain the verdict.

The analysis runs **server-side** rather than on the device, so detection models
can be updated and improved without shipping a new version of the app.

## Target platforms

iOS and Android.

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
   details — see the AWS Amplify documentation for the full schema.

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
    photos_page.dart         Cloud library: upload, listing, download,
                             and the AI Detection (FAX Check) action
    editing_page.dart        Image manipulation
    favorites_page.dart      Saved images
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
