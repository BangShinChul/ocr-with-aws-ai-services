import logging
import os
from pathlib import Path

from shared import env as _env  # noqa: F401  (loads .env before anything reads os.environ)
import gradio as gr

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROJECT_ROOT = _env.PROJECT_ROOT


def _env_str(name, default=""):
    return (os.environ.get(name) or default).strip()


def _env_bool(name, default):
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "y")


# ---------------------------------------------------------------------------
# AWS account and region
#   Set via .env (written by scripts/setup_env.py) or real environment variables.
#   AWS_PROFILE empty -> the default credential chain (env keys, SSO, instance role).
# ---------------------------------------------------------------------------
AWS_PROFILE = _env_str("AWS_PROFILE") or None
AWS_REGION = _env_str("AWS_REGION") or _env_str("AWS_DEFAULT_REGION") or None

# Private S3 bucket. Only needed for PDF input to Textract and for BDA.
S3_BUCKET = _env_str("OCR_S3_BUCKET")

# BDA is not offered in every Region; setup_env.py records whether it works here.
BDA_ENABLED = _env_bool("OCR_BDA_ENABLED", True)

# ---------------------------------------------------------------------------
# Bedrock models
#   OCR_MODEL_SCOPE is the cross-Region inference profile prefix:
#     global -> may route to any commercial Region (widest availability)
#     us / eu / apac / jp ... -> stays within that geography, if the model is offered there
# ---------------------------------------------------------------------------
MODEL_SCOPE = _env_str("OCR_MODEL_SCOPE", "global")
_MODEL_FAMILIES = {
    "Claude Opus 5.5": "anthropic.claude-opus-5-5",
    "Claude Sonnet 5.5": "anthropic.claude-sonnet-5-5",
    "Claude Haiku 5.5": "anthropic.claude-haiku-5-5",
}
BEDROCK_MODELS = {name: f"{MODEL_SCOPE}.{base}" for name, base in _MODEL_FAMILIES.items()}
DEFAULT_BEDROCK_MODEL = "Claude Sonnet 5.5"

# Model used to structure Textract/BDA text output into JSON.
POSTPROCESSING_MODEL = (_env_str("OCR_POSTPROCESSING_MODEL")
                        or _env_str("POSTPROCESSING_MODEL")
                        or BEDROCK_MODELS[DEFAULT_BEDROCK_MODEL])

# ---------------------------------------------------------------------------
# Local folders and web UI
# ---------------------------------------------------------------------------
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf")


def _has_documents(folder: Path) -> bool:
    images = folder / "images"
    return images.is_dir() and any(p.suffix.lower() in _IMAGE_EXTS for p in images.iterdir())


def _resolve_data_dir() -> Path:
    configured = _env_str("OCR_DATA_DIR")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else PROJECT_ROOT / path
    # Your own documents in data/ take precedence; fall back to the bundled samples.
    data = PROJECT_ROOT / "data"
    return data if _has_documents(data) else PROJECT_ROOT / "sample"


DATA_DIR = _resolve_data_dir()
RESULTS_DIR = Path(_env_str("OCR_RESULTS_DIR") or PROJECT_ROOT / "results")
SUPPORTED_EXTENSIONS = _IMAGE_EXTS

# Bind to loopback by default: the UI has no authentication.
SERVER_HOST = _env_str("OCR_HOST", "127.0.0.1")
SERVER_PORT = int(_env_str("OCR_PORT", "7860"))

# Image size constants
MAX_IMAGE_SIZE = 5 * 1024 * 1024 - 100000  # 5MB minus buffer for Bedrock

# ---------------------------------------------------------------------------
# Cost estimates (USD). Reference list prices from the AWS Price List API, us-east-1,
# on-demand, checked 2026-10-26. Other Regions and discounts are not reflected.
# ---------------------------------------------------------------------------
# Bedrock values are USD per 1,000 tokens (the calculators divide token counts by 1,000).
_GLOBAL_TOKEN_PRICES = {
    "anthropic.claude-opus-5-5": (0.004, 0.020),     # $4.00 / $20.00 per 1M tokens
    "anthropic.claude-sonnet-5-5": (0.002, 0.010),   # $2.00 / $10.00 per 1M tokens
    "anthropic.claude-haiku-5-5": (0.0001, 0.0005),  # $0.10 / $0.50 per 1M tokens
}
# Geographic (non-global) profiles are listed at 10% above the global tier.
_SCOPE_MULTIPLIER = 1.0 if MODEL_SCOPE == "global" else 1.1


def _bedrock_prices():
    prices = {}
    for base, (inp, out) in _GLOBAL_TOKEN_PRICES.items():
        prices[f"{MODEL_SCOPE}.{base}"] = {"input": inp * _SCOPE_MULTIPLIER, "output": out * _SCOPE_MULTIPLIER}
    custom = POSTPROCESSING_MODEL
    if custom not in prices:
        for base, (inp, out) in _GLOBAL_TOKEN_PRICES.items():
            if custom.endswith(base):
                mult = 1.0 if custom.startswith("global.") else 1.1
                prices[custom] = {"input": inp * mult, "output": out * mult}
    return prices


API_COSTS = {
    'textract_detect': 1.50 / 1000,          # DetectDocumentText: $1.50 per 1,000 pages
    'textract_async': 1.50 / 1000,           # StartDocumentTextDetection
    'textract_analyze': 65.00 / 1000,        # AnalyzeDocument (Forms + Tables)
    'textract_analyze_async': 65.00 / 1000,  # StartDocumentAnalysis (Forms + Tables)
    'textract_analyze_forms': 50.00 / 1000,
    'textract_analyze_tables': 15.00 / 1000,
    'bedrock': _bedrock_prices(),
    'bda': {
        'standard': {
            'document': 0.010,  # $0.010 per page
            'image': 0.003      # $0.003 per image
        },
        'custom': {
            'document': 0.040,  # $0.040 per page
            'image': 0.005,     # $0.005 per image
            'extra_field': 0.0005  # $0.0005 per additional field (beyond 30)
        }
    }
}



# Status HTML templates
STATUS_HTML = {
    "processing": lambda engine: f"""<div style='padding: 10px; background-color: #3b5998; color: white; 
                                     border-radius: 5px; font-weight: bold;'>Processing with {engine}...</div>""",
    "completed": lambda engine, time, cost, token_info="": f"""<div style='padding: 10px; background-color: #2e7d32; color: white; 
                                         border-radius: 5px; font-weight: bold;'> {engine} completed in {time:.3f} seconds (Est. cost: ${cost:.6f}){token_info}</div>""",
    "error": lambda engine, time, error: f"""<div style='padding: 10px; background-color: #c62828; color: white; 
                                           border-radius: 5px; font-weight: bold;'> {engine} error ({time:.3f}s): {error}</div>""",
    "global_processing": lambda: """<div style='padding: 10px; background-color: #3b5998; color: white; 
                                    border-radius: 5px; font-weight: bold;'>Processing with selected engines...</div>""",
    "global_completed": lambda time, cost: f"""<div style='padding: 10px; background-color: #2e7d32; color: white; 
                                        border-radius: 5px; font-weight: bold;'>All processing completed in {time:.3f} seconds (Total est. cost: ${cost:.6f})</div>""",
    "global_partial": lambda success, total, time, cost: f"""<div style='padding: 10px; background-color: #ed6c02; color: white; 
                                                     border-radius: 5px; font-weight: bold;'>{success}/{total} engines completed in {time:.3f} seconds (Total est. cost: ${cost:.6f})</div>"""
}

# Use a simpler theme approach that works across Gradio versions
CUSTOM_THEME = gr.themes.Default()