"""Where documents, schemas, ground truth and results live.

    <DATA_DIR>/images/<name>.<ext>   documents to test
    <DATA_DIR>/schema/<name>.json    fields to extract (+ optional "x-validation" rules)
    <DATA_DIR>/truth/<name>.json     human-verified answers, same keys as the schema
"""
import os
from pathlib import Path
from typing import List, Optional

from shared.config import DATA_DIR, RESULTS_DIR, SUPPORTED_EXTENSIONS


def images_dir() -> Path:
    return DATA_DIR / "images"


def list_documents() -> List[str]:
    folder = images_dir()
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS)


def document_path(name: str) -> Path:
    return images_dir() / os.path.basename(name)


def _stem(name: str) -> str:
    return os.path.splitext(os.path.basename(name))[0]


def schema_path(name: str) -> Path:
    return DATA_DIR / "schema" / f"{_stem(name)}.json"


def truth_path(name: str) -> Path:
    return DATA_DIR / "truth" / f"{_stem(name)}.json"


def read_text_if_exists(path: Path) -> Optional[str]:
    if path.is_file() and path.stat().st_size > 0:
        return path.read_text(encoding="utf-8")
    return None


def results_dir(*parts: str) -> Path:
    path = RESULTS_DIR.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
