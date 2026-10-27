"""
Lambda handlers for the OCR pipeline (one asset, one handler per Step Functions task).

Flow:  preprocess -> [read_claude || read_textract] -> validate
         -> store                       (nothing flagged)
         -> enqueue_review (task token) -> store   (reviewer confirmed values)

Only boto3 (Lambda runtime) and two dependency-free modules copied from the repo
(shared/review.py, shared/prompts.py) are used, so the asset needs no build step.
Large payloads (model output, Textract blocks) are kept in S3 under work/<doc_id>/;
the state machine passes only small references.
"""
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.config import Config

from prompts import OCR_SYSTEM_PROMPT, get_json_formatting_instructions, get_prompt_for_document_type
from review import apply_corrections, build_review_rows, flatten

log = logging.getLogger()
log.setLevel(logging.INFO)

BUCKET = os.environ.get("DOCUMENT_BUCKET", "")
RESULTS_TABLE = os.environ.get("RESULTS_TABLE", "")
REVIEW_TABLE = os.environ.get("REVIEW_TABLE", "")
MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-5-5")
DOC_TYPE_PROMPT = os.environ.get("DOC_TYPE_PROMPT", "form")
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "90"))
# Which signals send a document to a person. Hangul values are excluded by default:
# Textract cannot read Hangul, so that signal would flag every Korean name.
REVIEW_REASONS = [r.strip() for r in os.environ.get(
    "REVIEW_REASONS", "규칙 위반,저신뢰,OCR 근거 없음").split(",") if r.strip()]
REVIEW_TTL_DAYS = int(os.environ.get("REVIEW_TTL_DAYS", "7"))
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "OcrPipeline")

INCOMING_PREFIX = "incoming/"
DOC_TYPE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_IMAGE_BYTES = 3_750_000          # Bedrock Converse image limit
MAX_DOCUMENT_BYTES = 4_500_000       # Bedrock Converse document limit
FORMATS = {".png": ("image", "png"), ".jpg": ("image", "jpeg"), ".jpeg": ("image", "jpeg"),
           ".pdf": ("document", "pdf")}

# Adaptive retries absorb Bedrock/Textract throttling; Step Functions retries the rest.
_RETRY = Config(retries={"mode": "adaptive", "max_attempts": 6})
_s3 = boto3.client("s3")
_bedrock = boto3.client("bedrock-runtime", config=Config(retries={"mode": "adaptive", "max_attempts": 6},
                                                         read_timeout=300))
_textract = boto3.client("textract", config=_RETRY)
_ddb = boto3.resource("dynamodb")


class UnsupportedDocument(Exception):
    """Raised for inputs the pipeline cannot process; caught by the state machine."""


# ---------------------------------------------------------------- helpers

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _put_json(key, data):
    _s3.put_object(Bucket=BUCKET, Key=key, ContentType="application/json",
                   Body=json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
    return key


def _get_json(key):
    return json.loads(_s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())


def _get_text(key):
    try:
        return _s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    except _s3.exceptions.NoSuchKey:
        return None


def _to_ddb(value):
    """DynamoDB rejects floats; round-trip through JSON with Decimal."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str), parse_float=Decimal)


def _emit_metrics(doc_type, **values):
    """CloudWatch Embedded Metric Format: one log line, no PutMetricData call needed."""
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": METRIC_NAMESPACE, "Dimensions": [["DocType"]],
            "Metrics": [{"Name": k, "Unit": "Count"} for k in values]}]},
        "DocType": doc_type, **values}))


def _strip_fences(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text).strip()


# ---------------------------------------------------------------- steps

def preprocess(event, _context):
    """Input from EventBridge Pipes (SQS batch of 1) or a direct {"bucket","key"} test event."""
    record = event[0] if isinstance(event, list) else event
    if "body" in record:  # SQS record carrying the EventBridge "Object Created" event
        record = json.loads(record["body"])
    detail = record.get("detail", record)
    bucket = detail.get("bucket", {}).get("name") if isinstance(detail.get("bucket"), dict) else detail.get("bucket")
    key = detail.get("object", {}).get("key") if isinstance(detail.get("object"), dict) else detail.get("key")
    key = urllib.parse.unquote_plus(key or "")
    if bucket != BUCKET or not key.startswith(INCOMING_PREFIX):
        raise UnsupportedDocument(f"Unexpected object s3://{bucket}/{key}")

    rest = key[len(INCOMING_PREFIX):]
    doc_type, _, filename = rest.partition("/") if "/" in rest else ("default", "", rest)
    # The document type selects schemas/<doc_type>.json, so only plain names are accepted.
    if not DOC_TYPE_RE.match(doc_type) or not filename or "/" in filename:
        raise UnsupportedDocument("Upload to incoming/<doc_type>/<file> where doc_type uses only "
                                  "letters, digits, '-' or '_' (1-64 characters).")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in FORMATS:
        raise UnsupportedDocument(f"Unsupported file type '{ext}'. Use PNG, JPEG or single-page PDF.")
    head = _s3.head_object(Bucket=BUCKET, Key=key)
    size = head["ContentLength"]
    kind, fmt = FORMATS[ext]
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_DOCUMENT_BYTES
    if size > limit:
        raise UnsupportedDocument(f"File is {size / 1e6:.1f} MB; the limit for {fmt} is {limit / 1e6:.2f} MB. "
                                  "Export at a lower resolution (150-300 DPI) or as JPEG.")

    etag = head.get("ETag", "").strip('"')
    stem = re.sub(r"[^A-Za-z0-9_-]", "-", os.path.splitext(filename)[0])[:48] or "doc"
    doc_id = f"{stem}-{hashlib.sha1(f'{key}:{etag}'.encode()).hexdigest()[:10]}"
    schema_key = f"schemas/{doc_type}.json"
    has_schema = _get_text(schema_key) is not None
    item = {"doc_id": doc_id, "bucket": BUCKET, "key": key, "doc_type": doc_type, "filename": filename,
            "kind": kind, "format": fmt, "size": size, "schema_key": schema_key if has_schema else None}
    _ddb.Table(RESULTS_TABLE).put_item(Item=_to_ddb({**item, "status": "PROCESSING", "received_at": _now()}))
    # File names can contain personal data, so the log carries only the id and shape.
    log.info("preprocess doc_id=%s doc_type=%s kind=%s size=%d schema=%s",
             doc_id, doc_type, kind, size, bool(has_schema))
    return item


def read_claude(event, _context):
    doc = event["doc"]
    body = _s3.get_object(Bucket=BUCKET, Key=doc["key"])["Body"].read()
    schema = _get_text(doc["schema_key"]) if doc.get("schema_key") else None
    prompt = get_prompt_for_document_type(DOC_TYPE_PROMPT) + get_json_formatting_instructions(schema)
    block = ({"image": {"format": doc["format"], "source": {"bytes": body}}} if doc["kind"] == "image" else
             {"document": {"format": "pdf", "name": "document", "source": {"bytes": body}}})
    started = time.time()
    resp = _bedrock.converse(modelId=MODEL_ID, system=[{"text": OCR_SYSTEM_PROMPT}],
                             messages=[{"role": "user", "content": [{"text": prompt}, block]}],
                             # No temperature: Claude 5.5 models reject it (same request as the demo app).
                             inferenceConfig={"maxTokens": 8000})
    text = "".join(c.get("text", "") for c in resp["output"]["message"]["content"])
    try:
        data = json.loads(_strip_fences(text))
    except json.JSONDecodeError:
        data = {"_unparsed_text": text}
    usage = resp.get("usage", {})
    key = _put_json(f"work/{doc['doc_id']}/claude.json", data)
    return {"key": key, "model_id": MODEL_ID, "seconds": round(time.time() - started, 1),
            "input_tokens": usage.get("inputTokens", 0), "output_tokens": usage.get("outputTokens", 0),
            "parsed": "_unparsed_text" not in data}


def read_textract(event, _context):
    doc = event["doc"]
    resp = _textract.detect_document_text(Document={"S3Object": {"Bucket": BUCKET, "Name": doc["key"]}})
    blocks = [{k: b.get(k) for k in ("BlockType", "Text", "Confidence")}
              for b in resp.get("Blocks", []) if b.get("BlockType") in ("WORD", "LINE")]
    key = _put_json(f"work/{doc['doc_id']}/textract.json", blocks)
    words = [b for b in blocks if b["BlockType"] == "WORD"]
    low = sum(1 for b in words if (b.get("Confidence") or 0) < CONFIDENCE_THRESHOLD)
    return {"key": key, "words": len(words), "low_confidence_words": low,
            "pages": resp.get("DocumentMetadata", {}).get("Pages", 1)}


def validate(event, _context):
    doc, claude, textract = event["doc"], event["reads"][0], event["reads"][1]
    data = _get_json(claude["key"])
    blocks = _get_json(textract["key"])
    schema = _get_text(doc["schema_key"]) if doc.get("schema_key") else None
    rows = build_review_rows({"Claude": data}, "Claude", blocks, schema, CONFIDENCE_THRESHOLD)
    flagged = [r for r in rows if r["_kinds"] & set(REVIEW_REASONS)]
    clean_rows = [{k: v for k, v in r.items() if not k.startswith("_")} | {"kinds": sorted(r["_kinds"])}
                  for r in flagged]
    fields = [k for k, v in flatten(data).items() if v not in (None, "")]
    needs_review = bool(flagged) or not claude.get("parsed", True)
    _emit_metrics(doc["doc_type"], Documents=1, Fields=len(fields), FlaggedFields=len(flagged),
                  ReviewRequired=int(needs_review), InputTokens=claude.get("input_tokens", 0),
                  OutputTokens=claude.get("output_tokens", 0))
    key = _put_json(f"work/{doc['doc_id']}/review_rows.json", clean_rows)
    return {"needs_review": needs_review, "flagged": len(flagged), "fields": len(fields), "rows_key": key,
            "reasons": sorted({k for r in flagged for k in r["_kinds"]})}


def enqueue_review(event, _context):
    """Called with .waitForTaskToken: park the execution until a reviewer answers."""
    doc, check = event["doc"], event["check"]
    expires = datetime.now(timezone.utc) + timedelta(days=REVIEW_TTL_DAYS)
    _ddb.Table(REVIEW_TABLE).put_item(Item=_to_ddb({
        "doc_id": doc["doc_id"], "status": "PENDING", "task_token": event["token"],
        "created_at": _now(), "filename": doc["filename"], "doc_type": doc["doc_type"], "key": doc["key"],
        "result_key": event["claude"]["key"], "rows": _get_json(check["rows_key"]),
        "flagged": check["flagged"], "fields": check["fields"], "expires_at": int(expires.timestamp())}))
    _ddb.Table(RESULTS_TABLE).update_item(
        Key={"doc_id": doc["doc_id"]}, UpdateExpression="SET #s = :s, flagged = :f",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "REVIEW_PENDING", ":f": check["flagged"]})
    return {"queued": doc["doc_id"]}


def _confirmed_review(doc_id):
    """Reviewer values saved by the review client (kept out of the Step Functions history)."""
    item = _ddb.Table(REVIEW_TABLE).get_item(Key={"doc_id": doc_id}, ConsistentRead=True).get("Item") or {}
    if item.get("status") != "CONFIRMED":
        raise UnsupportedDocument(f"Review for {doc_id} is not confirmed (status {item.get('status')}).")
    return json.loads(json.dumps(item, default=lambda d: int(d) if d == int(d) else float(d)))


def store(event, _context):
    """Write the final result. `review.confirmed` is set when a person confirmed values."""
    doc = event["doc"]
    data = _get_json(event["claude"]["key"])
    review = {}
    if (event.get("review") or {}).get("confirmed"):
        item = _confirmed_review(doc["doc_id"])
        review = {"rows": item.get("confirmed_rows") or [], "reviewer": item.get("reviewer"),
                  "reviewer_arn": item.get("reviewer_arn"), "confirmed_at": item.get("confirmed_at")}
    corrections = []
    if review.get("rows"):
        data, corrections = apply_corrections(data, review["rows"])
    result = {"doc_id": doc["doc_id"], "source": {"bucket": BUCKET, "key": doc["key"]},
              "model_id": event["claude"].get("model_id"), "data": data,
              "review": {"required": bool(event.get("check", {}).get("needs_review")),
                         "reviewer": review.get("reviewer"), "reviewer_arn": review.get("reviewer_arn"),
                         "confirmed_at": review.get("confirmed_at"), "corrections": corrections},
              "tokens": {"input": event["claude"].get("input_tokens"), "output": event["claude"].get("output_tokens")},
              "completed_at": _now()}
    result_key = _put_json(f"results/{doc['doc_id']}.json", result)
    status = "REVIEWED" if review else "COMPLETED"
    _ddb.Table(RESULTS_TABLE).update_item(
        Key={"doc_id": doc["doc_id"]},
        UpdateExpression="SET #s = :s, result_key = :k, completed_at = :t, corrections = :c, flagged = :f",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=_to_ddb({":s": status, ":k": result_key, ":t": _now(),
                                           ":c": len(corrections), ":f": event.get("check", {}).get("flagged", 0)}))
    if review:
        _ddb.Table(REVIEW_TABLE).update_item(
            Key={"doc_id": doc["doc_id"]}, UpdateExpression="SET #s = :s, reviewed_at = :t REMOVE task_token",
            ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "DONE", ":t": _now()})
    return {"status": status, "result_key": result_key, "corrections": len(corrections)}


def record_failure(event, _context):
    doc = event.get("doc") or {}
    error = event.get("error") or {}
    cause = error.get("Cause", "")
    try:
        cause = json.loads(cause).get("errorMessage", cause)
    except (ValueError, AttributeError):
        pass
    if doc.get("doc_id"):
        _ddb.Table(RESULTS_TABLE).update_item(
            Key={"doc_id": doc["doc_id"]}, UpdateExpression="SET #s = :s, #e = :e, completed_at = :t",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={":s": "FAILED", ":e": f"{error.get('Error', '')}: {cause}"[:1000], ":t": _now()})
    _emit_metrics(doc.get("doc_type", "unknown"), Failures=1)
    return {"status": "FAILED", "error": error.get("Error"), "cause": cause[:1000]}
