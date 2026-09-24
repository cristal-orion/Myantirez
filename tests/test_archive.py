import base64
import io
import json
import os
import stat
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

from antirez import chat, config, db, gemini, ingest
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

    def test_failed_fallbacks_keep_gemini_error_and_youtube_reason(self):
        self.add_video()
        blocked = subprocess.CalledProcessError(1, "yt-dlp", stderr=b"WARNING: lento\nERROR: Sign in to confirm you're not a bot\n")
        with patch.object(ingest, "download_and_split", side_effect=blocked), \
             patch.object(ingest, "video_duration", return_value=None), \
             patch.object(gemini, "video_transcription", side_effect=gemini.GeminiError("Gemini HTTP 400: API key not valid.")), \
             patch.object(ingest, "youtube_captions", side_effect=blocked):
            ingest.process_video("abc123")
        item = db.video("abc123")
        self.assertEqual("error", item["status"])
        self.assertIn("API key not valid", item["error"])
        self.assertIn("yt-dlp: ERROR: Sign in to confirm", item["error"])

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

    def test_published_server_accepts_its_domain_only_with_password(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        public = {"ALLOWED_HOSTS": "antirez.example", "AUTH_PASSWORD": "segreta"}

        def call(path, credentials=None, data=None, origin=None):
            headers = {"Host": "antirez.example"}
            if credentials:
                headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
            if data is not None:
                headers["Content-Type"] = "application/json"
            if origin:
                headers["Origin"] = origin
            return urllib.request.urlopen(urllib.request.Request(base + path, data, headers))

        with patch.dict(os.environ, public):
            for credentials in (None, "antirez:sbagliata", "altro:segreta"):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    call("/api/status", credentials)
                self.assertEqual(401, error.exception.code)
                self.assertIn("Basic", error.exception.headers["WWW-Authenticate"])
            with call("/api/status", "antirez:segreta") as response:
                self.assertEqual({}, json.load(response)["stats"])
            with patch("antirez.server.api_key", return_value=""):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    call("/api/sync", "antirez:segreta", b"{}", "https://antirez.example")
                self.assertEqual(400, error.exception.code)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    call("/api/sync", "antirez:segreta", b"{}", "https://altro.example")
                self.assertEqual(403, error.exception.code)
        with self.assertRaises(urllib.error.HTTPError) as error:
            call("/api/status", "antirez:segreta")
        self.assertEqual(403, error.exception.code)

    def test_settings_are_local_private_and_active_without_restart(self):
        env = Path(self.temporary.name) / ".env"
        env.write_text("# Preferenze locali\nPORT=8765\nCHAT_MODEL=vecchio\n", encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def post(payload):
            request = urllib.request.Request(base + "/api/settings", json.dumps(payload).encode(),
                                             {"Content-Type": "application/json"})
            return urllib.request.urlopen(request)

        with patch.object(config, "ENV_PATH", env), patch.dict(os.environ, {}, clear=True):
            with urllib.request.urlopen(base + "/api/settings") as response:
                self.assertNotIn("api_key", json.load(response))
            with post({"api_key": "test-secret", "CHAT_MODEL": "gemini-2.5-flash"}) as response:
                result = json.load(response)
                self.assertTrue(result["has_key"])
                self.assertNotIn("test-secret", json.dumps(result))
            self.assertEqual("test-secret", config.api_key())
            self.assertEqual("gemini-2.5-flash", config.model("CHAT_MODEL", "default"))
            self.assertEqual(0o600, stat.S_IMODE(env.stat().st_mode))
            self.assertIn("PORT=8765", env.read_text())
            with urllib.request.urlopen(base + "/api/status") as response:
                self.assertTrue(json.load(response)["has_key"])
            for invalid in ({"api_key": "secret\nPORT=1"}, {"CHAT_MODEL": "../wrong"}, {"PORT": "1"}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post(invalid)
                self.assertEqual(400, error.exception.code)
            self.assertEqual("test-secret", config.api_key())
            with post({"clear_key": True}) as response:
                self.assertFalse(json.load(response)["has_key"])
            self.assertEqual("", config.api_key())
            self.assertEqual("gemini-2.5-flash", config.model("CHAT_MODEL", "default"))

    def test_gemini_retries_overload_but_not_invalid_requests(self):
        def failure(code, error):
            body = io.BytesIO(json.dumps({"error": error}).encode())
            return urllib.error.HTTPError("https://example", code, "errore", {}, body)

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        busy = {"message": "high demand"}
        limited = {"message": "quota", "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}
        answers = [failure(503, busy), failure(429, limited), Response(b'{"ok": true}')]
        with patch.object(urllib.request, "urlopen", side_effect=answers) as urlopen, \
             patch.object(gemini.time, "sleep") as sleep:
            self.assertEqual({"ok": True}, gemini.send(urllib.request.Request("https://example"), 5))
        self.assertEqual(3, urlopen.call_count)
        self.assertEqual([5, 7.0], [call.args[0] for call in sleep.call_args_list])

        with patch.object(urllib.request, "urlopen", side_effect=[failure(503, busy) for _ in range(4)]) as urlopen, \
             patch.object(gemini.time, "sleep"):
            with self.assertRaisesRegex(gemini.GeminiError, "HTTP 503: high demand"):
                gemini.send(urllib.request.Request("https://example"), 5)
        self.assertEqual(4, urlopen.call_count)

        for delays, code in ((gemini.RETRY_DELAYS, 400), ((), 503)):
            with patch.object(urllib.request, "urlopen", side_effect=[failure(code, busy)]) as urlopen, \
                 patch.object(gemini.time, "sleep") as sleep:
                with self.assertRaises(gemini.GeminiError):
                    gemini.send(urllib.request.Request("https://example"), 5, delays)
            self.assertEqual(1, urlopen.call_count)
            sleep.assert_not_called()

    def test_key_check_reports_rejected_key_and_missing_models(self):
        models = dict(config.MODEL_DEFAULTS)
        calls = []

        def google(req, timeout, delays):
            self.assertEqual((), delays)
            calls.append((req.full_url, req.get_header("X-goog-api-key"), req.get_method()))
            if req.get_header("X-goog-api-key") == "sbagliata":
                raise gemini.GeminiError("Gemini HTTP 400: API key not valid.")
            if req.full_url.endswith("v1alpha/models/gemini-3.5-transcribe"):
                raise gemini.GeminiError("Gemini HTTP 404: not found")
            return {}

        with patch.object(gemini, "send", side_effect=google):
            rejected = gemini.check("sbagliata", models)
            self.assertFalse(rejected["ok"])
            self.assertIn("API key not valid", rejected["message"])
            self.assertEqual(1, len(calls))
            partial = gemini.check("buona", models)
            self.assertFalse(partial["ok"])
            self.assertEqual(["TRANSCRIBE_MODEL"], list(partial["missing"]))
            self.assertTrue(all(method == "GET" for _, _, method in calls))
            models["TRANSCRIBE_MODEL"] = "altro-modello"
            self.assertTrue(gemini.check("buona", models)["ok"])

    def test_key_test_endpoint_prefers_typed_key_and_needs_one(self):
        env = Path(self.temporary.name) / ".env"
        env.write_text("GEMINI_API_KEY=salvata\n", encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def post(payload):
            request = urllib.request.Request(base + "/api/settings/test", json.dumps(payload).encode(),
                                             {"Content-Type": "application/json"})
            return urllib.request.urlopen(request)

        ok = {"ok": True, "message": "ok", "missing": {}}
        with patch.object(config, "ENV_PATH", env), patch.dict(os.environ, {}, clear=True), \
             patch.object(gemini, "check", return_value=ok) as check:
            with post({"api_key": " incollata ", "CHAT_MODEL": "gemini-2.5-flash"}) as response:
                self.assertEqual("typed", json.load(response)["tested"])
            self.assertEqual("incollata", check.call_args.args[0])
            self.assertEqual("gemini-2.5-flash", check.call_args.args[1]["CHAT_MODEL"])
            with post({}) as response:
                self.assertEqual("saved", json.load(response)["tested"])
            self.assertEqual("salvata", check.call_args.args[0])
            self.assertEqual("salvata", config.api_key())
            env.write_text("", encoding="utf-8")
            for invalid in ({}, {"CHAT_MODEL": "../x"}, {"PORT": "1"}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post(dict(invalid, **({"api_key": "k"} if invalid else {})))
                self.assertEqual(400, error.exception.code)

    def test_environment_settings_cannot_be_overridden_in_page(self):
        env = Path(self.temporary.name) / ".env"
        with patch.object(config, "ENV_PATH", env), patch.dict(os.environ, {"GEMINI_API_KEY": "external"}):
            self.assertIn("GEMINI_API_KEY", config.public_settings()["locked"])
            with self.assertRaises(ValueError):
                config.save_settings({"api_key": "replacement"})
            self.assertFalse(env.exists())


if __name__ == "__main__":
    unittest.main()
