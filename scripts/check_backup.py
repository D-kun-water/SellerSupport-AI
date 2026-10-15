"""Verify an SQLite backup and restore without overwriting existing files."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('Database integrity check failed.')
        tables=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result={}
        for table in tables:
            quoted='"'+table.replace('"','""')+'"'
            rows=db.execute('SELECT * FROM '+quoted+' ORDER BY 1').fetchall()
            raw=json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode()
            result[table]=dict(count=len(rows),sha256=hashlib.sha256(raw).hexdigest())
        return result


def verify(source,backup,restored):
    source,backup,restored=map(Path,(source,backup,restored))
    if not source.is_file() or backup.exists() or restored.exists() or backup.resolve()==restored.resolve():
        raise ValueError('Use an existing database and two new backup/restore paths.')
    expected=snapshot(source)
    with closing(sqlite3.connect(source)) as db,closing(sqlite3.connect(backup)) as target:
        db.backup(target)
    with closing(sqlite3.connect(backup)) as db,closing(sqlite3.connect(restored)) as target:
        db.backup(target)
    if expected!=snapshot(backup) or expected!=snapshot(restored):
        raise ValueError('Restored database differs from its source.')
    return dict(state='passed',tables=expected)
