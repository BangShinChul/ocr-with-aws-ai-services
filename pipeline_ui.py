"""Gradio section for the deployed pipeline: upload, review queue, approve/reject (local only)."""
import io
import json

import gradio as gr
import pandas as pd
from PIL import Image

import pipeline_client as pc
from shared.aws_client import get_aws_client
from shared.review import COLUMNS

_QUEUE_COLS = ["doc_id", "created_at", "filename", "doc_type", "flagged", "fields"]
_STATUS_COLS = ["received_at", "doc_id", "status", "flagged", "corrections", "error"]


def _msg(text, ok=True):
    color = "#2e7d32" if ok else "#c62828"
    return f"<div style='padding:8px;color:{color}'>{text}</div>"


def _upload(file, doc_type, schema_file):
    try:
        if not file:
            return _msg("파일을 선택해 주세요.", False)
        notes = []
        if schema_file:
            notes.append(f"스키마 {pc.put_schema(schema_file, doc_type)}")
        notes.append(f"문서 {pc.upload(file, doc_type)}")
        return _msg("업로드했습니다: " + ", ".join(notes) + ". 몇 초 뒤 처리가 시작됩니다.")
    except Exception as e:  # noqa: BLE001
        return _msg(str(e), False)


def _refresh():
    try:
        queue = pc.pending()
        recent = pc.status(30)
    except Exception as e:  # noqa: BLE001
        return (_msg(str(e), False), pd.DataFrame(columns=_QUEUE_COLS), gr.update(choices=[], value=None),
                pd.DataFrame(columns=_STATUS_COLS))
    q = pd.DataFrame([{c: i.get(c) for c in _QUEUE_COLS} for i in queue], columns=_QUEUE_COLS)
    s = pd.DataFrame([{c: i.get(c, "") for c in _STATUS_COLS} for i in recent], columns=_STATUS_COLS)
    ids = [i["doc_id"] for i in queue]
    return (_msg(f"검수 대기 {len(ids)}건"), q, gr.update(choices=ids, value=ids[0] if ids else None), s)


def _load(doc_id):
    if not doc_id:
        return pd.DataFrame(columns=COLUMNS), None, ""
    try:
        item = pc.get_review(doc_id)
        body = get_aws_client("s3").get_object(Bucket=pc.outputs()["DocumentBucket"], Key=item["key"])["Body"].read()
        image = Image.open(io.BytesIO(body)) if not item["key"].lower().endswith(".pdf") else None
    except Exception as e:  # noqa: BLE001
        return pd.DataFrame(columns=COLUMNS), None, _msg(str(e), False)
    rows = pd.DataFrame([{c: r.get(c) for c in COLUMNS} for r in item.get("rows", [])], columns=COLUMNS)
    return rows, image, _msg(f"{item.get('filename')}: {len(rows)}개 칸을 확인해 주세요. "
                             "`확정 값`을 고친 뒤 승인하면 처리가 이어집니다.")


def _approve(doc_id, table, reviewer):
    try:
        # to_json keeps ints as ints (to_dict would hand numpy types to the JSON encoder)
        rows = json.loads(table.to_json(orient="records", force_ascii=False)) if isinstance(table, pd.DataFrame) else []
        pc.approve(doc_id, rows, reviewer or "reviewer")
        return _msg(f"{doc_id} 승인했습니다. 결과는 results/{doc_id}.json 에 저장됩니다.")
    except Exception as e:  # noqa: BLE001
        return _msg(str(e), False)


def _reject(doc_id, reason):
    try:
        pc.reject(doc_id, reason or "rejected by reviewer")
        return _msg(f"{doc_id} 반려했습니다.")
    except Exception as e:  # noqa: BLE001
        return _msg(str(e), False)


def build_pipeline_section():
    with gr.Accordion(f"서버리스 파이프라인 (스택: {pc.STACK})", open=False):
        gr.Markdown("배포한 `infra/` 스택에 문서를 올리고, 검수 대기 중인 문서를 확인·승인합니다. "
                    "이 화면은 이 PC(127.0.0.1)에서만 열리며, 로컬 AWS 자격 증명으로 호출합니다.")
        with gr.Row():
            file = gr.File(label="스캔 파일 (PNG·JPEG·PDF)", type="filepath", scale=2)
            schema = gr.File(label="스키마 JSON (처음 한 번)", type="filepath", file_types=[".json"], scale=1)
            with gr.Column(scale=1):
                doc_type = gr.Textbox(value="flight-log", label="문서 유형 (incoming/<유형>/)")
                upload_btn = gr.Button("⬆️ 업로드", variant="primary")
        upload_msg = gr.HTML()
        refresh_btn = gr.Button("🔄 검수 대기·처리 현황 새로고침")
        queue_msg = gr.HTML()
        queue = gr.Dataframe(value=pd.DataFrame(columns=_QUEUE_COLS), interactive=False, label="검수 대기")
        with gr.Row():
            doc_id = gr.Dropdown(choices=[], label="검수할 문서", scale=2)
            reviewer = gr.Textbox(label="검수자", placeholder="name@example.com", scale=1)
        with gr.Row():
            image = gr.Image(label="원본", type="pil", scale=1)
            rows = gr.Dataframe(value=pd.DataFrame(columns=COLUMNS), interactive=True, wrap=True, scale=2,
                                label="검수 대상 (확정 값 열을 수정하세요)")
        load_msg = gr.HTML()
        with gr.Row():
            approve_btn = gr.Button("✅ 확정하고 처리 재개", variant="primary")
            reason = gr.Textbox(label="반려 사유", scale=2)
            reject_btn = gr.Button("반려")
        decision_msg = gr.HTML()
        recent = gr.Dataframe(value=pd.DataFrame(columns=_STATUS_COLS), interactive=False, label="최근 처리 현황")

        upload_btn.click(_upload, [file, doc_type, schema], upload_msg)
        refresh_btn.click(_refresh, None, [queue_msg, queue, doc_id, recent])
        doc_id.change(_load, doc_id, [rows, image, load_msg])
        approve_btn.click(_approve, [doc_id, rows, reviewer], decision_msg).then(
            _refresh, None, [queue_msg, queue, doc_id, recent])
        reject_btn.click(_reject, [doc_id, reason], decision_msg).then(
            _refresh, None, [queue_msg, queue, doc_id, recent])
