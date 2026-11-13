"""Single-owner encrypted persistence with optimistic concurrency control.

A revision number prevents stale browser sessions from silently overwriting data.
It is NOT a multi-tenant/user-account authorization system.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from domain import dumps, loads
from security import encrypt_json, decrypt_json


class SaveConflictError(RuntimeError):
    """Another browser has modified data since this session loaded it."""


class SecureStore:
    def __init__(self, secret_key: str, database_url: str = '', sqlite_path: str = 'data/hyunsense.sqlite3'):
        self.key = secret_key
        self.database_url = database_url.strip()
        self.sqlite_path = Path(sqlite_path)
        encrypt_json(b'test', self.key)

    def _connect(self):
        if self.database_url:
            import psycopg2
            return psycopg2.connect(self.database_url, connect_timeout=8, sslmode='require')
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.sqlite_path, timeout=15)

    def _ensure(self, con):
        cur = con.cursor()
        if self.database_url:
            cur.execute('CREATE TABLE IF NOT EXISTS hyunsense_data ('
                        'id TEXT PRIMARY KEY, payload BYTEA NOT NULL, revision BIGINT NOT NULL DEFAULT 0)')
            cur.execute('ALTER TABLE hyunsense_data ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0')
        else:
            cur.execute('CREATE TABLE IF NOT EXISTS hyunsense_data ('
                        'id TEXT PRIMARY KEY, payload BLOB NOT NULL, revision INTEGER NOT NULL DEFAULT 0)')
            cur.execute('PRAGMA table_info(hyunsense_data)')
            if 'revision' not in {row[1] for row in cur.fetchall()}:
                cur.execute('ALTER TABLE hyunsense_data ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
        con.commit()

    def load_with_revision(self) -> tuple[dict[str, Any] | None, int]:
        con = self._connect()
        try:
            self._ensure(con)
            cur = con.cursor()
            cur.execute("SELECT payload,revision FROM hyunsense_data WHERE id='owner'")
            row = cur.fetchone()
            return (loads(decrypt_json(bytes(row[0]), self.key)), int(row[1])) if row else (None, 0)
        finally:
            con.close()

    def load(self) -> dict[str, Any] | None:
        return self.load_with_revision()[0]

    def save(self, data: dict[str, Any], *, expected_revision: int | None = None) -> int:
        """Save only if expected_revision matches. No silent lost updates."""
        token = encrypt_json(dumps(data), self.key)
        con = self._connect()
        try:
            self._ensure(con)
            cur = con.cursor()
            if expected_revision is None:  # Legacy API, for local tools only; app always supplies revision.
                cur.execute("SELECT revision FROM hyunsense_data WHERE id='owner'")
                row = cur.fetchone()
                expected_revision = int(row[0]) if row else 0
            if expected_revision < 0:
                raise ValueError('revision은 음수일 수 없습니다.')
            if self.database_url:
                import psycopg2
                cur.execute('UPDATE hyunsense_data SET payload=%s,revision=revision+1 '
                            'WHERE id=%s AND revision=%s',
                            (psycopg2.Binary(token), 'owner', expected_revision))
                if expected_revision == 0 and cur.rowcount == 0:
                    cur.execute('INSERT INTO hyunsense_data(id,payload,revision) VALUES (%s,%s,1) '
                                'ON CONFLICT (id) DO NOTHING', ('owner', psycopg2.Binary(token)))
            else:
                cur.execute('UPDATE hyunsense_data SET payload=?,revision=revision+1 '
                            'WHERE id=? AND revision=?', (token, 'owner', expected_revision))
                if expected_revision == 0 and cur.rowcount == 0:
                    cur.execute('INSERT OR IGNORE INTO hyunsense_data(id,payload,revision) VALUES (?,?,1)',
                                ('owner', token))
            if cur.rowcount != 1:
                con.rollback()
                raise SaveConflictError('다른 기기에서 저장된 최신 데이터가 존재합니다.')
            con.commit()
            return expected_revision + 1
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def delete(self, *, expected_revision: int | None = None) -> None:
        con = self._connect()
        try:
            self._ensure(con)
            cur = con.cursor()
            if expected_revision is None:
                cur.execute("DELETE FROM hyunsense_data WHERE id='owner'")
            else:
                if self.database_url:
                    cur.execute('DELETE FROM hyunsense_data WHERE id=%s AND revision=%s',
                                ('owner', expected_revision))
                else:
                    cur.execute('DELETE FROM hyunsense_data WHERE id=? AND revision=?',
                                ('owner', expected_revision))
                if cur.rowcount != 1:
                    cur.execute("SELECT revision FROM hyunsense_data WHERE id='owner'")
                    if cur.fetchone() is not None:
                        con.rollback()
                        raise SaveConflictError('다른 기기의 변경사항이 있어 삭제하지 않았습니다.')
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
