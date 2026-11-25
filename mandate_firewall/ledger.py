"""Tamper-evident, append-only audit ledger.

Each entry stores the hash of the entry before it, so changing, deleting or
reordering any past row breaks verification. Decision entries store their
full inputs, so any decision can be re-run and compared without trusting
the AI or the original process.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .models import canonical_json, sha256_hex

GENESIS = "0" * 64


@dataclass(frozen=True)
class Entry:
    seq: int
    ts: str
    kind: str
    payload: dict
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries: int
    first_bad_seq: int | None = None
    reason: str = ""


def _entry_hash(seq: int, ts: str, kind: str, payload_json: str, prev_hash: str) -> str:
    return sha256_hex(f"{seq}|{ts}|{kind}|{payload_json}|{prev_hash}")


class Ledger:
    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS entries (
                seq INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def append(self, kind: str, payload: dict, ts: datetime) -> Entry:
        row = self.conn.execute(
            "SELECT seq, hash FROM entries ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = (row[0] + 1) if row else 1
        prev = row[1] if row else GENESIS
        ts_s = ts.isoformat()
        payload_json = canonical_json(payload)
        h = _entry_hash(seq, ts_s, kind, payload_json, prev)
        self.conn.execute(
            "INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?)",
            (seq, ts_s, kind, payload_json, prev, h),
        )
        self.conn.commit()
        return Entry(seq, ts_s, kind, json.loads(payload_json), prev, h)

    def entries(self) -> list[Entry]:
        rows = self.conn.execute(
            "SELECT seq, ts, kind, payload, prev_hash, hash FROM entries ORDER BY seq"
        ).fetchall()
        return [Entry(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5]) for r in rows]

    def get(self, seq: int) -> Entry | None:
        r = self.conn.execute(
            "SELECT seq, ts, kind, payload, prev_hash, hash FROM entries WHERE seq = ?",
            (seq,),
        ).fetchone()
        return Entry(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5]) if r else None

    def verify(self) -> VerifyResult:
        rows = self.conn.execute(
            "SELECT seq, ts, kind, payload, prev_hash, hash FROM entries ORDER BY seq"
        ).fetchall()
        prev = GENESIS
        expected_seq = 1
        for seq, ts, kind, payload, prev_hash, h in rows:
            if seq != expected_seq:
                return VerifyResult(False, len(rows), seq, f"Gap in sequence before entry {seq}")
            if prev_hash != prev:
                return VerifyResult(False, len(rows), seq, "Link to previous entry is broken")
            if _entry_hash(seq, ts, kind, payload, prev_hash) != h:
                return VerifyResult(False, len(rows), seq, "Entry content does not match its hash")
            prev = h
            expected_seq += 1
        return VerifyResult(True, len(rows))

    def head(self) -> str:
        row = self.conn.execute("SELECT hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

