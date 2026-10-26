"""Gradio callbacks for the Review tab (human review of flagged fields)."""
import json
import os
from datetime import datetime

import pandas as pd

from processor import LAST_RUN
from shared.review import ALL_REASONS, COLUMNS, apply_corrections, build_review_rows

from shared.paths import results_dir
_EMPTY = pd.DataFrame(columns=COLUMNS)


def _summary_html(rows, shown, primary):
    if not LAST_RUN["engine_jsons"]:
        return "<div style='padding:10px;color:#666'>파일을 처리하면 검수 목록이 여기에 나타납니다.</div>"
    if primary not in LAST_RUN["engine_jsons"]:
        return f"<div style='padding:10px;color:#c62828'>{primary} 결과가 없습니다. 처리한 엔진 중에서 기준 엔진을 골라 주세요.</div>"
    counts = {r: sum(1 for x in rows if r in x["_kinds"]) for r in ALL_REASONS}
    total = len(LAST_RUN.get("_field_count") or []) or "?"
    chips = " · ".join(f"{k} {v}" for k, v in counts.items())
    evidence = "Textract 근거 사용" if LAST_RUN["textract_blocks"] else "Textract 미실행 (신뢰도 신호 없음)"
    return (f"<div style='padding:10px;border-radius:6px;background:#fff3e0;color:#5d4037'>"
            f"<b>검수 대상 {len(shown)}건</b> / 전체 필드 {total}개 &nbsp;|&nbsp; {chips}"
            f"<br><small>기준 엔진: {primary} · {evidence} · 파일: {os.path.basename(LAST_RUN['image_name'] or '')}</small></div>")


def refresh_review(primary, threshold, reasons):
    rows = build_review_rows(LAST_RUN["engine_jsons"], primary, LAST_RUN["textract_blocks"],
                             LAST_RUN["schema"], float(threshold))
    data = LAST_RUN["engine_jsons"].get(primary)
    from shared.review import flatten
    LAST_RUN["_field_count"] = [k for k, v in flatten(data).items() if v not in (None, "")] if isinstance(data, dict) else []
    wanted = set(reasons or [])
    shown = [r for r in rows if r["_kinds"] & wanted]
    df = pd.DataFrame([{c: r[c] for c in COLUMNS} for r in shown], columns=COLUMNS) if shown else _EMPTY
    return _summary_html(rows, shown, primary), df


def save_review(primary, table):
    data = LAST_RUN["engine_jsons"].get(primary)
    if not isinstance(data, dict):
        return "<div style='padding:10px;color:#c62828'>저장할 결과가 없습니다.</div>"
    rows = table.to_dict("records") if isinstance(table, pd.DataFrame) else []
    corrected, log = apply_corrections(data, rows)
    base = os.path.splitext(os.path.basename(LAST_RUN["image_name"] or "document"))[0]
    path = str(results_dir("reviewed") / f"{base}_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"source_engine": primary, "reviewed_at": datetime.now().isoformat(timespec="seconds"),
                   "reviewed_fields": len(rows), "corrections": log, "data": corrected},
                  f, ensure_ascii=False, indent=2)
    return (f"<div style='padding:10px;border-radius:6px;background:#e8f5e9;color:#1b5e20'>"
            f"검수 {len(rows)}건 확인, 수정 {len(log)}건 반영 → <code>{path}</code></div>")
