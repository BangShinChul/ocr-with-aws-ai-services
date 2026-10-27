"""Review client: confirmed values go to DynamoDB, the task token carries only {"confirmed": true}."""
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

pytest.importorskip("gradio", reason="run with the demo app venv: ../.venv/bin/python -m pytest infra/tests")
import pipeline_client as pc  # noqa: E402


class Table:
    def __init__(self, item):
        self.item, self.updates = item, []

    def get_item(self, Key):
        return {"Item": dict(self.item)}

    def update_item(self, **kw):
        if kw.get("ConditionExpression") and self.item["status"] != "PENDING":
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.updates.append(kw)
        self.item["status"] = kw["ExpressionAttributeValues"][":c" if ":c" in kw["ExpressionAttributeValues"] else ":s"]


class Clients:
    def __init__(self):
        self.sfn_calls = []

    def __call__(self, name, *_, **__):
        outer = self

        class C:
            def get_caller_identity(self):
                return {"Arn": "arn:aws:sts::111122223333:assumed-role/Reviewer/someone"}

            def send_task_success(self, **kw):
                outer.sfn_calls.append(("success", kw))

            def send_task_failure(self, **kw):
                outer.sfn_calls.append(("failure", kw))
        return C()


@pytest.fixture
def env(monkeypatch):
    table = Table({"doc_id": "d1", "status": "PENDING", "task_token": "TOKEN-1",
                   "rows": [{"필드": "crew[0].name_kr", "추출 값": "최종현", "확정 값": "최종현"}]})
    clients = Clients()
    monkeypatch.setattr(pc, "outputs", lambda: {"ReviewTable": "reviews", "DocumentBucket": "docs"})
    monkeypatch.setattr(pc, "get_aws_client", clients)
    monkeypatch.setattr(pc, "get_aws_resource", lambda _: type("R", (), {"Table": lambda self, n: table})())
    return table, clients


def test_approve_keeps_personal_data_out_of_the_task_output(env):
    table, clients = env
    rows = pc.apply_edits(table.item["rows"], ["crew[0].name_kr=최중현"])
    pc.approve("d1", rows, "reviewer@example.com")
    values = table.updates[0]["ExpressionAttributeValues"]
    assert values[":c"] == "CONFIRMED" and values[":r"][0]["확정 값"] == "최중현"
    assert values[":a"].endswith("/Reviewer/someone")
    kind, call = clients.sfn_calls[0]
    assert kind == "success" and json.loads(call["output"]) == {"confirmed": True}
    assert "최중현" not in call["output"]


def test_second_decision_is_refused(env):
    pc.approve("d1", env[0].item["rows"], "a")
    env[0].item["task_token"] = "TOKEN-1"  # still present until store runs
    with pytest.raises(pc.PipelineError):
        pc.approve("d1", env[0].item["rows"], "b")


def test_reject_sends_a_generic_cause(env):
    table, clients = env
    pc.reject("d1", "스캔 불량, 최중현 기장 서명 누락")
    kind, call = clients.sfn_calls[0]
    assert kind == "failure" and "최중현" not in call["cause"]
    assert table.updates[0]["ExpressionAttributeValues"][":r"].startswith("스캔 불량")
