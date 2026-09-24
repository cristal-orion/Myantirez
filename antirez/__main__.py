import argparse
import logging

from . import db, ingest
from .config import api_key


def main():
    parser = argparse.ArgumentParser(description="Archivio locale di Antirez")
    parser.add_argument("command", choices=["serve", "sync", "status", "install-timer"])
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db.initialize()
    if args.command == "serve":
        from .server import serve
        serve(args.port)
    elif args.command == "sync":
        if not api_key():
            parser.error("Chiave Gemini mancante. Aggiungila nelle Impostazioni o in .env.")
        if not ingest.sync():
            print("Acquisizione già in esecuzione in un altro processo.")
        else:
            print("Acquisizione completata.")
    elif args.command == "install-timer":
        if not api_key():
            parser.error("Configura prima GEMINI_API_KEY nel file .env.")
        from .scheduler import install
        install()
    else:
        print("Archivio:", db.stats(), "Ultimo controllo:", db.setting("last_sync") or "mai")
        if db.setting("sync_error"):
            print("Ultimo errore:", db.setting("sync_error"))


if __name__ == "__main__":
    main()
