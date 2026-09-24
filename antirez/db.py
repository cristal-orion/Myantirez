import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import DATA_DIR

DB_PATH = DATA_DIR / "archive.sqlite3"
STOPWORDS = {"a", "al", "alla", "che", "chi", "come", "con", "cosa", "da", "dei", "del", "della", "di",
             "e", "gli", "ha", "i", "il", "in", "la", "le", "lo", "mi", "nei", "nel", "per", "quali",
             "quello", "si", "sono", "su", "tra", "un", "una"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA journal_mode=WAL")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize():
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, published TEXT NOT NULL,
                url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                summary TEXT NOT NULL DEFAULT '', transcript TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', transcript_method TEXT NOT NULL DEFAULT 'audio', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passages (
                id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                position INTEGER NOT NULL, body TEXT NOT NULL, embedding TEXT,
                UNIQUE(video_id, position)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS passage_search USING fts5(body, content='passages', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
            CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN
                INSERT INTO passage_search(rowid, body) VALUES (new.id, new.body);
            END;
            CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN
                INSERT INTO passage_search(passage_search, rowid, body) VALUES('delete', old.id, old.body);
            END;
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
        """)
        if "transcript_method" not in {row["name"] for row in db.execute("PRAGMA table_info(videos)")}:
            db.execute("ALTER TABLE videos ADD COLUMN transcript_method TEXT NOT NULL DEFAULT 'audio'")
        # "" is the owner of the archive; guests get their own conversations.
        if "owner" not in {row["name"] for row in db.execute("PRAGMA table_info(conversations)")}:
            db.execute("ALTER TABLE conversations ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_owner ON conversations(owner, updated_at)")


def setting(key, value=None):
    with connect() as db:
        if value is None:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else ""
        db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def upsert_video(video):
    with connect() as db:
        db.execute("""INSERT INTO videos(id,title,published,url,created_at,updated_at)
                      VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                      title=excluded.title, published=excluded.published, url=excluded.url,
                      updated_at=excluded.updated_at""",
                   (video["id"], video["title"], video["published"], video["url"], now(), now()))


def update_video(video_id, **fields):
    allowed = {"status", "summary", "transcript", "transcript_method", "error"}
    if not fields or set(fields) - allowed:
        raise ValueError("Invalid video update")
    fields["updated_at"] = now()
    with connect() as db:
        db.execute(f"UPDATE videos SET {', '.join(key + '=?' for key in fields)} WHERE id=?", (*fields.values(), video_id))


def video(video_id):
    with connect() as db:
        row = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        return dict(row) if row else None


def videos(query="", limit=100, offset=0):
    with connect() as db:
        if query.strip():
            words = re.findall(r"\w+", query, flags=re.UNICODE)[:12]
            if not words:
                return []
            match = " OR ".join('"' + word.replace('"', '') + '"' for word in words)
            rows = db.execute("""SELECT v.id, v.title, v.published, v.url, v.status, v.summary, v.error
                 FROM videos v WHERE v.title LIKE ? OR v.summary LIKE ? OR v.id IN
                   (SELECT p2.video_id FROM passage_search JOIN passages p2 ON p2.id=passage_search.rowid
                    WHERE passage_search MATCH ?)
                 ORDER BY v.published DESC LIMIT ? OFFSET ?""",
                (f"%{query}%", f"%{query}%", match, limit, offset)).fetchall()
        else:
            rows = db.execute("""SELECT id,title,published,url,status,summary,error FROM videos
                                 ORDER BY published DESC LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
        return [dict(row) for row in rows]


def replace_passages(video_id, bodies):
    with connect() as db:
        db.execute("DELETE FROM passages WHERE video_id=?", (video_id,))
        for position, body in enumerate(bodies):
            db.execute("INSERT INTO passages(video_id,position,body) VALUES(?,?,?)", (video_id, position, body))


def unset_embeddings():
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT id,body FROM passages WHERE embedding IS NULL ORDER BY id")]


def save_embeddings(pairs):
    with connect() as db:
        db.executemany("UPDATE passages SET embedding=? WHERE id=?", [(json.dumps(vector), ident) for ident, vector in pairs])


def passages():
    with connect() as db:
        return [dict(row) for row in db.execute("""SELECT p.id,p.video_id,p.body,p.embedding,v.title,v.url
                     FROM passages p JOIN videos v ON v.id=p.video_id WHERE v.status='ready'""")]


def lexical_passages(question, limit=24):
    words = [word for word in re.findall(r"\w+", question.lower(), flags=re.UNICODE)
             if word not in STOPWORDS][:12]
    if not words:
        return []
    match = " OR ".join('"' + word.replace('"', '') + '"' for word in words)
    with connect() as db:
        return [dict(row) for row in db.execute("""SELECT p.id,p.video_id,p.body,p.embedding,v.title,v.url
            FROM passage_search JOIN passages p ON p.id=passage_search.rowid JOIN videos v ON v.id=p.video_id
            WHERE passage_search MATCH ? AND v.status='ready' ORDER BY bm25(passage_search) LIMIT ?""",
            (match, limit))]


def conversations(owner=""):
    with connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM conversations WHERE owner=? ORDER BY updated_at DESC", (owner,))]


def conversation(ident, owner=""):
    with connect() as db:
        row = db.execute("SELECT * FROM conversations WHERE id=? AND owner=?", (ident, owner)).fetchone()
        if not row:
            return None
        messages = [dict(msg) for msg in db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (ident,))]
        for msg in messages:
            msg["sources"] = json.loads(msg["sources"])
        return {**dict(row), "messages": messages}


def add_conversation(title, owner=""):
    with connect() as db:
        cursor = db.execute("INSERT INTO conversations(title,owner,created_at,updated_at) VALUES(?,?,?,?)",
                            (title, owner, now(), now()))
        return cursor.lastrowid


def today_start(zone="Europe/Rome"):
    """Midnight in Italy as a UTC timestamp, comparable with created_at."""
    midnight = datetime.now(ZoneInfo(zone)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def questions_since(owner, since):
    with connect() as db:
        return db.execute("""SELECT count(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id
                             WHERE c.owner=? AND m.role='user' AND m.created_at >= ?""", (owner, since)).fetchone()[0]


def add_message(conversation_id, role, content, sources=None):
    with connect() as db:
        db.execute("INSERT INTO messages(conversation_id,role,content,sources,created_at) VALUES(?,?,?,?,?)",
                   (conversation_id, role, content, json.dumps(sources or []), now()))
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), conversation_id))


def stats():
    with connect() as db:
        return {row["status"]: row["n"] for row in db.execute("SELECT status,count(*) n FROM videos GROUP BY status")}
