import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CHANNEL_ID = "UCDDG9vOcmgwlslJJpCWjqOg"
CHANNEL_URL = "https://www.youtube.com/@antirez/videos"


def load_env(path=ROOT / ".env"):
    """Small local .env reader; environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


load_env()


def api_key():
    return os.environ.get("GEMINI_API_KEY", "").strip()


def model(name, default):
    return os.environ.get(name, default).strip() or default
