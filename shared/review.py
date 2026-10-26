"""
Human review queue: decide which extracted fields a person should check.

A field is flagged by any of three independent signals:
  1. Rule violation   - arithmetic/time rules declared in the schema ("x-validation") fail.
  2. Engine mismatch  - another engine extracted a different value for the same field.
  3. OCR evidence     - Amazon Textract read the value with low word confidence,
                        or Textract has no matching word at all (e.g. Korean handwriting,
                        which Textract does not support).

Claude on Bedrock returns no per-field confidence score, so (2) and (3) act as the
cross-check for LLM output, and (1) catches values that are internally inconsistent.
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

REASON_RULE = "규칙 위반"
REASON_MISMATCH = "엔진 간 불일치"
REASON_LOW_CONF = "저신뢰"
REASON_HANGUL = "한글 (OCR 교차검증 불가)"
REASON_NO_EVIDENCE = "OCR 근거 없음"
ALL_REASONS = [REASON_RULE, REASON_MISMATCH, REASON_LOW_CONF, REASON_HANGUL, REASON_NO_EVIDENCE]
_PRIORITY = {REASON_RULE: 1, REASON_MISMATCH: 2, REASON_LOW_CONF: 3, REASON_HANGUL: 3, REASON_NO_EVIDENCE: 4}
_HANGUL = re.compile(r"[\uac00-\ud7a3]")
_PRIORITY_LABEL = {1: "높음", 2: "높음", 3: "중간", 4: "낮음"}

COLUMNS = ["우선순위", "필드", "추출 값", "확정 값", "사유", "Textract 신뢰도", "다른 엔진 값"]


# ---------- JSON path helpers ----------

def flatten(obj: Any, prefix: str = "") -> Dict[str, Any]:
    """Flatten nested JSON into {"legs[0].flight_no": "AB 101", ...} (leaves only)."""
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    elif prefix:
        out[prefix] = obj
    return out


_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _split_path(path: str) -> List[Any]:
    return [int(i) if i else k for k, i in _TOKEN.findall(path)]


def get_path(obj: Any, path: str) -> Any:
    for part in _split_path(path):
        try:
            obj = obj[part]
        except (KeyError, IndexError, TypeError):
            return None
    return obj


def set_path(obj: Any, path: str, value: Any) -> None:
    parts = _split_path(path)
    for part in parts[:-1]:
        obj = obj[part]
    obj[parts[-1]] = value


# ---------- value normalization ----------

def _norm(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip().upper()


def _same(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return _norm(a) == _norm(b)


# ---------- signal 3: Textract word confidence ----------

def textract_word_confidence(blocks: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Index Textract output: normalized WORD -> best confidence, plus LINE texts for fallback."""
    words: Dict[str, float] = {}
    lines: List[Tuple[str, float]] = []
    for b in blocks or []:
        text = b.get("Text")
        if not text:
            continue
        if b.get("BlockType") == "WORD":
            key = _norm(text)
            words[key] = max(words.get(key, 0.0), float(b.get("Confidence", 0.0)))
        elif b.get("BlockType") == "LINE":
            lines.append((_norm(text).replace(" ", ""), float(b.get("Confidence", 0.0))))
    return {"words": words, "lines": lines} if words or lines else {}


def _evidence(value: Any, index: Dict[str, Any]) -> Optional[float]:
    """Confidence Textract has for this value; None if Textract never read it.

    Tries whole words first, then a line that contains the value with spaces removed
    (Textract often splits e.g. 'HL0000' into 'HL' + '0000')."""
    tokens = [t for t in _norm(value).split(" ") if t]
    scores = [index["words"].get(t) for t in tokens]
    if scores and all(s is not None for s in scores):
        return min(scores)
    compact = "".join(tokens)
    hits = [c for line, c in index["lines"] if compact and compact in line]
    return max(hits) if hits else None


# ---------- signal 1: schema rules ----------

def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _minutes(v: Any) -> Optional[int]:
    """'0723' -> 443 (clock), '1+43' -> 103 (duration)."""
    if v is None:
        return None
    s = str(v).strip()
    m = re.fullmatch(r"(\d{1,2})\+(\d{2})", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r"(\d{2})(\d{2})", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _check_rule(rule: Dict[str, Any], data: Any, base: str) -> Optional[Tuple[str, List[str]]]:
    """Return (message, involved field paths) if the rule fails, else None."""
    p = lambda rel: f"{base}.{rel}" if base else rel  # noqa: E731
    kind = rule.get("type", "sum")

    if kind == "sum":
        terms = [(p(f), c) for f, c in rule["lhs"]] + [(p(f), -c) for f, c in rule["rhs"]]
        vals = [(_num(get_path(data, f)), c) for f, c in terms]
        if any(v is None for v, _ in vals):
            return None
        if abs(sum(v * c for v, c in vals)) > rule.get("tolerance", 0.05):
            return rule["name"], [f for f, _ in terms]

    elif kind == "duration":
        start, end, dur = (p(rule[k]) for k in ("start", "end", "duration"))
        s, e, d = (_minutes(get_path(data, f)) for f in (start, end, dur))
        if None in (s, e, d):
            return None
        if (e - s) % (24 * 60) != d:
            return rule["name"], [start, end, dur]

    elif kind == "duration_total":
        items = get_path(data, rule["each"]) or []
        parts = [f"{rule['each']}[{i}].{rule['field']}" for i in range(len(items))]
        mins = [_minutes(get_path(data, f)) for f in parts]
        total = sum((_num(get_path(data, f)) or 0) * c for f, c in rule["equals"])
        if mins and None not in mins and abs(sum(mins) - total) > 0.5:
            return rule["name"], parts + [f for f, _ in rule["equals"]]
    return None


def validate(data: Any, schema: Optional[Dict[str, Any]]) -> List[Tuple[str, List[str]]]:
    failures = []
    for rule in (schema or {}).get("x-validation", []):
        each = rule.get("each") if rule.get("type") != "duration_total" else None
        if each:
            for i in range(len(get_path(data, each) or [])):
                hit = _check_rule(rule, data, f"{each}[{i}]")
                if hit:
                    failures.append(hit)
        else:
            hit = _check_rule(rule, data, "")
            if hit:
                failures.append(hit)
    return failures


# ---------- review queue ----------

def build_review_rows(engine_jsons: Dict[str, Any], primary: str,
                      textract_blocks: Optional[List[Dict[str, Any]]],
                      schema_str: Optional[str], threshold: float) -> List[Dict[str, Any]]:
    data = engine_jsons.get(primary)
    if not isinstance(data, dict):
        return []
    try:
        schema = json.loads(schema_str) if schema_str else None
    except json.JSONDecodeError:
        schema = None

    fields = flatten(data)
    others = {name: flatten(j) for name, j in engine_jsons.items()
              if name != primary and isinstance(j, dict)}
    word_conf = textract_word_confidence(textract_blocks)
    reasons: Dict[str, List[str]] = {f: [] for f in fields}

    for msg, involved in validate(data, schema):
        for f in involved:
            reasons.setdefault(f, []).append(f"{REASON_RULE}: {msg}")

    rows = []
    for path, value in fields.items():
        if value is None or value == "":
            continue
        # Free-text fields (summaries, remarks) are worded differently by every engine,
        # so only short values are compared across engines.
        comparable = not (isinstance(value, str) and len(value) > 40)
        diffs = [f"{n}={o[path]!r}" for n, o in others.items()
                 if comparable and o.get(path) not in (None, "") and not _same(o[path], value)]
        if diffs:
            reasons[path].append(REASON_MISMATCH)

        conf = None
        if word_conf and not isinstance(value, bool):
            conf = _evidence(value, word_conf)
            if conf is None:
                hangul = isinstance(value, str) and _HANGUL.search(value)
                reasons[path].append(REASON_HANGUL if hangul else REASON_NO_EVIDENCE)
            elif conf < threshold:
                reasons[path].append(REASON_LOW_CONF)

        if not reasons.get(path):
            continue
        priority = min(_PRIORITY[r.split(":")[0]] for r in reasons[path])
        rows.append({
            "_priority": priority,
            "_kinds": {r.split(":")[0] for r in reasons[path]},
            "우선순위": _PRIORITY_LABEL[priority],
            "필드": path,
            "추출 값": value,
            "확정 값": value,
            "사유": " / ".join(dict.fromkeys(reasons[path])),
            "Textract 신뢰도": "" if conf is None else round(conf, 1),
            "다른 엔진 값": ", ".join(diffs),
        })
    rows.sort(key=lambda r: (r["_priority"], r["필드"]))
    return rows


def apply_corrections(data: Dict[str, Any], edited_rows: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Write reviewer-confirmed values back into a copy of the extracted JSON."""
    result = json.loads(json.dumps(data, ensure_ascii=False))
    log = []
    for row in edited_rows:
        before, after = row.get("추출 값"), row.get("확정 값")
        if after is None or str(after) == str(before):
            continue
        if isinstance(before, bool):
            after = str(after).strip().lower() in ("true", "1", "yes", "y")
        elif isinstance(before, int) and re.fullmatch(r"-?\d+", str(after).strip()):
            after = int(after)
        elif isinstance(before, float) and _num(after) is not None:
            after = float(after)
        set_path(result, row["필드"], after)
        log.append({"field": row["필드"], "before": before, "after": after, "reason": row.get("사유")})
    return result, log
