---
name: ocr-poc-setup
description: Set up and run this OCR PoC demo in the user's own AWS account with an AWS profile they name - preflight checks, optional private S3 bucket, schema and truth files for their scanned documents, batch accuracy report, and the local review UI. Use when the user wants to install, configure, run, or evaluate this demo. Korean triggers - OCR 데모 설치, 정확도 측정, 데모 실행, 고객 계정 설정.
---

# OCR PoC setup

Read `AGENTS.md` at the repository root and follow its **Procedure** section step by step.
It is the single source of truth; this skill only makes it discoverable as a slash command.

Arguments from the user, if any (profile, Region, file names): $ARGUMENTS

Key gates (details in AGENTS.md):
1. Use only the AWS profile the user names; confirm the account ID with them before anything else.
2. Create the S3 bucket only after the user approves the printed plan.
3. Truth values are filled by the user's staff from the original documents, never from model output.
4. Keep the UI on 127.0.0.1.
