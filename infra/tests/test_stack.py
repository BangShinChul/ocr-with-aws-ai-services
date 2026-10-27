"""Synth-level checks: workflow shape and the no-public-exposure guarantees."""
import json
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import stage_lambda_asset  # noqa: E402
from ocr_pipeline.stack import OcrPipelineStack  # noqa: E402


def template() -> Template:
    # Same feature flags as `cdk synth` (cdk.json), e.g. bucket-policy based access logging.
    context = json.loads((Path(__file__).resolve().parent.parent / "cdk.json").read_text())["context"]
    app = cdk.App(context=context)
    stack = OcrPipelineStack(app, "Test", asset_dir=str(stage_lambda_asset()),
                             model_id="global.anthropic.claude-sonnet-5-5", doc_type_prompt="form",
                             confidence_threshold="90", review_reasons="규칙 위반", review_ttl_days=7,
                             retain_data=False)
    return Template.from_stack(stack)


T = template()


def test_no_public_entry_points():
    for t in ("AWS::ApiGateway::RestApi", "AWS::ApiGatewayV2::Api", "AWS::Lambda::Url",
              "AWS::CloudFront::Distribution", "AWS::ElasticLoadBalancingV2::LoadBalancer",
              "AWS::EC2::SecurityGroup", "AWS::EC2::EIP", "AWS::EC2::Instance"):
        T.resource_count_is(t, 0)
    assert "0.0.0.0/0" not in json.dumps(T.to_json())


def test_buckets_block_public_access_and_require_tls():
    T.all_resources_properties("AWS::S3::Bucket", {"PublicAccessBlockConfiguration": {
        "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True}})
    T.has_resource_properties("AWS::S3::BucketPolicy", {"PolicyDocument": {"Statement": Match.array_with([
        Match.object_like({"Effect": "Deny", "Condition": {"Bool": {"aws:SecureTransport": "false"}}})])}})


def test_workflow_waits_for_reviewer_task_token():
    sm = next(r for r in T.find_resources("AWS::StepFunctions::StateMachine").values())
    definition = json.dumps(sm["Properties"]["DefinitionString"])
    assert "lambda:invoke.waitForTaskToken" in definition
    assert "ReadInParallel" in definition and "NeedsReview" in definition


def test_model_permission_is_scoped_to_configured_model():
    policies = json.dumps(T.find_resources("AWS::IAM::Policy"))
    assert "inference-profile/global.anthropic.claude-sonnet-5-5" in policies
    actions = set()
    for policy in T.find_resources("AWS::IAM::Policy").values():
        for st in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions.update([st["Action"]] if isinstance(st["Action"], str) else st["Action"])
    assert {a for a in actions if a.startswith("bedrock:")} == {"bedrock:InvokeModel"}
    assert not any(a.endswith(":*") or a == "*" for a in actions)


def test_model_reader_concurrency_is_capped_and_throttles_are_retried_first():
    fns = T.find_resources("AWS::Lambda::Function", {"Properties": {"Handler": "handlers.read_claude"}})
    (fn,) = fns.values()
    assert fn["Properties"]["ReservedConcurrentExecutions"] == 10
    sm = next(r for r in T.find_resources("AWS::StepFunctions::StateMachine").values())
    parts = sm["Properties"]["DefinitionString"]["Fn::Join"][1]
    definition = json.loads("".join(p if isinstance(p, str) else "TOKEN" for p in parts))
    branch = next(b for b in definition["States"]["ReadInParallel"]["Branches"] if b["StartAt"] == "ReadClaudeStep")
    retries = [r["ErrorEquals"] for r in branch["States"]["ReadClaudeStep"]["Retry"]]
    assert retries[0] == ["Lambda.TooManyRequestsException"]
    assert sum("Lambda.TooManyRequestsException" in r for r in retries) == 1


def test_store_can_read_the_confirmed_review():
    policies = json.dumps(T.find_resources("AWS::IAM::Policy"))
    assert "dynamodb:GetItem" in policies


def _definition():
    sm = next(r for r in T.find_resources("AWS::StepFunctions::StateMachine").values())
    parts = sm["Properties"]["DefinitionString"]["Fn::Join"][1]
    return json.loads("".join(p if isinstance(p, str) else "TOKEN" for p in parts))


def test_preprocess_receives_the_whole_pipes_batch():
    params = _definition()["States"]["PreprocessStep"].get("Parameters", {})
    # Either no Parameters (whole input) or an explicit path; never the literal string "$".
    assert params.get("Payload") != "$"
    assert params.get("Payload.$", "$") == "$"
