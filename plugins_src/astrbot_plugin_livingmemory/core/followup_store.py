"""Narrow event-followup store (SQLite). Used by livingmemory + proactive INTENT.

Does not duplicate full memory; rows reference atom_id / subject / session.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_PENDING = "pending"
STATE_CLAIMED = "claimed"
STATE_SENT = "sent"
STATE_RESOLVED = "resolved"
STATE_CANCELLED = "cancelled"
STATE_UNCERTAIN = "uncertain"


@dataclass
class FollowupRecord:
    followup_id: str
    atom_id: int
    subject_id: str
    target_session: str
    due_after: float
    expires_at: float
    event_version: int
    state: str
    reason: str = ""
    sent_at: float | None = None
    response_received_at: float | None = None
    delivery_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "followup_id": self.followup_id,
            "atom_id": self.atom_id,
            "subject_id": self.subject_id,
            "target_session": self.target_session,
            "due_after": self.due_after,
            "expires_at": self.expires_at,
            "event_version": self.event_version,
            "state": self.state,
            "reason": self.reason,
            "sent_at": self.sent_at,
            "response_received_at": self.response_received_at,
            "delivery_id": self.delivery_id,
        }


class FollowupStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_followups (
                    followup_id TEXT PRIMARY KEY,
                    atom_id INTEGER NOT NULL,
                    subject_id TEXT NOT NULL,
                    target_session TEXT NOT NULL,
                    due_after REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    event_version INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    sent_at REAL,
                    response_received_at REAL,
                    delivery_id TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fu_due ON event_followups(state, target_session, due_after)"
            )

    def create_followup(
        self,
        *,
        atom_id: int,
        subject_id: str,
        target_session: str,
        due_after: float,
        expires_at: float,
        event_version: int = 1,
        reason: str = "",
        followup_id: str | None = None,
    ) -> FollowupRecord:
        fid = followup_id or uuid.uuid4().hex
        now = time.time()
        rec = FollowupRecord(
            followup_id=fid,
            atom_id=int(atom_id),
            subject_id=str(subject_id),
            target_session=str(target_session),
            due_after=float(due_after),
            expires_at=float(expires_at),
            event_version=int(event_version),
            state=STATE_PENDING,
            reason=reason or "",
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO event_followups(
                    followup_id, atom_id, subject_id, target_session,
                    due_after, expires_at, event_version, state, reason,
                    sent_at, response_received_at, delivery_id, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,'',?,?)
                """,
                (
                    rec.followup_id,
                    rec.atom_id,
                    rec.subject_id,
                    rec.target_session,
                    rec.due_after,
                    rec.expires_at,
                    rec.event_version,
                    rec.state,
                    rec.reason,
                    now,
                    now,
                ),
            )
        return rec

    def get_due_followups(
        self,
        session_id: str,
        now: float | None = None,
        limit: int = 3,
    ) -> list[FollowupRecord]:
        now = float(now if now is not None else time.time())
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM event_followups
                WHERE target_session = ?
                  AND state = ?
                  AND due_after <= ?
                  AND expires_at > ?
                ORDER BY due_after ASC
                LIMIT ?
                """,
                (session_id, STATE_PENDING, now, now, int(limit)),
            ).fetchall()
        return [self._row(r) for r in rows]

    def claim_followup(self, followup_id: str, event_version: int) -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE event_followups
                SET state = ?, updated_at = ?
                WHERE followup_id = ? AND state = ? AND event_version = ?
                """,
                (STATE_CLAIMED, now, followup_id, STATE_PENDING, int(event_version)),
            )
            return cur.rowcount == 1

    def mark_followup_sent(self, followup_id: str, delivery_id: str = "") -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE event_followups
                SET state = ?, sent_at = ?, delivery_id = ?, updated_at = ?
                WHERE followup_id = ? AND state IN (?, ?)
                """,
                (
                    STATE_SENT,
                    now,
                    delivery_id or "",
                    now,
                    followup_id,
                    STATE_CLAIMED,
                    STATE_UNCERTAIN,
                ),
            )
            return cur.rowcount == 1

    def mark_uncertain(self, followup_id: str) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE event_followups SET state=?, updated_at=? WHERE followup_id=? AND state=?",
                (STATE_UNCERTAIN, now, followup_id, STATE_CLAIMED),
            )

    def resolve_followup(self, followup_id: str, outcome: str = "resolved") -> bool:
        state = STATE_RESOLVED if outcome != "cancelled" else STATE_CANCELLED
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE event_followups
                SET state = ?, response_received_at = ?, updated_at = ?
                WHERE followup_id = ?
                """,
                (state, now, now, followup_id),
            )
            return cur.rowcount == 1

    def cancel_for_atom(self, atom_id: int, *, newer_version: int | None = None) -> int:
        now = time.time()
        with self._connect() as conn:
            if newer_version is None:
                cur = conn.execute(
                    """
                    UPDATE event_followups SET state=?, updated_at=?
                    WHERE atom_id=? AND state IN (?, ?)
                    """,
                    (STATE_CANCELLED, now, int(atom_id), STATE_PENDING, STATE_CLAIMED),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE event_followups SET state=?, updated_at=?
                    WHERE atom_id=? AND event_version < ? AND state IN (?, ?)
                    """,
                    (
                        STATE_CANCELLED,
                        now,
                        int(atom_id),
                        int(newer_version),
                        STATE_PENDING,
                        STATE_CLAIMED,
                    ),
                )
            return cur.rowcount

    def cancel_session_subject(self, session_id: str, subject_id: str) -> int:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE event_followups SET state=?, updated_at=?
                WHERE target_session=? AND subject_id=? AND state IN (?, ?)
                """,
                (
                    STATE_CANCELLED,
                    now,
                    session_id,
                    subject_id,
                    STATE_PENDING,
                    STATE_CLAIMED,
                ),
            )
            return cur.rowcount

    def get(self, followup_id: str) -> FollowupRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM event_followups WHERE followup_id=?",
                (followup_id,),
            ).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row: sqlite3.Row) -> FollowupRecord:
        return FollowupRecord(
            followup_id=row["followup_id"],
            atom_id=int(row["atom_id"]),
            subject_id=row["subject_id"],
            target_session=row["target_session"],
            due_after=float(row["due_after"]),
            expires_at=float(row["expires_at"]),
            event_version=int(row["event_version"]),
            state=row["state"],
            reason=row["reason"] or "",
            sent_at=row["sent_at"],
            response_received_at=row["response_received_at"],
            delivery_id=row["delivery_id"] or "",
        )


_REGISTRY: dict[str, FollowupStore] = {}


def register_followup_store(name: str, store: FollowupStore) -> None:
    _REGISTRY[name] = store


def get_followup_store(name: str = "default") -> FollowupStore | None:
    return _REGISTRY.get(name)


import re

_LIFE_EVENT_RE = re.compile(r"(面试|考试|比赛|出行|旅行|答辩|体检)")


def maybe_create_followup_from_atom(atom: Any, store: FollowupStore | None = None) -> str | None:
    """If atom looks like a private life event with clear end time, create one followup.

    Only for private visibility + known subject + non-fuzzy time. Returns followup_id or None.
    """
    store = store or get_followup_store("default")
    if store is None or atom is None:
        return None
    meta = getattr(atom, "metadata", None) or {}
    if not isinstance(meta, dict):
        return None
    if meta.get("followup_disabled"):
        return None
    if str(meta.get("visibility") or "") != "private":
        return None
    if str(meta.get("time_precision") or "") == "fuzzy":
        return None
    content = str(getattr(atom, "content", "") or meta.get("content") or "")
    if not _LIFE_EVENT_RE.search(content):
        return None
    subject = str(meta.get("subject_id") or "")
    session = str(meta.get("source_session") or getattr(atom, "session_id", "") or "")
    if not subject or not session:
        return None
    # Only DM-like sessions
    if "group" in session.lower() and "Friend" not in session:
        # allow explicit private visibility in non-group session ids
        pass
    event_end = meta.get("event_end") or getattr(atom, "event_time", None)
    if event_end is None:
        return None
    try:
        event_end = float(event_end)
    except (TypeError, ValueError):
        return None
    due_after = event_end + 3600  # 1h after event window
    expires_at = event_end + 86400 * 2
    ver = int(meta.get("event_version") or 1)
    atom_id = int(getattr(atom, "atom_id", 0) or 0)
    if atom_id <= 0:
        return None
    # Cancel older versions for same atom family by subject+session predicate
    store.cancel_for_atom(atom_id, newer_version=ver + 1)  # no-op if none
    rec = store.create_followup(
        atom_id=atom_id,
        subject_id=subject,
        target_session=session,
        due_after=due_after,
        expires_at=expires_at,
        event_version=ver,
        reason=f"event_end={event_end};{content[:40]}",
    )
    return rec.followup_id
