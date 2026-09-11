from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

DATABASE_DIR = Path("/var/lib/zq-arb")
BACKUP_DIR = Path("/var/backups/zq-arb")
RETENTION_SECONDS = 14 * 24 * 60 * 60


def main() -> int:
    sources = sorted(DATABASE_DIR.glob("*.sqlite3"))
    if not sources:
        print(f"databases not present yet: {DATABASE_DIR}")
        return 0
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source in sources:
        backup_database(source)
    return 0


def backup_database(database: Path) -> None:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    temporary = BACKUP_DIR / f"{database.stem}-{timestamp}.sqlite3.tmp"
    destination = BACKUP_DIR / f"{database.stem}-{timestamp}.sqlite3"
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(temporary)) as target:
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise RuntimeError(f"backup integrity check failed: {result}")
    os.chmod(temporary, 0o600)
    temporary.replace(destination)

    cutoff = time.time() - RETENTION_SECONDS
    for candidate in BACKUP_DIR.glob(f"{database.stem}-*.sqlite3"):
        if candidate.stat().st_mtime < cutoff:
            candidate.unlink()
    print(destination)


if __name__ == "__main__":
    raise SystemExit(main())
