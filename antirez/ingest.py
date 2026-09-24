"""Feed discovery, repeatable sync and audio processing."""

import fcntl
import json
import logging
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from . import db, gemini
from .config import CHANNEL_ID, CHANNEL_URL, DATA_DIR, api_key

LOG = logging.getLogger(__name__)
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


def feed():
    req = urllib.request.Request(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; AntirezArchive/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        root = ET.fromstring(response.read())
    result = []
    for entry in root.findall(ATOM + "entry"):
        ident = entry.findtext(YT + "videoId")
        title = entry.findtext(ATOM + "title")
        published = entry.findtext(ATOM + "published")
        if ident and title and published:
            result.append({"id": ident, "title": title, "published": published,
                           "url": f"https://www.youtube.com/watch?v={ident}"})
    if not result:
        raise RuntimeError("Il feed YouTube non contiene video: riprova più tardi.")
    return result


def older_videos(limit=100):
    """If the PC was off longer than the feed's 15 entries, inspect older uploads."""
    result = subprocess.run([
        "yt-dlp", "--flat-playlist", "--playlist-end", str(limit),
        "--dump-single-json", CHANNEL_URL,
    ], capture_output=True, text=True, timeout=180, check=True)
    data = json.loads(result.stdout)
    return [{"id": item["id"], "title": item.get("title") or item["id"],
             "published": (item.get("upload_date") or "")[:10],
             "url": f"https://www.youtube.com/watch?v={item['id']}"}
            for item in data.get("entries", []) if item and item.get("id")]


def split_passages(title, summary, transcript, size=1400, overlap=180):
    """Index prose in overlapping windows so questions can match across boundaries."""
    result = []
    for start in range(0, len(summary), size):
        begin = summary.rfind(" ", 0, start) + 1 if start else 0
        result.append(f"Titolo: {title}\nRiassunto: {summary[begin:start + size].strip()}")
    text = " ".join(transcript.split())
    start = 0
    while start < len(text):
        if start:
            start = text.rfind(" ", 0, start) + 1
        end = min(start + size, len(text))
        if end < len(text):
            split = text.rfind(". ", start + size // 2, end)
            if split > start:
                end = split + 1
        result.append(f"Video: {title}\nTrascrizione: {text[start:end].strip()}")
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return result


def download_and_split(video_id, workdir):
    url = f"https://www.youtube.com/watch?v={video_id}"
    subprocess.run(["yt-dlp", "--no-playlist", "--no-progress", "--no-warnings",
                    "-f", "bestaudio", "-o", str(workdir / "source.%(ext)s"), url],
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=900)
    sources = list(workdir.glob("source.*"))
    if not sources:
        raise RuntimeError("YouTube non ha restituito una traccia audio.")
    chunk_dir = workdir / "chunks"
    chunk_dir.mkdir()
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(sources[0]),
                    "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus",
                    "-b:a", "32k", "-f", "segment", "-segment_time", "720",
                    "-segment_format", "ogg", str(chunk_dir / "%03d.ogg")],
                   capture_output=True, check=True, timeout=900)
    chunks = sorted(chunk_dir.glob("*.ogg"))
    if not chunks:
        raise RuntimeError("Impossibile estrarre l'audio del video.")
    return chunks


def describe(exc):
    """yt-dlp explains a failure on stderr; its exit status alone says nothing."""
    stderr = getattr(exc, "stderr", None)
    if isinstance(exc, subprocess.CalledProcessError) and stderr:
        text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return "yt-dlp: " + lines[-1][:300]
    return str(exc)


def video_duration(video_id):
    try:
        result = subprocess.run(["yt-dlp", "--no-playlist", "--skip-download", "--no-warnings",
                                 "--print", "%(duration)s", f"https://www.youtube.com/watch?v={video_id}"],
                                capture_output=True, text=True, check=True, timeout=90)
        seconds = float(result.stdout.strip())
        return max(1, round(seconds))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def youtube_captions(video_id):
    """Last resort: original-language captions, not a Gemini transcription."""
    with tempfile.TemporaryDirectory(prefix="antirez-captions-") as temporary:
        target = Path(temporary)
        subprocess.run([
            "yt-dlp", "--no-playlist", "--no-progress", "--no-warnings", "--skip-download",
            "--write-auto-subs", "--write-subs", "--sub-langs", "it-orig,en-US-orig,en-orig",
            "--sub-format", "json3", "-o", str(target / "captions.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=120)
        files = list(target.glob("*.json3"))
        if not files:
            raise RuntimeError("Non sono disponibili sottotitoli nella lingua originale.")
        priority = {"it-orig": 0, "en-US-orig": 1, "en-orig": 2}
        files.sort(key=lambda path: priority.get(path.name.split(".")[-2], 9))
        events = json.loads(files[0].read_text(encoding="utf-8")).get("events", [])
    lines = []
    for event in events:
        if event.get("aAppend") or not event.get("segs"):
            continue
        text = "".join(segment.get("utf8", "") for segment in event["segs"]).strip()
        if text and text != "[musica]":
            lines.append(text)
    if not lines:
        raise RuntimeError("I sottotitoli non contengono testo parlato.")
    return "\n".join(lines)


def index_missing_embeddings():
    pending = db.unset_embeddings()
    for start in range(0, len(pending), 16):
        batch = pending[start:start + 16]
        try:
            vectors = gemini.embed([item["body"] for item in batch])
            db.save_embeddings(zip((item["id"] for item in batch), vectors))
        except gemini.GeminiError as exc:
            LOG.warning("Ricerca semantica temporaneamente non disponibile: %s", exc)
            break  # FTS still works; next sync can retry indexing.


def process_video(ident):
    item = db.video(ident)
    if not item or item["status"] == "ready":
        return
    db.update_video(ident, status="processing", error="")
    try:
        with tempfile.TemporaryDirectory(prefix="antirez-audio-") as temporary:
            chunks = download_and_split(ident, Path(temporary))
            parts = []
            for chunk in chunks:
                if chunk.stat().st_size > 14 * 1024 * 1024:
                    raise RuntimeError("Un segmento audio è troppo grande per la trascrizione inline.")
                parts.append(gemini.transcription(chunk.read_bytes()))
        transcript = "\n\n".join(parts)
        method = "audio"
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        LOG.warning("Audio non disponibile per %s (%s): uso il video con Gemini Flash", ident, describe(exc))
        try:
            transcript = gemini.video_transcription(item["url"], video_duration(ident))
            method = "video"
        except gemini.GeminiError as fallback_error:
            LOG.warning("Video %s: %s; provo i sottotitoli originali", ident, fallback_error)
            try:
                transcript = youtube_captions(ident)
                method = "captions"
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as caption_error:
                # The Gemini error usually explains the failure; captions are only the last resort.
                error = f"{fallback_error} Sottotitoli: {describe(caption_error)}"
                db.update_video(ident, status="error", error=error[:500])
                LOG.warning("Sottotitoli %s: %s", ident, describe(caption_error))
                return
    except gemini.GeminiError as exc:
        db.update_video(ident, status="error", error=str(exc)[:500])
        LOG.warning("Video %s: %s", ident, exc)
        return
    # Persist transcription before summary: retries should not re-download audio.
    db.update_video(ident, transcript=transcript, transcript_method=method)
    finish_video(ident)


def is_running():
    path = DATA_DIR / "sync.lock"
    if not path.exists():
        return False
    with path.open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False


def finish_video(ident):
    item = db.video(ident)
    if not item or item["status"] == "ready" or not item["transcript"]:
        return
    try:
        summary = item["summary"] or gemini.summarize(item["title"], item["transcript"])
        db.replace_passages(ident, split_passages(item["title"], summary, item["transcript"]))
        db.update_video(ident, summary=summary, status="ready", error="")
    except (RuntimeError, gemini.GeminiError) as exc:
        db.update_video(ident, status="error", error=str(exc)[:500])
        LOG.warning("Riassunto %s: %s", ident, exc)


def sync():
    """Exclusive lock across web and scheduled process; retry partial failures."""
    db.initialize()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / "sync.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        db.setting("sync_running", "1")
        db.setting("sync_error", "")
        try:
            if not api_key():
                raise RuntimeError("Inserisci GEMINI_API_KEY nel file .env prima di acquisire i video.")
            items = feed()
            first_run = not db.setting("initialized")
            if first_run:
                items = items[:10]
                db.setting("initial_cutoff", min(item["published"] for item in items))
            else:
                cutoff = db.setting("initial_cutoff")
                if not cutoff:
                    existing = db.videos(limit=500)
                    if existing:
                        cutoff = min(item["published"] for item in existing if item["published"])
                        db.setting("initial_cutoff", cutoff)
                if cutoff:
                    items = [item for item in items if item["published"] > cutoff]
                # The feed is only ~15 entries deep. If all are unseen, fetch more.
                if items and all(db.video(item["id"]) is None for item in items):
                    known = {item["id"]: item for item in items}
                    try:
                        for item in older_videos():
                            if db.video(item["id"]):
                                break
                            if cutoff and item["published"] and item["published"] < cutoff[:10]:
                                break
                            known.setdefault(item["id"], item)
                    except (OSError, subprocess.SubprocessError, ValueError) as exc:
                        LOG.warning("Impossibile cercare video più vecchi: %s", exc)
                    items = list(known.values())
            for item in reversed(items):
                db.upsert_video(item)
            db.setting("initialized", "1")
            # Oldest first, with resumed partial transcripts and failed items retried.
            for item in reversed(db.videos(limit=500)):
                if item["status"] == "ready":
                    continue
                stored = db.video(item["id"])
                if stored["transcript"]:
                    finish_video(item["id"])
                else:
                    process_video(item["id"])
            index_missing_embeddings()
            db.setting("last_sync", db.now())
            return True
        except Exception as exc:
            db.setting("sync_error", str(exc)[:500])
            LOG.exception("Acquisizione interrotta")
            raise
        finally:
            db.setting("sync_running", "0")
