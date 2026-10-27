"""
Unit tests for the pipeline Lambda handlers with in-memory fakes (no AWS calls).

Run:  cd infra && .venv/bin/python -m pytest -q
"""
import io
import json
import os
import sys
from pathlib import Path

import pytest
import re

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path[:0] = [str(HERE.parent / "lambda"), str(REPO / "shared")]
os.environ.update({"AWS_DEFAULT_REGION": "ap-northeast-2", "DOCUMENT_BUCKET": "docs",
                   "RESULTS_TABLE": "results", "REVIEW_TABLE": "reviews"})

import handlers  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}, "total": {"type": "integer"},
                   "name": {"type": "string"}},
    "x-validation": [{"name": "a + b = total", "type": "sum", "lhs": [["a", 1], ["b", 1]], "rhs": [["total", 1]]}],
}


RESERVED = {"error", "status", "name", "size", "key", "data", "type", "date", "time", "count"}


class FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **_):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Key]), "ETag": '"etag1"'}


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["doc_id"]] = dict(Item)

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get(Key["doc_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, ExpressionAttributeNames=None):
        item = self.items.setdefault(Key["doc_id"], dict(Key))
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_part, _, remove_part = UpdateExpression.partition(" REMOVE ")
        # Like DynamoDB, refuse reserved words used as bare attribute names.
        for name in re.findall(r"(?:SET |, |REMOVE )([A-Za-z_]+) ?=?", UpdateExpression):
            if name.lower() in RESERVED:
                raise ValueError(f"reserved keyword: {name}")
        for assign in set_part.removeprefix("SET ").split(","):
            k, v = (s.strip() for s in assign.split("="))
            item[names.get(k, k)] = values[v]
        for k in filter(None, (s.strip() for s in remove_part.split(","))):
            item.pop(k, None)


class FakeDynamo:
    def __init__(self):
        self.tables = {"results": FakeTable(), "reviews": FakeTable()}

    def Table(self, name):  # noqa: N802 (boto3 API name)
        return self.tables[name]


class FakeBedrock:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": "```json\n" + json.dumps(self.reply) + "\n```"}]}},
                "usage": {"inputTokens": 6220, "outputTokens": 943}}


class FakeTextract:
    def __init__(self, words):
        self.words = words

    def detect_document_text(self, Document):
        blocks = [{"BlockType": "WORD", "Text": w, "Confidence": c} for w, c in self.words]
        blocks.append({"BlockType": "LINE", "Text": " ".join(w for w, _ in self.words), "Confidence": 99})
        return {"Blocks": blocks, "DocumentMetadata": {"Pages": 1}}


@pytest.fixture
def aws(monkeypatch):
    s3, ddb = FakeS3(), FakeDynamo()
    monkeypatch.setattr(handlers, "_s3", s3)
    monkeypatch.setattr(handlers, "_ddb", ddb)
    monkeypatch.setattr(handlers, "BUCKET", "docs")
    monkeypatch.setattr(handlers, "RESULTS_TABLE", "results")
    monkeypatch.setattr(handlers, "REVIEW_TABLE", "reviews")
    s3.put_object(Bucket="docs", Key="schemas/flight-log.json", Body=json.dumps(SCHEMA))
    return s3, ddb


def pipes_event(key):
    """What EventBridge Pipes hands to the state machine: a batch with one SQS record."""
    body = {"detail-type": "Object Created", "source": "aws.s3",
            "detail": {"bucket": {"name": "docs"}, "object": {"key": key}}}
    return [{"messageId": "1", "body": json.dumps(body)}]


def run_reads(aws_fakes, monkeypatch, reply, words, key="incoming/flight-log/scan%2001.png"):
    s3, _ = aws_fakes
    s3.put_object(Bucket="docs", Key="incoming/flight-log/scan 01.png", Body=b"\x89PNG fake")
    monkeypatch.setattr(handlers, "_bedrock", FakeBedrock(reply))
    monkeypatch.setattr(handlers, "_textract", FakeTextract(words))
    doc = handlers.preprocess(pipes_event(key), None)
    reads = [handlers.read_claude({"doc": doc}, None), handlers.read_textract({"doc": doc}, None)]
    return doc, reads


def test_preprocess_parses_pipes_batch_and_records_document(aws, monkeypatch):
    doc, _ = run_reads(aws, monkeypatch, {"a": 1}, [("1", 99)])
    assert doc["key"] == "incoming/flight-log/scan 01.png"  # URL-decoded
    assert doc["doc_type"] == "flight-log" and doc["kind"] == "image" and doc["format"] == "png"
    assert doc["schema_key"] == "schemas/flight-log.json"
    assert doc["doc_id"].startswith("scan-01-")
    assert aws[1].tables["results"].items[doc["doc_id"]]["status"] == "PROCESSING"


@pytest.mark.parametrize("key,body,message", [
    ("incoming/x/notes.txt", b"hi", "Unsupported file type"),
    ("incoming/x/big.png", b"0" * 3_800_000, "limit"),
    ("other/x.png", b"0", "Unexpected object"),
    ("incoming/../schemas/x.png", b"0", "doc_type uses only"),
    ("incoming/a/b/c.png", b"0", "doc_type uses only"),
])
def test_preprocess_rejects_unsupported_input(aws, key, body, message):
    aws[0].put_object(Bucket="docs", Key=key, Body=body)
    with pytest.raises(handlers.UnsupportedDocument, match=message):
        handlers.preprocess(pipes_event(key), None)


def test_clean_document_is_stored_without_review(aws, monkeypatch):
    reply = {"a": 2, "b": 3, "total": 5}
    doc, reads = run_reads(aws, monkeypatch, reply, [("2", 99), ("3", 99), ("5", 99)])
    assert reads[0]["parsed"] and reads[0]["input_tokens"] == 6220
    assert "temperature" not in handlers._bedrock.calls[0]["inferenceConfig"]
    check = handlers.validate({"doc": doc, "reads": reads}, None)
    assert check["needs_review"] is False and check["flagged"] == 0

    out = handlers.store({"doc": doc, "claude": reads[0], "check": check, "review": {}}, None)
    assert out["status"] == "COMPLETED"
    result = json.loads(aws[0].objects[f"results/{doc['doc_id']}.json"])
    assert result["data"] == reply and result["review"]["corrections"] == []
    assert aws[1].tables["results"].items[doc["doc_id"]]["status"] == "COMPLETED"


def test_rule_violation_goes_to_review_and_reviewer_values_are_applied(aws, monkeypatch):
    reply = {"a": 2, "b": 3, "total": 6}  # 2 + 3 != 6
    doc, reads = run_reads(aws, monkeypatch, reply, [("2", 99), ("3", 99), ("6", 99)])
    check = handlers.validate({"doc": doc, "reads": reads}, None)
    assert check["needs_review"] is True and "규칙 위반" in check["reasons"]

    handlers.enqueue_review({"doc": doc, "check": check, "claude": reads[0], "token": "TOKEN-1"}, None)
    pending = aws[1].tables["reviews"].items[doc["doc_id"]]
    assert pending["status"] == "PENDING" and pending["task_token"] == "TOKEN-1"
    assert aws[1].tables["results"].items[doc["doc_id"]]["status"] == "REVIEW_PENDING"

    # Before the reviewer confirms, store must refuse to use the review.
    with pytest.raises(handlers.UnsupportedDocument, match="not confirmed"):
        handlers.store({"doc": doc, "claude": reads[0], "check": check, "review": {"confirmed": True}}, None)

    # What the local review client writes before calling SendTaskSuccess with {"confirmed": true}.
    rows = [dict(r) for r in pending["rows"]]
    for r in rows:
        if r["필드"] == "total":
            r["확정 값"] = "5"
    pending.update(status="CONFIRMED", confirmed_rows=rows, reviewer="reviewer@example.com",
                   reviewer_arn="arn:aws:sts::111122223333:assumed-role/Reviewer/someone",
                   confirmed_at="2026-10-27T05:00:00+00:00")
    out = handlers.store({"doc": doc, "claude": reads[0], "check": check, "review": {"confirmed": True}}, None)
    assert out == {"status": "REVIEWED", "result_key": f"results/{doc['doc_id']}.json", "corrections": 1}
    result = json.loads(aws[0].objects[out["result_key"]])
    assert result["data"]["total"] == 5
    assert result["review"]["corrections"][0]["before"] == 6
    assert result["review"]["reviewer_arn"].endswith("/Reviewer/someone")
    done = aws[1].tables["reviews"].items[doc["doc_id"]]
    assert done["status"] == "DONE" and "task_token" not in done


def test_unparsed_model_output_forces_review(aws, monkeypatch):
    doc, reads = run_reads(aws, monkeypatch, {}, [("x", 99)])
    reads[0]["parsed"] = False
    assert handlers.validate({"doc": doc, "reads": reads}, None)["needs_review"] is True


def test_record_failure_marks_document_failed(aws, monkeypatch):
    doc, _ = run_reads(aws, monkeypatch, {"a": 1}, [("1", 99)])
    error = {"Error": "ThrottlingException", "Cause": json.dumps({"errorMessage": "slow down"})}
    out = handlers.record_failure({"doc": doc, "error": error}, None)
    assert out["cause"] == "slow down"
    item = aws[1].tables["results"].items[doc["doc_id"]]
    assert item["status"] == "FAILED" and "slow down" in item["error"]


def test_record_failure_without_document(aws):
    assert handlers.record_failure({"error": {"Error": "UnsupportedDocument", "Cause": "{}"}}, None)["status"] == "FAILED"
