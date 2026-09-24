"""Optional systemd user timer, created only when the user explicitly installs it."""

import subprocess
import sys
import time
import urllib.error
import urllib.request
import os
from pathlib import Path

from .config import ROOT


def quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def install():
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "antirez-sync.service").write_text(
        "[Unit]\nDescription=Aggiorna archivio video Antirez\n\n"
        "[Service]\nType=oneshot\n"
        f"Environment={quote('PYTHONPATH=' + str(ROOT))}\n"
        f"ExecStart={quote(sys.executable)} -m antirez sync\n", encoding="utf-8",
    )
    (unit_dir / "antirez-sync.timer").write_text(
        "[Unit]\nDescription=Controlla i video Antirez tre volte al giorno\n\n"
        "[Timer]\nOnCalendar=*-*-* 09,15,21:00:00\nPersistent=true\n"
        "RandomizedDelaySec=5m\n\n[Install]\nWantedBy=timers.target\n", encoding="utf-8",
    )
    (unit_dir / "antirez-web.service").write_text(
        "[Unit]\nDescription=Pagina locale Antirez, con calma\n\n"
        "[Service]\nType=simple\n"
        f"Environment={quote('PYTHONPATH=' + str(ROOT))}\n"
        f"ExecStart={quote(sys.executable)} -m antirez serve\n"
        "Restart=on-failure\nRestartSec=5s\n\n"
        "[Install]\nWantedBy=default.target\n", encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "antirez-sync.timer"], check=True)
    subprocess.run(["systemctl", "--user", "is-active", "--quiet", "antirez-sync.timer"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "antirez-web.service"], check=True)
    subprocess.run(["systemctl", "--user", "restart", "antirez-web.service"], check=True)
    address = f"http://127.0.0.1:{os.environ.get('PORT', '8765')}"
    for _ in range(20):
        try:
            with urllib.request.urlopen(address + "/api/status", timeout=1) as response:
                if response.status == 200:
                    print(f"Pagina locale pronta su {address}; timer attivo alle 09, 15 e 21.")
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(.25)
    raise RuntimeError("Il server non si avvia. Controlla: journalctl --user -u antirez-web.service -n 50")
