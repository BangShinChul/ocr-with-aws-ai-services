"""
Batch accuracy evaluation over your own documents.

For every document in <DATA_DIR>/images that has a matching truth file, runs the chosen
engines, compares the result with the human-verified truth field by field, and writes:

    results/eval_<timestamp>/summary.csv        one row per engine (mean accuracy, time, cost)
    results/eval_<timestamp>/per_document.csv   one row per document x engine
    results/eval_<timestamp>/mismatches.csv     every field that did not match the truth
    results/eval_<timestamp>/raw/<doc>__<engine>.json

Usage:
    ./run.sh evaluate                                   # default engines
    ./run.sh evaluate --engines sonnet haiku textract-detect
    ./run.sh evaluate --only scan_01.png --details
"""
import shared.env  # noqa: F401  (load .env first)

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from statistics import mean

from engines.bda_engine import BDAEngine
from engines.bedrock_engine import BedrockEngine
from engines.textract_engine import TextractAnalyzeEngine, TextractDetectEngine
from shared.config import BDA_ENABLED, BEDROCK_MODELS, DATA_DIR, POSTPROCESSING_MODEL, S3_BUCKET, logger
from shared.cost_calculator import calculate_bda_cost, calculate_bedrock_cost
from shared.evaluator import get_detailed_accuracy
from shared.paths import document_path, list_documents, read_text_if_exists, results_dir, schema_path, truth_path

ENGINES = {
    "opus": "Claude Opus 5.5 (image)",
    "sonnet": "Claude Sonnet 5.5 (image)",
    "haiku": "Claude Haiku 5.5 (image)",
    "textract-detect": "Textract DetectDocumentText + LLM",
    "textract-analyze": "Textract AnalyzeDocument + LLM",
    "bda": "BDA Standard + LLM",
}
DEFAULT_ENGINES = ["sonnet", "haiku", "textract-detect"]


def run_engine(key, path, schema, doc_type):
    """Return (json, seconds, cost_usd, error)."""
    if key in ("opus", "sonnet", "haiku"):
        model_id = BEDROCK_MODELS[f"Claude {key.capitalize()} 5.5"]
        r = BedrockEngine().process_image(str(path), {"model_id": model_id, "document_type": doc_type,
                                                       "output_schema": schema})
        _, cost = calculate_bedrock_cost(model_id, r.get("token_usage"))
    elif key.startswith("textract"):
        engine = TextractDetectEngine() if key == "textract-detect" else TextractAnalyzeEngine()
        r = engine.process_image(str(path), {"output_schema": schema, "s3_bucket": S3_BUCKET})
        _, llm = calculate_bedrock_cost(POSTPROCESSING_MODEL, r.get("token_usage"))
        cost = (r.get("textract_cost") or 0) + llm
    else:
        r = BDAEngine().process_image(str(path), {"s3_bucket": S3_BUCKET, "output_schema": schema,
                                                  "document_type": doc_type, "use_blueprint": False})
        _, bda = calculate_bda_cost(False, "document", 1)
        _, llm = calculate_bedrock_cost(POSTPROCESSING_MODEL, r.get("token_usage"))
        cost = bda + llm
    error = r.get("text") if r.get("operation_type") == "error" or not r.get("json") else None
    return r.get("json"), float(r.get("process_time") or 0), cost, error


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engines", nargs="+", default=DEFAULT_ENGINES, choices=list(ENGINES))
    parser.add_argument("--only", nargs="+", help="Evaluate only these file names")
    parser.add_argument("--doc-type", default="form", help="generic | form | receipt | table | handwritten")
    parser.add_argument("--details", action="store_true", help="Print mismatched fields to the console")
    args = parser.parse_args()

    if "bda" in args.engines and (not BDA_ENABLED or not S3_BUCKET):
        sys.exit("BDA needs OCR_S3_BUCKET and a Region where BDA is available (see README).")

    docs = [d for d in list_documents() if not args.only or d in args.only]
    plan = [(d, truth_path(d)) for d in docs]
    missing = [d for d, t in plan if not read_text_if_exists(t)]
    plan = [(d, t) for d, t in plan if read_text_if_exists(t)]
    print(f"Data folder: {DATA_DIR}")
    if missing:
        print(f"Skipping {len(missing)} document(s) without a truth file: {', '.join(missing)}")
    if not plan:
        sys.exit("No document has a truth file. Put answers in <data>/truth/<name>.json "
                 "(./run.sh template <name> creates an empty one).")

    out = results_dir(f"eval_{datetime.now():%Y%m%d_%H%M%S}")
    (out / "raw").mkdir(exist_ok=True)
    per_doc, mismatches = [], []
    started = time.time()

    for name, tpath in plan:
        truth = json.loads(tpath.read_text(encoding="utf-8"))
        schema = read_text_if_exists(schema_path(name))
        if not schema:
            print(f"  ! {name}: no schema file — field names may not match the truth file")
        for key in args.engines:
            label = ENGINES[key]
            extracted, secs, cost, error = run_engine(key, document_path(name), schema, args.doc_type)
            detail = get_detailed_accuracy(extracted or {}, truth)
            (out / "raw" / f"{name}__{key}.json").write_text(
                json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8")
            per_doc.append({"document": name, "engine": label, "accuracy_pct": detail["total_accuracy"],
                            "matched": detail.get("matches", 0), "fields": detail.get("total", 0),
                            "seconds": round(secs, 1), "cost_usd": round(cost, 5), "error": error or ""})
            for f in detail["field_details"]:
                if not f["match"]:
                    mismatches.append({"document": name, "engine": label, "field": f["field"],
                                       "expected": f["expected"], "extracted": f["extracted"]})
            flag = f"  ERROR: {str(error)[:120]}" if error else ""
            print(f"  {name:<28} {label:<36} {detail['total_accuracy']:>6.1f}%  "
                  f"{secs:>5.1f}s  ${cost:.4f}{flag}")
            if args.details:
                for f in detail["field_details"]:
                    if not f["match"]:
                        print(f"      {f['field']}: expected={f['expected']!r} got={f['extracted']!r}")

    summary = []
    for key in args.engines:
        rows = [r for r in per_doc if r["engine"] == ENGINES[key]]
        summary.append({
            "engine": ENGINES[key], "documents": len(rows),
            "mean_accuracy_pct": round(mean(r["accuracy_pct"] for r in rows), 2),
            "min_accuracy_pct": min(r["accuracy_pct"] for r in rows),
            "field_accuracy_pct": round(100 * sum(r["matched"] for r in rows) / max(1, sum(r["fields"] for r in rows)), 2),
            "mean_seconds": round(mean(r["seconds"] for r in rows), 1),
            "mean_cost_usd": round(mean(r["cost_usd"] for r in rows), 5),
            "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        })

    for fname, rows in (("summary.csv", summary), ("per_document.csv", per_doc), ("mismatches.csv", mismatches)):
        with open(out / fname, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: opens cleanly in Excel
            if rows:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)

    print(f"\n{'engine':<36} {'docs':>4} {'mean acc':>9} {'field acc':>9} {'sec':>6} {'$/doc':>8}")
    for s in summary:
        print(f"{s['engine']:<36} {s['documents']:>4} {s['mean_accuracy_pct']:>8.1f}% "
              f"{s['field_accuracy_pct']:>8.1f}% {s['mean_seconds']:>6.1f} {s['mean_cost_usd']:>8.4f}")
    print(f"\nReports: {out}  ({time.time() - started:.0f}s)")
    print("Costs are estimates from us-east-1 list prices; check your bill for actual charges.")


if __name__ == "__main__":
    logger.setLevel("WARNING")
    import logging
    logging.getLogger().setLevel(logging.WARNING)
    main()
