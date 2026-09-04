from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

SOURCE = Path("/var/lib/zq-arb/engine.sqlite3")
BACKUP_DIR = Path("/var/backups/zq-arb")
RETENTION_SECONDS = 14 * 24 * 60 * 60


def main() -> int:
    if not SOURCE.is_file():
        print(f"database not present yet: {SOURCE}")
        return 0
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    temporary = BACKUP_DIR / f"engine-{timestamp}.sqlite3.tmp"
    destination = BACKUP_DIR / f"engine-{timestamp}.sqlite3"
    with sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True) as source:
        with sqlite3.connect(temporary) as target:
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise RuntimeError(f"backup integrity check failed: {result}")
    os.chmod(temporary, 0o600)
    temporary.replace(destination)

    cutoff = time.time() - RETENTION_SECONDS
    for candidate in BACKUP_DIR.glob("engine-*.sqlite3"):
        if candidate.stat().st_mtime < cutoff:
            candidate.unlink()
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
