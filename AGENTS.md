# Agent guide: set up and run the OCR PoC demo

This file is read automatically by Codex, Kiro and other coding agents. Claude Code reads it
through `CLAUDE.md`. The `ocr-poc-setup` skill (`.kiro/skills/`, `.claude/skills/`) points here.

The user is usually a customer engineer who wants to test OCR accuracy on their own scanned
documents in **their own AWS account**. Reply in the user's language.

## What this repo is

A local web UI (`app.py`, Gradio) and a batch CLI (`evaluate.py`) that run the same document
through Amazon Textract, Amazon Bedrock (Claude, reading the image directly) and Bedrock Data
Automation, then compare each result with a human-verified truth file.

- Settings: `.env` (written by `scripts/setup_env.py`; see `.env.example`). Non-empty `.env` values
  override shell variables.
- Documents: `data/images/`, schemas: `data/schema/<name>.json`, answers: `data/truth/<name>.json`
  (same base name). `data/` and `results/` are git-ignored.
- Nothing is deployed. The app calls AWS APIs with the configured profile and serves the UI on
  `127.0.0.1` only.

## Procedure

Follow the steps in order. Stop and ask when a step says so.

1. **Get the AWS profile and Region from the user.** Never pick a profile yourself and never use
   one the user did not name. If they are unsure, show `aws configure list-profiles`.
2. **Confirm the account.** Run `aws sts get-caller-identity --profile <p>` and show the account ID
   and ARN. Ask the user to confirm it is the account intended for the PoC (not production) before
   continuing. For SSO profiles, ask them to run `aws sso login --profile <p>` if this fails.
3. **Ask whether they need PDF input or BDA.** Images (PNG/JPEG/TIFF) work without S3.
   PDF (Textract async) and BDA need a private S3 bucket in the same Region.
   - Existing bucket: `--bucket <name>`.
   - New bucket: `--create-bucket`. This creates a resource. Show the user the plan the script
     prints (private, Block Public Access on, HTTPS-only, objects expire after 7 days) and get an
     explicit yes. Only then add `--yes`. Do not create any other AWS resource.
4. **Run setup.** macOS/Linux `./setup.sh --profile <p> --region <r> [bucket option]`,
   Windows `.\setup.ps1 ...`. It creates `.venv`, installs pinned dependencies, makes a few cent-level
   test calls (Bedrock "Reply OK", one tiny Textract image) and writes `.env`.
   Read the summary. If Sonnet 5.5 is NOT READY:
   - `AccessDeniedException`: IAM policy (compare with `docs/iam-policy-example.json`) or model
     access not enabled in the Bedrock console for that account.
   - "not offered with scope": try `--model-scope global`, or a Region where the model is offered.
     Explain that `global` may process requests in other Regions; if the user has data-residency
     rules, they must decide.
   BDA "unavailable here" is normal in some Regions (e.g. ap-northeast-2). Say so; do not work around it.
5. **Prepare data with the user.**
   - Copy their scans to `data/images/`. Do not upload, send or copy documents anywhere else.
   - Draft `data/schema/<name>.json` from the form layout (look at the image if you can). Use
     `examples/flight-log.schema.json` as the pattern: one field per cell, a `description` with the
     exact notation (e.g. `HHMM`, `H+MM`, `DD-MMM-YY`, "Hangul stays Hangul"), arrays for table rows,
     and `x-validation` rules for sums and time spans the form must satisfy.
   - Create the answer file with `./run.sh template <file>` (empty values). **The user's staff fill in
     the truth values from the original document.** Do not fill truth values from model output, and do
     not "correct" truth values to match a model. If asked to pre-fill, refuse that part and explain that
     it would make the accuracy number meaningless.
6. **Measure.** `./run.sh evaluate` (default engines: sonnet, haiku, textract-detect; add
   `textract-analyze`, `opus`, `bda` with `--engines`). Report the summary table, point to
   `results/eval_*/mismatches.csv`, and describe which fields fail and why (notation, handwriting,
   Hangul, scan quality). Accuracy is exact match per field; nulls in truth are not scored.
   One sample is not a basis for a production accuracy claim; say so.
7. **Show the UI.** `./run.sh` (Windows `.\run.ps1`) → http://127.0.0.1:7860. The **Review** tab lists
   fields a person should check (rule violations, engine disagreement, low Textract confidence,
   Hangul). Never set `OCR_HOST` to anything but loopback: the UI has no login.
8. **Clean up when the PoC ends.** Delete `data/`, `results/`, `.env`, and the bucket if setup created
   it (`aws s3 rb s3://<bucket> --force --profile <p>` — irreversible; confirm first).

## Rules

- Do not modify IAM, networking, or any AWS resource beyond the single optional bucket.
- Do not commit or print the contents of `.env`, `data/`, or `results/`.
- Costs printed by the tools are estimates from us-east-1 list prices.
- Code changes: keep the existing style; run `python -m py_compile` on changed files and
  `./run.sh evaluate --only <file>` on one document before reporting success.
