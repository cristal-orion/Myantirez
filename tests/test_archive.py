import io
import json
import tempfile
import unittest
import urllib.request
import subprocess
import fcntl
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from antirez import chat, db, gemini, ingest
from antirez.server import Handler


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        path = Path(self.temporary.name)
        self.patches = [patch.object(db, "DATA_DIR", path), patch.object(db, "DB_PATH", path / "archive.sqlite3"),
                        patch.object(ingest, "DATA_DIR", path)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        db.initialize()

    def add_video(self, ident="abc123", title="Sui modelli locali"):
        db.upsert_video({"id": ident, "title": title, "published": datetime.now(timezone.utc).isoformat(),
                         "url": f"https://www.youtube.com/watch?v={ident}"})

    def test_archive_search_finds_transcript_and_replaces_index(self):
        self.add_video()
        db.replace_passages("abc123", ["Ridurre la latenza di un modello locale", "Un algoritmo di caching"])
        db.update_video("abc123", status="ready", transcript="testo", summary="riassunto")
        self.assertEqual(["abc123"], [item["id"] for item in db.videos("latenza")])
        self.assertEqual(1, len(db.lexical_passages("latenza")))
        db.replace_passages("abc123", ["Nuovo testo aggiornato"])
        self.assertEqual([], db.lexical_passages("latenza"))
        self.assertEqual(1, len(db.lexical_passages("aggiornato")))

    def test_sync_lock_reports_running_without_stale_database_flag(self):
        lock = Path(self.temporary.name) / "sync.lock"
        with lock.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(ingest.is_running())
        self.assertFalse(ingest.is_running())

    def test_transcriber_uses_same_endpoint_and_reads_all_parts(self):
        with patch.object(gemini, "request", return_value={"candidates": [{"content": {"parts": [
            {"audioTranscription": {"text": "Prima frase."}},
            {"audioTranscription": {"text": "Seconda frase."}},
        ]}}]}) as request:
            self.assertEqual("Prima frase. Seconda frase.", gemini.transcription(b"audio"))
            self.assertIn("v1alpha/models/gemini-3.5-transcribe:generateContent", request.call_args.args[0])
            self.assertEqual("SMART", request.call_args.args[1]["generationConfig"]["audioTranscriptionConfig"]["mode"])

    def test_video_fallback_splits_url_and_records_method(self):
        self.add_video()
        with patch.object(ingest, "download_and_split", side_effect=subprocess.CalledProcessError(1, "yt-dlp")), \
             patch.object(ingest, "video_duration", return_value=420), \
             patch.object(gemini, "video_transcription", return_value="Testo dal video") as fallback, \
             patch.object(gemini, "summarize", return_value="Riassunto fedele"):
            ingest.process_video("abc123")
            fallback.assert_called_once_with("https://www.youtube.com/watch?v=abc123", 420)
        self.assertEqual("ready", db.video("abc123")["status"])
        self.assertEqual("video", db.video("abc123")["transcript_method"])
        self.assertEqual("Testo dal video", db.video("abc123")["transcript"])

    def test_video_transcription_uses_time_windows(self):
        response = {"candidates": [{"content": {"parts": [{"text": "Parole dette."}]}, "finishReason": "STOP"}]}
        with patch.object(gemini, "request", return_value=response) as request:
            self.assertEqual("Parole dette.\n\nParole dette.", gemini.video_transcription("https://www.youtube.com/watch?v=abc123", 420))
            self.assertEqual("300s", request.call_args.args[1]["contents"][0]["parts"][0]["videoMetadata"]["startOffset"])

    def test_captions_are_used_when_video_model_cannot_transcribe(self):
        self.add_video()
        with patch.object(ingest, "download_and_split", side_effect=subprocess.CalledProcessError(1, "yt-dlp")), \
             patch.object(ingest, "video_duration", return_value=600), \
             patch.object(gemini, "video_transcription", side_effect=gemini.GeminiError("Output troncato")), \
             patch.object(ingest, "youtube_captions", return_value="Parole originali") as captions, \
             patch.object(gemini, "summarize", return_value="Riassunto"):
            ingest.process_video("abc123")
            captions.assert_called_once_with("abc123")
        self.assertEqual("captions", db.video("abc123")["transcript_method"])

    def test_sync_starts_with_ten_and_retries_failed_video(self):
        items = [{"id": f"v{i}", "title": f"Video {i}", "published": f"2026-09-{22-i:02d}T12:00:00Z",
                  "url": f"https://www.youtube.com/watch?v=v{i}"} for i in range(15)]
        def process(ident):
            db.update_video(ident, status="ready", summary="Pronto", transcript="Testo")
        with patch.object(ingest, "api_key", return_value="fake"), patch.object(ingest, "feed", return_value=items), \
             patch.object(ingest, "process_video", side_effect=process) as call, \
             patch.object(ingest, "index_missing_embeddings"):
            ingest.sync()
            self.assertEqual(10, len(db.videos()))
            self.assertEqual(10, call.call_count)
            call.reset_mock()
            ingest.sync()
            self.assertEqual(10, len(db.videos()))
            self.assertEqual(0, call.call_count)
            newer = {"id": "new", "title": "Un video nuovo", "published": "2026-09-23T12:00:00Z",
                     "url": "https://www.youtube.com/watch?v=new"}
            items.insert(0, newer)
            ingest.sync()
            self.assertEqual(11, len(db.videos()))
            call.assert_called_once_with("new")
            db.update_video("v0", status="error", error="temporaneo", transcript="")
            call.reset_mock()
            ingest.sync()
            call.assert_called_once_with("v0")

    def test_chat_cites_real_video_and_saves_history(self):
        self.add_video()
        db.update_video("abc123", status="ready", summary="I modelli locali sono rapidi", transcript="Con una cache riduci latenza")
        db.replace_passages("abc123", ["Ridurre la latenza con una cache locale"])
        with patch.object(chat, "api_key", return_value="fake"), \
             patch.object(gemini, "generate", return_value="La cache riduce la latenza [1].") as generate:
            result = chat.answer("Come riduce la latenza?", None)
            self.assertEqual("abc123", result["sources"][0]["video_id"])
            self.assertIn("[1]", result["answer"])
            self.assertEqual(2, len(db.conversation(result["conversation_id"])["messages"]))
            self.assertIn("Ridurre la latenza", generate.call_args.args[1])

    def test_feed_reads_channel_id_and_does_not_need_a_youtube_key(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
          <entry><yt:videoId>test123</yt:videoId><title>Nuovo video</title><published>2026-09-22T00:00:00Z</published></entry></feed>'''
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        with patch.object(urllib.request, "urlopen", return_value=Response(xml)) as fetch:
            result = ingest.feed()
            self.assertEqual("test123", result[0]["id"])
            self.assertIn("UCDDG9vOcmgwlslJJpCWjqOg", fetch.call_args.args[0].full_url)

    def test_local_server_serves_page_and_requires_key_for_sync(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/") as response:
            self.assertIn(b"Antirez", response.read())
        with urllib.request.urlopen(base + "/api/status") as response:
            self.assertEqual({}, json.load(response)["stats"])
        with patch("antirez.server.api_key", return_value=""):
            req = urllib.request.Request(base + "/api/sync", b"{}", {"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            self.assertEqual(400, error.exception.code)
        request = urllib.request.Request(base + "/api/status", headers={"Host": "external.example"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(403, error.exception.code)


if __name__ == "__main__":
    unittest.main()
