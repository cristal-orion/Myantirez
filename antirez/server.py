"""Loopback-only web app by default. API and static page live on the same origin.

To publish it behind a proxy, set HOST, ALLOWED_HOSTS and AUTH_PASSWORD. Friends can get
guest accounts with GUEST_ACCOUNTS="Nome:password,...": no settings, no sync, and at most
GUEST_CHAT_LIMIT chat questions per Italian day (30 by default)."""

import base64
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import chat, db, ingest
from .config import ROOT, api_key, public_settings, save_settings, setting, settings_to_test

LOG = logging.getLogger(__name__)
STATIC = ROOT / "static"
LOCAL_OWNER = {"name": "", "owner": "", "admin": True}
ADMIN_ONLY = {"/api/settings", "/api/settings/test", "/api/sync"}
STATIC_FILES = {"/": ("index.html", "text/html"),
                "/app.css": ("app.css", "text/css"),
                "/app.js": ("app.js", "text/javascript")}


def accounts():
    owner = {"name": setting("AUTH_USER", "antirez"), "password": setting("AUTH_PASSWORD"), "owner": "", "admin": True}
    guests = []
    for entry in setting("GUEST_ACCOUNTS").split(","):
        name, _, password = entry.strip().partition(":")
        if name.strip() and password:
            guests.append({"name": name.strip(), "password": password, "owner": name.strip().lower(), "admin": False})
    return [owner, *guests]


def chat_allowance(account):
    """(questions left today, daily limit) for a guest; (None, None) for the owner."""
    if account["admin"]:
        return None, None
    try:
        limit = max(0, int(setting("GUEST_CHAT_LIMIT", "30")))
    except ValueError:
        limit = 30
    used = db.questions_since(account["owner"], db.today_start())
    return max(0, limit - used), limit


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, code=200, headers=None):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        for name, header in (headers or {}).items():
            self.send_header(name, header)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.local_host():
            return self.send_json({"error": "Host non consentito."}, 403)
        self.account = self.authenticate()
        if not self.account:
            return self.request_login()
        url = urlsplit(self.path)
        path = url.path
        query = parse_qs(url.query)
        if path in ADMIN_ONLY and not self.account["admin"]:
            return self.forbidden()
        try:
            if path == "/api/status":
                left, limit = chat_allowance(self.account)
                return self.send_json({"stats": db.stats(), "has_key": bool(api_key()),
                                       "last_sync": db.setting("last_sync"),
                                       "error": db.setting("sync_error"),
                                       "running": ingest.is_running(),
                                       "account": {"name": self.account["name"], "admin": self.account["admin"],
                                                   "chat_left": left, "chat_limit": limit}})
            if path == "/api/settings":
                return self.send_json(public_settings())
            if path == "/api/videos":
                return self.send_json(db.videos(query.get("q", [""])[0][:200], limit=200))
            if path.startswith("/api/videos/"):
                item = db.video(path.rsplit("/", 1)[-1])
                return self.send_json(item) if item else self.send_json({"error": "Video non trovato."}, 404)
            if path == "/api/conversations":
                return self.send_json(db.conversations(self.account["owner"]))
            if path.startswith("/api/conversations/"):
                item = db.conversation(int(path.rsplit("/", 1)[-1]), self.account["owner"])
                return self.send_json(item) if item else self.send_json({"error": "Chat non trovata."}, 404)
            if path in STATIC_FILES:
                name, mimetype = STATIC_FILES[path]
                body = (STATIC / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetype + "; charset=utf-8")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; img-src 'self' https://i.ytimg.com; connect-src 'self'; script-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            return self.send_json({"error": "Pagina non trovata."}, 404)
        except (ValueError, OverflowError):
            return self.send_json({"error": "Indirizzo non valido."}, 400)
        except Exception:
            LOG.exception("Errore durante la richiesta")
            return self.send_json({"error": "Errore interno. Controlla il terminale."}, 500)

    def json_body(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
            raise ValueError("Serve un corpo JSON.")
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 32_000:
            raise ValueError("Richiesta troppo lunga.")
        return json.loads(self.rfile.read(size))

    def origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        origin_url = urlsplit(origin)
        # A proxy that terminates TLS forwards the browser's https origin unchanged.
        return origin_url.scheme in ("http", "https") and origin_url.netloc == self.headers.get("Host")

    def local_host(self):
        extra = {name.strip().lower() for name in setting("ALLOWED_HOSTS").split(",") if name.strip()}
        return urlsplit("http://" + self.headers.get("Host", "")).hostname in {"127.0.0.1", "localhost", *extra}

    def authenticate(self):
        """HTTP Basic auth, active only when AUTH_PASSWORD is set; names ignore case."""
        if not setting("AUTH_PASSWORD"):
            return LOCAL_OWNER
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        try:
            user, _, given = base64.b64decode(token, validate=True).decode("utf-8").partition(":")
        except ValueError:
            return None
        if scheme.lower() != "basic":
            return None
        found = None
        for account in accounts():  # check them all, so timing does not tell which names exist
            user_ok = hmac.compare_digest(user.lower().encode(), account["name"].lower().encode())
            password_ok = hmac.compare_digest(given.encode(), account["password"].encode())
            if user_ok and password_ok and found is None:
                found = account
        return found

    def forbidden(self):
        return self.send_json({"error": "Questa parte è riservata a chi gestisce l'archivio."}, 403)

    def request_login(self):
        return self.send_json({"error": "Accesso riservato."}, 401,
                              {"WWW-Authenticate": 'Basic realm="Antirez, con calma", charset="UTF-8"'})

    def do_POST(self):
        if not self.local_host() or not self.origin_ok():
            return self.send_json({"error": "Origine non consentita."}, 403)
        self.account = self.authenticate()
        if not self.account:
            return self.request_login()
        path = urlsplit(self.path).path
        if path in ADMIN_ONLY and not self.account["admin"]:
            return self.forbidden()
        try:
            data = self.json_body()
            if not isinstance(data, dict):
                raise ValueError("Richiesta non valida.")
            if path == "/api/sync":
                if not api_key():
                    return self.send_json({"error": "Aggiungi la chiave Gemini nelle Impostazioni."}, 400)
                if ingest.is_running():
                    return self.send_json({"error": "Acquisizione già in corso."}, 409)
                threading.Thread(target=self.background_sync, daemon=True).start()
                return self.send_json({"started": True}, 202)
            if path == "/api/settings":
                return self.send_json(save_settings(data))
            if path == "/api/settings/test":
                key, models, typed = settings_to_test(data)
                result = chat.gemini.check(key, models)
                result["tested"] = "typed" if typed else "saved"
                return self.send_json(result)
            if path == "/api/chat":
                question = data.get("question", "")
                ident = data.get("conversation_id")
                if not isinstance(question, str) or not question.strip() or len(question) > 2000:
                    raise ValueError("Scrivi una domanda di massimo 2000 caratteri.")
                if ident is not None and (not isinstance(ident, int) or isinstance(ident, bool) or ident <= 0):
                    raise ValueError("Conversazione non valida.")
                left, limit = chat_allowance(self.account)
                if left == 0:
                    return self.send_json({"error": f"Hai già fatto le {limit} domande di oggi: "
                                                    "il contatore riparte a mezzanotte."}, 429)
                result = chat.answer(question.strip(), ident, self.account["owner"])
                result["chat_left"] = None if left is None else left - 1
                return self.send_json(result)
            return self.send_json({"error": "Pagina non trovata."}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except chat.gemini.GeminiError as exc:
            return self.send_json({"error": str(exc)}, 502)
        except Exception:
            LOG.exception("Errore durante la richiesta")
            return self.send_json({"error": "Errore interno. Controlla il terminale."}, 500)

    @staticmethod
    def background_sync():
        try:
            ingest.sync()
        except Exception:
            LOG.exception("Acquisizione fallita")


def serve(port=None):
    port = port or int(setting("PORT", "8765"))
    host = setting("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"Antirez è pronto: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
