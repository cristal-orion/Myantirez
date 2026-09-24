"""Minimal Gemini REST client; the transcription model needs the v1alpha API."""

import base64
import json
import logging
import time
import urllib.error
import urllib.request

from .config import api_key, model

LOG = logging.getLogger(__name__)
# Overload and rate limits: Google itself says to try again later.
RETRY_CODES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (5, 15, 45)


class GeminiError(RuntimeError):
    pass


def request(path, payload, timeout=180):
    key = api_key()
    if not key:
        raise GeminiError("Chiave Gemini mancante. Inseriscila nelle Impostazioni.")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/" + path,
        data=body,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    return send(req, timeout)


def retry_delay(error, default):
    """Google may say how long to wait (RetryInfo, e.g. "37s"); never wait more than a minute."""
    for item in error.get("details", []) if isinstance(error, dict) else []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("RetryInfo"):
            try:
                return min(60.0, max(1.0, float(str(item.get("retryDelay", "")).rstrip("s"))))
            except ValueError:
                pass
    return default


def send(req, timeout, delays=RETRY_DELAYS):
    for delay in (*delays, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                error = json.load(exc)["error"]
                detail = error.get("message", "")
            except (ValueError, KeyError, TypeError, AttributeError):
                error, detail = {}, ""
            if exc.code in RETRY_CODES and delay is not None:
                wait = retry_delay(error, delay)
                LOG.warning("Gemini HTTP %s, nuovo tentativo tra %s s", exc.code, wait)
                time.sleep(wait)
                continue
            # Never include response headers or request headers: those contain credentials.
            raise GeminiError(f"Gemini HTTP {exc.code}: {detail[:300] or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GeminiError(f"Impossibile raggiungere Gemini: {exc.reason if hasattr(exc, 'reason') else exc}") from exc


def check(key, models):
    """Read-only calls with no quota cost: is the key valid, does each model exist for it?"""
    def get(path):
        # No retries: the settings page is waiting for an answer.
        return send(urllib.request.Request("https://generativelanguage.googleapis.com/" + path,
                                           headers={"x-goog-api-key": key}), 30, delays=())
    try:
        get("v1beta/models?pageSize=1")
    except GeminiError as exc:
        return {"ok": False, "message": f"Google rifiuta la chiave. {exc}", "missing": {}}
    # Transcription needs v1alpha, as in transcription(); the other models use v1beta.
    missing = {}
    for name, version in (("TRANSCRIBE_MODEL", "v1alpha"), ("CHAT_MODEL", "v1beta"), ("EMBED_MODEL", "v1beta")):
        try:
            get(f"{version}/models/{models[name]}")
        except GeminiError as exc:
            missing[name] = f"{models[name]}: {exc}"
    if missing:
        return {"ok": False, "missing": missing,
                "message": "La chiave funziona, ma questi modelli non sono disponibili: " + "; ".join(missing.values())}
    return {"ok": True, "message": "La chiave funziona e i tre modelli sono disponibili.", "missing": {}}


def text_from(response):
    try:
        return "\n".join(
            part["text"] for part in response["candidates"][0]["content"]["parts"]
            if not part.get("thought") and isinstance(part.get("text"), str)
        ).strip()
    except (KeyError, IndexError, TypeError):
        return ""


def transcription(audio):
    response = request(
        "v1alpha/models/" + model("TRANSCRIBE_MODEL", "gemini-3.5-transcribe") + ":generateContent",
        {
            "contents": [{"parts": [{"inlineData": {
                "mimeType": "audio/ogg", "data": base64.b64encode(audio).decode("ascii"),
            }}]}],
            "generationConfig": {"audioTranscriptionConfig": {"mode": "SMART"}},
        }, timeout=240,
    )
    try:
        result = " ".join(
            part["audioTranscription"]["text"].strip()
            for part in response["candidates"][0]["content"]["parts"]
            if isinstance(part.get("audioTranscription", {}).get("text"), str)
        ).strip()
    except (KeyError, IndexError, TypeError):
        result = ""
    if not result:
        raise GeminiError("La trascrizione è vuota: controlla l'audio o riprova più tardi.")
    return result


def video_transcription(url, duration=None):
    """Explicit fallback when YouTube denies the audio download (not the audio model)."""
    windows = ([(start, min(start + 300, duration)) for start in range(0, duration, 300)]
               if duration and duration > 0 else [(None, None)])
    return "\n\n".join(video_window(url, start, end) for start, end in windows)


def video_window(url, start, end):
    """A truncated window is split in half and retried, so one dense passage does not lose the video."""
    video_part = {"fileData": {"fileUri": url}}
    if start is not None:
        video_part["videoMetadata"] = {"startOffset": f"{start}s", "endOffset": f"{end}s"}
    response = request(
        "v1beta/models/" + model("CHAT_MODEL", "gemini-3.5-flash") + ":generateContent",
        {
            "contents": [{"parts": [video_part, {"text":
                "Trascrivi integralmente il parlato di questo estratto nella lingua originale. "
                "Mantieni le idee, i nomi e i termini tecnici; non riassumere, non tradurre, "
                "non aggiungere introduzioni né parole non udibili. Restituisci solo la trascrizione."}]}],
            # Thinking tokens count against maxOutputTokens: on a transcription they only truncate it.
            "generationConfig": {"temperature": 0, "maxOutputTokens": 10000,
                                 "thinkingConfig": {"thinkingBudget": 0}},
        }, timeout=240,
    )
    try:
        truncated = response["candidates"][0].get("finishReason") == "MAX_TOKENS"
    except (KeyError, IndexError, TypeError):
        truncated = False
    if truncated:
        usage = response.get("usageMetadata", {})
        LOG.warning("Estratto %s-%s s troncato (pensiero %s, testo %s token)", start, end,
                    usage.get("thoughtsTokenCount", 0), usage.get("candidatesTokenCount", "?"))
        if start is not None and end - start > 60:
            middle = (start + end) // 2
            return video_window(url, start, middle) + "\n\n" + video_window(url, middle, end)
        raise GeminiError("Trascrizione video troncata: riprovare con segmenti più brevi.")
    text = text_from(response)
    if not text:
        raise GeminiError("Gemini non ha restituito parlato dal video.")
    return text


def generate(system, prompt, timeout=180):
    response = request(
        "v1beta/models/" + model("CHAT_MODEL", "gemini-3.5-flash") + ":generateContent",
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }, timeout=timeout,
    )
    text = text_from(response)
    if not text:
        raise GeminiError("Gemini ha restituito una risposta vuota.")
    return text


def summarize(title, transcript):
    return generate(
        "Riassumi fedelmente in italiano una trascrizione di un video di Salvatore Sanfilippo. "
        "Apri con 2-3 frasi che dicano l'idea principale, poi elenca i punti importanti "
        "e gli eventuali esempi tecnici. Non inventare dettagli; segnala ciò che è incerto. "
        "Il titolo è un metadato, non una prova del contenuto.",
        f"Titolo: {title}\n\nTrascrizione:\n{transcript}", timeout=240,
    )


def embed(texts, task_type="RETRIEVAL_DOCUMENT"):
    """Keep vectors in SQLite; no external vector database required."""
    if not texts:
        return []
    embed_model = model("EMBED_MODEL", "gemini-embedding-001")
    requests = [{"model": "models/" + embed_model,
                 "content": {"parts": [{"text": text}]},
                 "taskType": task_type, "outputDimensionality": 768} for text in texts]
    if len(texts) == 1:
        result = request("v1beta/models/" + embed_model + ":embedContent", requests[0])
        return [result["embedding"]["values"]]
    result = request("v1beta/models/" + embed_model + ":batchEmbedContents", {"requests": requests})
    vectors = [item["values"] for item in result["embeddings"]]
    if len(vectors) != len(texts):
        raise GeminiError("Numero di vettori inatteso dalla ricerca semantica.")
    return vectors
