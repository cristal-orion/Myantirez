import os
import re
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ANTIREZ_DATA_DIR") or ROOT / "data")
CHANNEL_ID = "UCDDG9vOcmgwlslJJpCWjqOg"
CHANNEL_URL = "https://www.youtube.com/@antirez/videos"
ENV_PATH = Path(os.environ.get("ANTIREZ_ENV_FILE") or ROOT / ".env")
MODEL_DEFAULTS = {"TRANSCRIBE_MODEL": "gemini-3.5-transcribe",
                  "CHAT_MODEL": "gemini-3.5-flash", "EMBED_MODEL": "gemini-embedding-001"}
_write_lock = threading.Lock()


def load_env(path=None):
    """Read local settings without putting secrets into the process environment."""
    path = path or ENV_PATH
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def setting(name, default=""):
    value = os.environ[name] if name in os.environ else load_env().get(name, default)
    return (value or default).strip()


def api_key():
    return setting("GEMINI_API_KEY")


def model(name, default):
    return setting(name, default)


def public_settings():
    """Expose only whether a key exists, never its value or a masked fragment."""
    return {"has_key": bool(api_key()),
            "models": {name: model(name, default) for name, default in MODEL_DEFAULTS.items()},
            "locked": [name for name in ("GEMINI_API_KEY", *MODEL_DEFAULTS) if name in os.environ]}


def clean_key(key):
    if not isinstance(key, str) or len(key) > 512 or any(ord(c) < 32 for c in key):
        raise ValueError("Chiave API non valida.")
    return key.strip()


def clean_model(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value.strip()):
        raise ValueError("Il nome del modello deve contenere solo lettere, numeri, punti, _ o -.")
    return value.strip()


def settings_to_test(values):
    """The key and models typed in the page, falling back to the saved ones."""
    if values.keys() - {"api_key", *MODEL_DEFAULTS}:
        raise ValueError("Impostazione non riconosciuta.")
    typed = clean_key(values.get("api_key", ""))
    key = typed or api_key()
    if not key:
        raise ValueError("Incolla una chiave da provare oppure salvane una.")
    models = {name: clean_model(values[name]) if name in values else model(name, default)
              for name, default in MODEL_DEFAULTS.items()}
    return key, models, bool(typed)


def save_settings(values):
    allowed = {"api_key", "clear_key", *MODEL_DEFAULTS}
    if values.keys() - allowed:
        raise ValueError("Impostazione non riconosciuta.")
    key = clean_key(values.get("api_key", ""))
    clear = values.get("clear_key", False)
    if not isinstance(clear, bool) or (clear and key):
        raise ValueError("Scegli se salvare o rimuovere la chiave.")
    updates = {}
    if key or clear:
        updates["GEMINI_API_KEY"] = "" if clear else key
    for name in MODEL_DEFAULTS:
        if name in values:
            updates[name] = clean_model(values[name])
    if any(name in os.environ for name in updates):
        raise ValueError("Una di queste impostazioni è gestita dall'ambiente del sistema e non si può cambiare qui.")
    if not updates:
        return public_settings()

    with _write_lock:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
        remaining = dict(updates)
        for index, line in enumerate(lines):
            name = line.split("=", 1)[0].strip()
            if name in updates:
                lines[index] = f"{name}={updates[name]}"
                remaining.pop(name, None)
        lines.extend(f"{name}={value}" for name, value in remaining.items())
        # Create privately, then replace atomically so a failed write cannot truncate .env.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ENV_PATH.parent,
                                             prefix=".env-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write("\n".join(lines) + "\n")
            os.replace(temporary, ENV_PATH)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return public_settings()
