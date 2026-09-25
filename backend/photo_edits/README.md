# Photo edits Lambda

Backend for the editing screen (`lib/Pages/editing_page.dart`). It receives an
image, applies one of 12 effects, saves the result to the CloudLens S3 bucket
and returns a temporary link to it.

Effects: `invert`, `grayscale`, `blur`, `edge`, `flip`, `brightness`,
`contrast`, `sharpen`, `sepia`, `pencil`, `threshold`, `emboss`.

## API

`POST /photoEdits`

Request:

```json
{ "file_name": "blur_1727000000000.jpg", "body": "<base64 JPEG>", "operation": "blur" }
```

Response (`200`):

```json
{ "url": "<presigned link, valid 1 hour>", "key": "<S3 key of the edited image>" }
```

Errors return `400` (bad input), `413` (image over 5 MB) or `500`, with an
`error` message.

Where results are saved:

- With the Cognito authorizer (step 5 below): `<userId>_<file_name>`, the same
  prefix the app uses for uploads, so edits appear in the user's cloud library.
- Without it: `edited/<file_name>`.

## Build

```
python backend/photo_edits/build.py
```

Produces `photo_edits.zip` (gitignored) with the Linux build of Pillow that
Lambda needs.

## Test

```
pip install Pillow
python -m unittest backend/photo_edits/test_lambda_function.py
```

## Deploy (AWS Console, region us-east-1)

1. **Create the function.** Lambda → Create function → Author from scratch.
   Name `cloudlens-photo-edits`, runtime **Python 3.13**, architecture
   **x86_64**, execution role "Create a new role with basic Lambda permissions".
   The runtime **must** be Python 3.13, not the console's newer default:
   `build.py` bundles the Python 3.13 build of Pillow, and any other runtime
   fails every request with `Runtime.ImportModuleError: cannot import name
   '_imaging' from 'PIL'`.
2. **Upload the code.** Code → Upload from → .zip file → `photo_edits.zip`.
   The handler must stay `lambda_function.lambda_handler`.
3. **Configure it.** Configuration → General configuration: memory **1024 MB**,
   timeout **30 s**. Configuration → Environment variables: add
   `BUCKET_NAME` = your bucket name.
4. **Allow it to use the bucket.** Configuration → Permissions → open the role
   → Add permissions → Create inline policy → JSON:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": ["s3:PutObject", "s3:GetObject"],
         "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/*"
       }
     ]
   }
   ```

5. **Create the endpoint.** API Gateway → Create API → **HTTP API** → Build.
   Integration: Lambda `cloudlens-photo-edits`. Route: `POST` `/photoEdits`.
   Stage `$default` with auto-deploy. The endpoint is
   `<Invoke URL>/photoEdits`.
6. **Require sign-in (recommended).** In the API: Authorization → Manage
   authorizers → Create → **JWT**. Identity source
   `$request.header.Authorization`, issuer
   `https://cognito-idp.us-east-1.amazonaws.com/<USER_POOL_ID>`, audience
   `<APP_CLIENT_ID>`. Attach it to `POST /photoEdits`. The app must then send
   the signed-in user's Cognito token in the `Authorization` header.
