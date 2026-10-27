"""
Client for the deployed serverless pipeline (infra/): upload scans, check status, and answer
human-review requests. Runs locally with your own IAM credentials; nothing is exposed.

    ./run.sh pipeline schema  data/schema/flight-log.json --type flight-log
    ./run.sh pipeline upload  data/images/scan_01.png      --type flight-log
    ./run.sh pipeline status
    ./run.sh pipeline pending
    ./run.sh pipeline show    <doc_id>
    ./run.sh pipeline approve <doc_id> --set crew[0].name_kr=최중현 --reviewer you@example.com
    ./run.sh pipeline reject  <doc_id> --reason "unreadable scan"
    ./run.sh pipeline result  <doc_id>

The stack name comes from OCR_PIPELINE_STACK in .env (default: OcrPipeline).
"""
import shared.env  # noqa: F401  (load .env first)

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared.aws_client import get_aws_client, get_aws_session

STACK = os.environ.get("OCR_PIPELINE_STACK", "OcrPipeline")
_DOC_TYPE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class PipelineError(Exception):
    pass


def get_aws_resource(service_name):
    return get_aws_session().resource(service_name)


@lru_cache(maxsize=1)
def outputs() -> dict:
    """Bucket and table names from the CloudFormation stack outputs."""
    try:
        stack = get_aws_client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]
    except Exception as e:  # noqa: BLE001
        raise PipelineError(f"Stack '{STACK}' not found in this account/Region. Deploy infra/ first "
                            f"or set OCR_PIPELINE_STACK. ({e})") from e
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _plain(value):
    """DynamoDB Decimals -> int/float so values print and round-trip as JSON."""
    return json.loads(json.dumps(value, default=lambda d: int(d) if d == int(d) else float(d)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_ddb(value):
    """DynamoDB rejects floats; round-trip through JSON with Decimal."""
    return json.loads(json.dumps(value, ensure_ascii=False), parse_float=Decimal)


def _doc_type(doc_type: str) -> str:
    if not _DOC_TYPE.match(doc_type or ""):
        raise PipelineError("Document type must be 1-64 letters, digits, '-' or '_'.")
    return doc_type


def put_schema(path: str, doc_type: str) -> str:
    text = Path(path).read_text(encoding="utf-8")
    json.loads(text)  # fail early on invalid JSON
    key = f"schemas/{_doc_type(doc_type)}.json"
    get_aws_client("s3").put_object(Bucket=outputs()["DocumentBucket"], Key=key, Body=text.encode("utf-8"),
                                    ContentType="application/json")
    return key


def upload(path: str, doc_type: str) -> str:
    p = Path(path)
    if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".pdf"):
        raise PipelineError("Use PNG, JPEG or single-page PDF.")
    key = f"incoming/{_doc_type(doc_type)}/{p.name}"
    get_aws_client("s3").upload_file(str(p), outputs()["DocumentBucket"], key)
    return key


def status(limit: int = 20) -> list:
    """Most recent documents (a scan is fine at PoC volume)."""
    table = get_aws_resource("dynamodb").Table(outputs()["ResultsTable"])
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    items.sort(key=lambda i: i.get("received_at", ""), reverse=True)
    return _plain(items[:limit])


def pending() -> list:
    table = get_aws_resource("dynamodb").Table(outputs()["ReviewTable"])
    resp = table.query(IndexName="status-created_at", KeyConditionExpression=Key("status").eq("PENDING"),
                       ScanIndexForward=True)
    return _plain(resp["Items"])


def get_review(doc_id: str) -> dict:
    item = get_aws_resource("dynamodb").Table(outputs()["ReviewTable"]).get_item(Key={"doc_id": doc_id}).get("Item")
    if not item:
        raise PipelineError(f"No review request for {doc_id}.")
    return _plain(item)


def approve(doc_id: str, rows: list, reviewer: str) -> None:
    """Save the reviewer's values, then resume the execution.

    The values go to the review table, not into SendTaskSuccess, so personal data stays out of
    the Step Functions execution history. The caller's IAM ARN is recorded next to the name the
    reviewer typed; CloudTrail also logs who called SendTaskSuccess.
    """
    item = get_review(doc_id)
    if item.get("status") != "PENDING" or not item.get("task_token"):
        raise PipelineError(f"{doc_id} is not waiting for review (status {item.get('status')}).")
    caller = get_aws_client("sts").get_caller_identity()["Arn"]
    clean = json.loads(json.dumps(rows, ensure_ascii=False, default=str))
    try:
        get_aws_resource("dynamodb").Table(outputs()["ReviewTable"]).update_item(
            Key={"doc_id": doc_id},
            UpdateExpression="SET #s = :c, confirmed_rows = :r, reviewer = :n, reviewer_arn = :a, confirmed_at = :t",
            ConditionExpression="#s = :p",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":c": "CONFIRMED", ":p": "PENDING", ":r": _to_ddb(clean),
                                       ":n": reviewer, ":a": caller, ":t": _now()})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise PipelineError(f"{doc_id} was already decided by someone else.") from e
        raise
    get_aws_client("stepfunctions").send_task_success(taskToken=item["task_token"],
                                                      output=json.dumps({"confirmed": True}))


def reject(doc_id: str, reason: str) -> None:
    item = get_review(doc_id)
    if item.get("status") != "PENDING" or not item.get("task_token"):
        raise PipelineError(f"{doc_id} is not waiting for review (status {item.get('status')}).")
    caller = get_aws_client("sts").get_caller_identity()["Arn"]
    try:
        get_aws_resource("dynamodb").Table(outputs()["ReviewTable"]).update_item(
            Key={"doc_id": doc_id},
            UpdateExpression="SET #s = :s, reviewer_arn = :a, decided_at = :t, reject_reason = :r REMOVE task_token",
            ConditionExpression="#s = :p", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "REJECTED", ":p": "PENDING", ":a": caller, ":t": _now(),
                                       ":r": reason[:500]})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise PipelineError(f"{doc_id} was already decided by someone else.") from e
        raise
    # The reason stays in the review table; the execution history gets a generic cause.
    get_aws_client("stepfunctions").send_task_failure(taskToken=item["task_token"], error="ReviewerRejected",
                                                      cause="Rejected by reviewer (see review table)")


def result(doc_id: str) -> dict:
    body = get_aws_client("s3").get_object(Bucket=outputs()["DocumentBucket"], Key=f"results/{doc_id}.json")["Body"]
    return json.loads(body.read())


def apply_edits(rows: list, edits: list) -> list:
    """`field=value` pairs -> 확정 값 on the matching review rows."""
    by_field = {r["필드"]: r for r in rows}
    for edit in edits or []:
        field, sep, value = edit.partition("=")
        if not sep or field not in by_field:
            raise PipelineError(f"'{edit}': use field=value with a field from `show` ({', '.join(by_field)}).")
        by_field[field]["확정 값"] = value
    return rows


# ---------------------------------------------------------------- CLI

def _print_rows(rows):
    for r in rows:
        print(f"  {r.get('우선순위', ''):<3} {r['필드']:<34} {str(r.get('추출 값')):<18} {r.get('사유', '')}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("schema", "upload"):
        s = sub.add_parser(name)
        s.add_argument("path")
        s.add_argument("--type", required=True, help="Document type = folder under incoming/ and schemas/<type>.json")
    sub.add_parser("status").add_argument("--limit", type=int, default=20)
    sub.add_parser("pending")
    for name in ("show", "result"):
        sub.add_parser(name).add_argument("doc_id")
    s = sub.add_parser("approve")
    s.add_argument("doc_id")
    s.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
    s.add_argument("--reviewer", default=os.environ.get("USER", "reviewer"))
    s = sub.add_parser("reject")
    s.add_argument("doc_id")
    s.add_argument("--reason", required=True)
    args = ap.parse_args(argv)

    try:
        if args.cmd == "schema":
            print("uploaded", put_schema(args.path, args.type))
        elif args.cmd == "upload":
            print("uploaded", upload(args.path, args.type), "- the pipeline starts within a few seconds")
        elif args.cmd == "status":
            for i in status(args.limit):
                print(f"{i.get('received_at', ''):<26} {i['doc_id']:<40} {i.get('status', ''):<15} "
                      f"flagged={i.get('flagged', '-')} corrections={i.get('corrections', '-')} {i.get('error', '')[:80]}")
        elif args.cmd == "pending":
            items = pending()
            print(f"{len(items)} document(s) waiting for review")
            for i in items:
                print(f"  {i['created_at']}  {i['doc_id']:<40} {i.get('flagged')} of {i.get('fields')} fields flagged")
        elif args.cmd == "show":
            item = get_review(args.doc_id)
            print(f"{item['doc_id']}  status={item['status']}  file={item.get('filename')}")
            _print_rows(item.get("rows", []))
        elif args.cmd == "approve":
            rows = apply_edits(get_review(args.doc_id).get("rows", []), args.set)
            approve(args.doc_id, rows, args.reviewer)
            print(f"approved {args.doc_id}; the result is written to results/{args.doc_id}.json")
        elif args.cmd == "reject":
            reject(args.doc_id, args.reason)
            print(f"rejected {args.doc_id}")
        elif args.cmd == "result":
            print(json.dumps(result(args.doc_id), ensure_ascii=False, indent=2))
    except PipelineError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
