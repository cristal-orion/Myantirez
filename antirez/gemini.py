"""Minimal Gemini REST client; the transcription model needs the v1alpha API."""

import base64
import json
import urllib.error
import urllib.request

from .config import api_key, model


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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc)["error"].get("message", "")
        except (ValueError, KeyError, TypeError):
            detail = ""
        # Never include response headers or request headers: those contain credentials.
        raise GeminiError(f"Gemini HTTP {exc.code}: {detail[:300] or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GeminiError(f"Impossibile raggiungere Gemini: {exc.reason if hasattr(exc, 'reason') else exc}") from exc


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
    parts = []
    windows = ([(start, min(start + 300, duration)) for start in range(0, duration, 300)]
               if duration and duration > 0 else [(None, None)])
    for start, end in windows:
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
                "generationConfig": {"temperature": 0, "maxOutputTokens": 10000},
            }, timeout=240,
        )
        try:
            if response["candidates"][0].get("finishReason") == "MAX_TOKENS":
                raise GeminiError("Trascrizione video troncata: riprovare con segmenti più brevi.")
        except (KeyError, IndexError, TypeError):
            pass
        text = text_from(response)
        if not text:
            raise GeminiError("Gemini non ha restituito parlato dal video.")
        parts.append(text)
    return "\n\n".join(parts)


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
