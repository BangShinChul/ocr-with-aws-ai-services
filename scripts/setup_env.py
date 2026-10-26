"""
Preflight check and configuration for the OCR PoC demo.

Verifies that the chosen AWS profile can reach every service the demo uses,
optionally creates a private S3 bucket (only needed for PDF and BDA),
and writes the result to .env so ./run.sh picks it up.

    python scripts/setup_env.py --profile my-poc --region ap-northeast-2
    python scripts/setup_env.py --profile my-poc --region us-east-1 --create-bucket
    python scripts/setup_env.py --check-only            # re-run checks with the current .env

Nothing is created in the AWS account unless --create-bucket is given, and even then
the plan is shown and confirmed first (skip the prompt with --yes).
"""
import argparse
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
sys.path.insert(0, str(ROOT))

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound
except ImportError:
    sys.exit("boto3 is not installed. Run ./setup.sh (or setup.ps1) instead of calling this script directly.")

MODEL_FAMILIES = ["anthropic.claude-sonnet-5-5", "anthropic.claude-haiku-5-5", "anthropic.claude-opus-5-5"]
REQUIRED_MODEL = "anthropic.claude-sonnet-5-5"
MANAGED_KEYS = ["AWS_PROFILE", "AWS_REGION", "OCR_MODEL_SCOPE", "OCR_S3_BUCKET", "OCR_BDA_ENABLED"]
OK, WARN, FAIL = "  [OK]  ", "  [!]   ", "  [X]   "


def read_env():
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def write_env(updates):
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else [
        "# Written by scripts/setup_env.py. Non-empty values here take precedence over shell variables."]
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def err_code(e):
    return e.response.get("Error", {}).get("Code", "") if isinstance(e, ClientError) else type(e).__name__


def tiny_png():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (220, 70), "white")
    ImageDraw.Draw(img).text((20, 25), "PREFLIGHT OK 0723", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def check_models(session, region, scope, ping):
    bedrock = session.client("bedrock", region_name=region)
    runtime = session.client("bedrock-runtime", region_name=region)
    try:
        profiles, token = set(), None
        while True:
            kw = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 100}
            if token:
                kw["nextToken"] = token
            page = bedrock.list_inference_profiles(**kw)
            profiles |= {p["inferenceProfileId"] for p in page.get("inferenceProfileSummaries", [])}
            token = page.get("nextToken")
            if not token:
                break
    except ClientError as e:
        print(f"{WARN}Could not list inference profiles ({err_code(e)}); trying the models directly")
        profiles = None

    usable = {}
    for family in MODEL_FAMILIES:
        model_id = f"{scope}.{family}"
        if profiles is not None and model_id not in profiles:
            print(f"{FAIL}{model_id}: not offered with scope '{scope}' in {region}")
            usable[family] = False
            continue
        if not ping:
            print(f"{OK}{model_id}: listed (not called, --skip-model-check)")
            usable[family] = True
            continue
        try:
            runtime.converse(modelId=model_id, messages=[{"role": "user", "content": [{"text": "Reply OK"}]}],
                             inferenceConfig={"maxTokens": 5})
            print(f"{OK}{model_id}: callable")
            usable[family] = True
        except ClientError as e:
            code = err_code(e)
            hint = {"AccessDeniedException": "IAM permission or model access not granted",
                    "ResourceNotFoundException": "model not available to this account",
                    "ValidationException": "model id not valid in this Region"}.get(code, "")
            print(f"{FAIL}{model_id}: {code} {('- ' + hint) if hint else ''}")
            usable[family] = False
    return usable


def check_textract(session, region):
    try:
        resp = session.client("textract", region_name=region).detect_document_text(Document={"Bytes": tiny_png()})
        words = [b["Text"] for b in resp.get("Blocks", []) if b.get("BlockType") == "WORD"]
        print(f"{OK}Amazon Textract: callable (read: {' '.join(words) or 'no text'})")
        return True
    except (ClientError, BotoCoreError) as e:
        print(f"{FAIL}Amazon Textract: {err_code(e)}")
        return False


def check_bda(session, region):
    try:
        session.client("bedrock-data-automation", region_name=region).list_data_automation_projects(maxResults=1)
        print(f"{OK}Bedrock Data Automation: available in {region}")
        return True
    except (ClientError, BotoCoreError) as e:
        print(f"{WARN}Bedrock Data Automation: {err_code(e)} - not available in {region} or no permission. "
              "BDA will be disabled; Textract and Bedrock still work.")
        return False


def check_bucket(session, region, bucket):
    s3 = session.client("s3", region_name=region)
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        print(f"{FAIL}S3 bucket {bucket}: {err_code(e)} (does not exist or not accessible with this profile)")
        return False
    location = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint") or "us-east-1"
    if location != region:
        print(f"{FAIL}S3 bucket {bucket} is in {location}; use a bucket in {region}")
        return False
    try:
        pab = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        if not all(pab.values()):
            print(f"{WARN}S3 bucket {bucket}: Block Public Access is not fully on; turn it on before uploading documents")
    except ClientError:
        print(f"{WARN}S3 bucket {bucket}: no bucket-level Block Public Access setting found")
    print(f"{OK}S3 bucket {bucket} ({region})")
    return True


def create_bucket(session, region, bucket, expire_days, assume_yes):
    print(f"""
  The following will be created in the AWS account:
    S3 bucket          {bucket}  ({region})
    Public access      blocked (all four Block Public Access settings)
    Object ownership   BucketOwnerEnforced (ACLs disabled)
    Encryption         SSE-S3 (default)
    Bucket policy      deny any request not using HTTPS
    Lifecycle          delete objects after {expire_days} day(s)
    Tag                Project=ocr-poc
  No website hosting, no public URL. Delete later with:  aws s3 rb s3://{bucket} --force""")
    if not assume_yes and input("  Create it? [y/N] ").strip().lower() not in ("y", "yes"):
        print(f"{WARN}Bucket not created")
        return False
    s3 = session.client("s3", region_name=region)
    kw = {"Bucket": bucket}
    if region != "us-east-1":
        kw["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kw)
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_ownership_controls(Bucket=bucket, OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [{"Key": "Project", "Value": "ocr-poc"}]})
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [{
        "ID": f"expire-{expire_days}d", "Status": "Enabled", "Filter": {},
        "Expiration": {"Days": expire_days}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]})
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
        "Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}))
    print(f"{OK}Created private bucket {bucket}")
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", help="AWS CLI profile name (omit to use the default credential chain)")
    p.add_argument("--region", help="AWS Region, e.g. ap-northeast-2")
    p.add_argument("--model-scope", help="Bedrock inference profile prefix: global (default), us, eu, apac, ...")
    p.add_argument("--bucket", help="Existing private S3 bucket to use for PDF/BDA")
    p.add_argument("--create-bucket", action="store_true", help="Create a private bucket (name from --bucket or generated)")
    p.add_argument("--bucket-expire-days", type=int, default=7)
    p.add_argument("--skip-model-check", action="store_true", help="Do not call the models (no Bedrock charges)")
    p.add_argument("--check-only", action="store_true", help="Run checks with current settings; do not write .env")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation")
    a = p.parse_args()

    current = read_env()
    # Same precedence as the app: command line > .env > shell environment.
    profile = a.profile if a.profile is not None else current.get("AWS_PROFILE") or os.environ.get("AWS_PROFILE") or None
    region = a.region or current.get("AWS_REGION") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    scope = a.model_scope or current.get("OCR_MODEL_SCOPE") or "global"
    bucket = a.bucket if a.bucket is not None else current.get("OCR_S3_BUCKET", "")
    if not region:
        sys.exit("Region is required: --region ap-northeast-2 (or another Region)")

    print("\n== AWS identity")
    try:
        session = boto3.session.Session(profile_name=profile, region_name=region)
        ident = session.client("sts").get_caller_identity()
    except ProfileNotFound:
        sys.exit(f"{FAIL}Profile '{profile}' not found. Check `aws configure list-profiles`.")
    except (NoCredentialsError, ClientError, BotoCoreError) as e:
        sys.exit(f"{FAIL}Could not authenticate ({err_code(e)}). For SSO profiles run `aws sso login --profile {profile}`.")
    account = ident["Account"]
    print(f"{OK}Profile: {profile or '(default chain)'}  Account: {account}  Region: {region}")
    print(f"        Caller: {ident['Arn']}")

    print(f"\n== Amazon Bedrock models (scope: {scope})")
    usable = check_models(session, region, scope, ping=not a.skip_model_check)
    print("\n== Amazon Textract")
    textract_ok = check_textract(session, region)
    print("\n== Bedrock Data Automation")
    bda_ok = check_bda(session, region)

    print("\n== S3 bucket (only for PDF input and BDA)")
    bucket_ok = False
    if a.create_bucket and not a.check_only:
        bucket = bucket or f"ocr-poc-{account}-{region}"
        try:
            exists = check_bucket(session, region, bucket)
        except ClientError:
            exists = False
        bucket_ok = exists or create_bucket(session, region, bucket, a.bucket_expire_days, a.yes)
    elif bucket:
        bucket_ok = check_bucket(session, region, bucket)
    else:
        print(f"{WARN}No bucket configured. Images work without one; PDF and BDA need --bucket or --create-bucket.")

    for sub in ("images", "schema", "truth"):
        (ROOT / "data" / sub).mkdir(parents=True, exist_ok=True)

    if not a.check_only:
        write_env({
            "AWS_PROFILE": profile or "",
            "AWS_REGION": region,
            "OCR_MODEL_SCOPE": scope,
            "OCR_S3_BUCKET": bucket if bucket_ok else "",
            "OCR_BDA_ENABLED": "true" if (bda_ok and bucket_ok) else "false",
        })
        print(f"\nSaved settings to {ENV_FILE.name}")

    print("\n== Summary")
    print(f"  Bedrock (image reading) : {'ready' if usable.get(REQUIRED_MODEL) else 'NOT READY'}")
    print(f"  Textract                : {'ready' if textract_ok else 'NOT READY'}")
    print(f"  PDF via Textract        : {'ready' if bucket_ok else 'needs a bucket'}")
    print(f"  BDA                     : {'ready' if bda_ok and bucket_ok else ('needs a bucket' if bda_ok else 'unavailable here')}")
    if not usable.get(REQUIRED_MODEL):
        print("\n  Claude Sonnet 5.5 is required. Check model access in the Amazon Bedrock console,\n"
              "  the IAM policy (docs/iam-policy-example.json), or try another --model-scope.")
        sys.exit(1)
    print("\nNext: put documents in data/images, then  ./run.sh  (Windows: .\\run.ps1)")


if __name__ == "__main__":
    main()
