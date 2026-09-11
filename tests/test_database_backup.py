from __future__ import annotations

import importlib.util
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def test_backup_keeps_paper_and_live_separate_with_readable_snapshots(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "backup_sqlite", Path(__file__).resolve().parents[1] / "deploy" / "backup_sqlite.py"
    )
    assert spec and spec.loader
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    backup.DATABASE_DIR = tmp_path / "databases"
    backup.BACKUP_DIR = tmp_path / "backups"
    backup.DATABASE_DIR.mkdir()
    backup.BACKUP_DIR.mkdir()
    for mode in ("paper", "live"):
        with (
            closing(sqlite3.connect(backup.DATABASE_DIR / f"{mode}.sqlite3")) as connection,
            connection,
        ):
            connection.execute("CREATE TABLE marker (environment TEXT)")
            connection.execute("INSERT INTO marker VALUES (?)", (mode,))
        old = backup.BACKUP_DIR / f"{mode}-20000101T000000Z.sqlite3"
        old.touch()
        os.utime(old, (1, 1))
    assert backup.main() == 0
    snapshots = sorted(backup.BACKUP_DIR.glob("*.sqlite3"))
    assert len(snapshots) == 2
    for snapshot in snapshots:
        with closing(sqlite3.connect(snapshot)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert connection.execute("SELECT environment FROM marker").fetchone() == (
                snapshot.stem.split("-")[0],
            )
    assert not list(backup.BACKUP_DIR.glob("*.tmp"))
