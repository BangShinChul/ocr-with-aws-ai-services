#!/usr/bin/env python3
"""CDK app: serverless OCR pipeline (see README.md in this folder)."""
import shutil
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from ocr_pipeline.stack import OcrPipelineStack

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def stage_lambda_asset() -> Path:
    """Copy the handlers and the two dependency-free shared modules into one asset folder.

    No Docker and no pip install: the Lambda Python runtime already ships boto3.
    """
    out = HERE / ".build" / "lambda"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy2(HERE / "lambda" / "handlers.py", out / "handlers.py")
    for name in ("review.py", "prompts.py"):
        shutil.copy2(REPO / "shared" / name, out / name)
    return out


def main() -> None:
    app = cdk.App()
    ctx = app.node.try_get_context

    stack = OcrPipelineStack(
        app,
        ctx("stackName") or "OcrPipeline",
        asset_dir=str(stage_lambda_asset()),
        model_id=ctx("modelId") or "global.anthropic.claude-sonnet-5-5",
        doc_type_prompt=ctx("docTypePrompt") or "form",
        confidence_threshold=str(ctx("confidenceThreshold") or "90"),
        review_reasons=ctx("reviewReasons") or "규칙 위반,저신뢰,OCR 근거 없음",
        review_ttl_days=int(ctx("reviewTtlDays") or 7),
        retain_data=str(ctx("retainData") or "false").lower() == "true",
        read_concurrency=int(ctx("readConcurrency") if ctx("readConcurrency") is not None else 10),
        description="Serverless OCR pipeline: S3 -> EventBridge -> SQS -> Pipes -> Step Functions "
                    "(Bedrock + Textract, human review via task token). No public endpoints.",
    )
    cdk.Tags.of(stack).add("Project", ctx("projectTag") or "trinity-ocr-poc")
    cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))
    app.synth()


if __name__ == "__main__":
    main()
