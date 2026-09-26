"""Creates or updates the credibility check backend on AWS (us-east-1).

    python backend/credibility_check/build.py
    python backend/credibility_check/deploy.py

Safe to run repeatedly: anything that exists is updated, anything missing is
created. Needs the AWS CLI signed in to the CloudLens account. The Cognito user
pool and app client IDs are read from lib/amplifyconfiguration.dart.

Resources:
    DynamoDB   CloudLensChecks (userId, checkId), CloudLensResultCache (imageHash)
    IAM        cloudlens-check-api-role, cloudlens-check-worker-role
    Lambda     cloudlens-check-api, cloudlens-check-worker (Python 3.13)
    API        routes POST /checks, GET /checks, GET /checks/{checkId} on
               cloudlens-api, behind a Cognito JWT authorizer
"""

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REGION = "us-east-1"
API_ID = "ieip1diyzc"  # cloudlens-api
BUCKET = "cloudlens-photos-2026"
CHECKS_TABLE = "CloudLensChecks"
CACHE_TABLE = "CloudLensResultCache"
API_FN, WORKER_FN = "cloudlens-check-api", "cloudlens-check-worker"
AUTHORIZER = "cloudlens-cognito"
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
ROUTES = ["POST /checks", "GET /checks", "GET /checks/{checkId}"]

AWS = shutil.which("aws") or str(Path.home() / "AppData/Local/Programs/Amazon/AWSCLIV2/aws.exe")


class AwsError(Exception):
    pass


def aws(*args, ok_codes=()):
    """Runs an AWS CLI command and returns its JSON output (or None)."""
    proc = subprocess.run([AWS, *args, "--region", REGION, "--output", "json"], capture_output=True, text=True)
    if proc.returncode != 0:
        if any(code in proc.stderr for code in ok_codes):
            return None
        raise AwsError(f"aws {' '.join(args[:2])} failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def cognito_ids():
    config = (ROOT / "lib" / "amplifyconfiguration.dart").read_text(encoding="utf-8")
    pool = re.search(r'"PoolId":\s*"(us-east-1_[A-Za-z0-9]+)"', config)
    client = re.search(r'"AppClientId":\s*"([a-z0-9]+)"', config)
    if not pool or not client:
        sys.exit("Could not find the user pool ID and app client ID in lib/amplifyconfiguration.dart")
    return pool.group(1), client.group(1)


def ensure_table(name, keys):
    if aws("dynamodb", "describe-table", "--table-name", name, ok_codes=("ResourceNotFoundException",)):
        print(f"  table {name}: exists")
        return
    attrs = [{"AttributeName": k, "AttributeType": "S"} for k in keys]
    schema = [{"AttributeName": k, "KeyType": t} for k, t in zip(keys, ["HASH", "RANGE"])]
    aws("dynamodb", "create-table", "--table-name", name, "--billing-mode", "PAY_PER_REQUEST",
        "--attribute-definitions", json.dumps(attrs), "--key-schema", json.dumps(schema))
    aws("dynamodb", "wait", "table-exists", "--table-name", name)
    print(f"  table {name}: created")


def ensure_role(name, policy):
    trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    role = aws("iam", "get-role", "--role-name", name, ok_codes=("NoSuchEntity",))
    if role is None:
        role = aws("iam", "create-role", "--role-name", name, "--assume-role-policy-document", json.dumps(trust))
        print(f"  role {name}: created")
    else:
        print(f"  role {name}: exists")
    aws("iam", "attach-role-policy", "--role-name", name,
        "--policy-arn", "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    aws("iam", "put-role-policy", "--role-name", name, "--policy-name", f"{name}-access",
        "--policy-document", json.dumps({"Version": "2012-10-17", "Statement": policy}))
    return role["Role"]["Arn"]


def ensure_function(name, role_arn, zip_path, env, timeout, memory):
    env_json = json.dumps({"Variables": env})
    if aws("lambda", "get-function", "--function-name", name, ok_codes=("ResourceNotFoundException",)):
        aws("lambda", "update-function-code", "--function-name", name, "--zip-file", f"fileb://{zip_path}")
        aws("lambda", "wait", "function-updated-v2", "--function-name", name)
        aws("lambda", "update-function-configuration", "--function-name", name, "--role", role_arn,
            "--runtime", "python3.13", "--handler", "lambda_function.lambda_handler",
            "--timeout", str(timeout), "--memory-size", str(memory), "--environment", env_json)
        aws("lambda", "wait", "function-updated-v2", "--function-name", name)
        print(f"  function {name}: updated")
        return
    # A new IAM role takes a few seconds before Lambda can assume it.
    for attempt in range(12):
        try:
            aws("lambda", "create-function", "--function-name", name, "--runtime", "python3.13",
                "--architectures", "x86_64", "--role", role_arn, "--handler", "lambda_function.lambda_handler",
                "--zip-file", f"fileb://{zip_path}", "--timeout", str(timeout), "--memory-size", str(memory),
                "--environment", env_json)
            break
        except AwsError as e:
            if "cannot be assumed" not in str(e) or attempt == 11:
                raise
            time.sleep(5)
    aws("lambda", "wait", "function-active-v2", "--function-name", name)
    print(f"  function {name}: created")


def ensure_api(account, pool, client):
    issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{pool}"
    authorizers = aws("apigatewayv2", "get-authorizers", "--api-id", API_ID)["Items"]
    auth = next((a for a in authorizers if a["Name"] == AUTHORIZER), None)
    jwt = json.dumps({"Audience": [client], "Issuer": issuer})
    if auth:
        aws("apigatewayv2", "update-authorizer", "--api-id", API_ID, "--authorizer-id", auth["AuthorizerId"],
            "--jwt-configuration", jwt)
        auth_id = auth["AuthorizerId"]
        print(f"  authorizer {AUTHORIZER}: updated")
    else:
        auth_id = aws("apigatewayv2", "create-authorizer", "--api-id", API_ID, "--name", AUTHORIZER,
                      "--authorizer-type", "JWT", "--identity-source", "$request.header.Authorization",
                      "--jwt-configuration", jwt)["AuthorizerId"]
        print(f"  authorizer {AUTHORIZER}: created")

    fn_arn = f"arn:aws:lambda:{REGION}:{account}:function:{API_FN}"
    integrations = aws("apigatewayv2", "get-integrations", "--api-id", API_ID)["Items"]
    integ = next((i for i in integrations if i.get("IntegrationUri") == fn_arn), None)
    if integ:
        integ_id = integ["IntegrationId"]
        print("  integration: exists")
    else:
        integ_id = aws("apigatewayv2", "create-integration", "--api-id", API_ID, "--integration-type", "AWS_PROXY",
                       "--integration-uri", fn_arn, "--payload-format-version", "2.0")["IntegrationId"]
        print("  integration: created")

    existing = {r["RouteKey"]: r for r in aws("apigatewayv2", "get-routes", "--api-id", API_ID)["Items"]}
    for key in ROUTES:
        args = ["--api-id", API_ID, "--authorization-type", "JWT", "--authorizer-id", auth_id,
                "--target", f"integrations/{integ_id}"]
        if key in existing:
            aws("apigatewayv2", "update-route", "--route-id", existing[key]["RouteId"], *args)
            print(f"  route {key}: updated")
        else:
            aws("apigatewayv2", "create-route", "--route-key", key, *args)
            print(f"  route {key}: created")

    aws("lambda", "add-permission", "--function-name", API_FN, "--statement-id", "apigateway-checks",
        "--action", "lambda:InvokeFunction", "--principal", "apigateway.amazonaws.com",
        "--source-arn", f"arn:aws:execute-api:{REGION}:{account}:{API_ID}/*/*/checks*",
        ok_codes=("ResourceConflictException",))
    print("  permission for API Gateway to call the Check API: ok")


def main():
    for z in ("check_api", "check_worker"):
        if not (HERE / "dist" / f"{z}.zip").exists():
            sys.exit(f"Missing dist/{z}.zip. Run build.py first.")
    account = aws("sts", "get-caller-identity")["Account"]
    pool, client = cognito_ids()
    table = lambda n: f"arn:aws:dynamodb:{REGION}:{account}:table/{n}"  # noqa: E731
    worker_arn = f"arn:aws:lambda:{REGION}:{account}:function:{WORKER_FN}"

    print("DynamoDB")
    ensure_table(CHECKS_TABLE, ["userId", "checkId"])
    ensure_table(CACHE_TABLE, ["imageHash"])

    print("IAM")
    photos = f"arn:aws:s3:::{BUCKET}/*"
    api_role = ensure_role("cloudlens-check-api-role", [
        {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem",
                                       "dynamodb:Query"], "Resource": table(CHECKS_TABLE)},
        {"Effect": "Allow", "Action": ["dynamodb:GetItem"], "Resource": table(CACHE_TABLE)},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": photos},
        {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": worker_arn},
    ])
    worker_role = ensure_role("cloudlens-check-worker-role", [
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Resource": table(CHECKS_TABLE)},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem"], "Resource": table(CACHE_TABLE)},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": photos},
        {"Effect": "Allow", "Action": ["textract:DetectDocumentText"], "Resource": "*"},
    ])

    print("Lambda")
    common = {"CHECKS_TABLE": CHECKS_TABLE, "CACHE_TABLE": CACHE_TABLE, "BUCKET_NAME": BUCKET}
    ensure_function(WORKER_FN, worker_role, HERE / "dist" / "check_worker.zip",
                    {**common, "T_TRUE": "70", "T_FALSE": "30", "MAX_RESULTS": "5", "EVIDENCE_ENABLED": "false",
                     "MODEL_ID": MODEL_ID}, timeout=60, memory=512)
    ensure_function(API_FN, api_role, HERE / "dist" / "check_api.zip",
                    {**common, "WORKER_FUNCTION": WORKER_FN}, timeout=10, memory=256)

    print("API Gateway")
    ensure_api(account, pool, client)
    print("Done.")


if __name__ == "__main__":
    main()
