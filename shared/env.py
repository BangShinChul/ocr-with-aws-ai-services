"""
Load settings from a local `.env` file into the process environment.

Kept dependency-free on purpose.

Precedence: a non-empty value in `.env` wins over the shell environment. This is
deliberate: `.env` records the profile and Region that setup verified, so a stray
`AWS_PROFILE` left in someone's shell cannot silently point the demo at a different
account. Empty values in `.env` are ignored (the shell / default chain applies).
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            os.environ[key] = value
    # Keep botocore's own Region variable consistent with the verified Region.
    if os.environ.get("AWS_REGION"):
        os.environ["AWS_DEFAULT_REGION"] = os.environ["AWS_REGION"]


load_env_file()

# botocore reads AWS_DEFAULT_REGION; accept the more common AWS_REGION too.
if os.environ.get("AWS_REGION") and not os.environ.get("AWS_DEFAULT_REGION"):
    os.environ["AWS_DEFAULT_REGION"] = os.environ["AWS_REGION"]

# Gradio sends usage telemetry to an external endpoint unless disabled.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
