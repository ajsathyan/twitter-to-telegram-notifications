from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class AccountState:
    username: str
    user_id: str | None
    display_name: str | None
    last_seen_tweet_id: str | None
    include_reposts: bool


class SQLiteNotifierState:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def initialize(self) -> None:
        if self.path.parent and str(self.path.parent) != ".":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watched_accounts (
                    username TEXT PRIMARY KEY,
                    user_id TEXT,
                    display_name TEXT,
                    last_seen_tweet_id TEXT,
                    include_reposts INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_tweets (
                    tweet_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    delivery_status TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS classifier_decisions (
                    tweet_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    send INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    matched_topics TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    PRIMARY KEY(tweet_id, username, provider)
                )
                """
            )

    def upsert_account(self, username: str, *, include_reposts: bool) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watched_accounts (username, include_reposts, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    include_reposts = excluded.include_reposts,
                    updated_at = excluded.updated_at
                """,
                (username, int(include_reposts), now),
            )

    def set_user(self, username: str, *, user_id: str, display_name: str) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE watched_accounts
                SET user_id = ?, display_name = ?, updated_at = ?
                WHERE username = ?
                """,
                (user_id, display_name, now, username),
            )

    def set_last_seen(self, username: str, tweet_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE watched_accounts SET last_seen_tweet_id = ?, updated_at = ? WHERE username = ?",
                (tweet_id, _now(), username),
            )

    def get_account(self, username: str) -> AccountState:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT username, user_id, display_name, last_seen_tweet_id, include_reposts
                FROM watched_accounts
                WHERE username = ?
                """,
                (username,),
            ).fetchone()
        if row is None:
            raise KeyError(username)
        return AccountState(
            username=row["username"],
            user_id=row["user_id"],
            display_name=row["display_name"],
            last_seen_tweet_id=row["last_seen_tweet_id"],
            include_reposts=bool(row["include_reposts"]),
        )

    def was_sent(self, tweet_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM sent_tweets WHERE tweet_id = ?", (tweet_id,)).fetchone()
        return row is not None

    def mark_sent(self, tweet_id: str, *, username: str, delivery_status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sent_tweets (tweet_id, username, sent_at, delivery_status)
                VALUES (?, ?, ?, ?)
                """,
                (tweet_id, username, _now(), delivery_status),
            )

    def record_classifier_decision(
        self,
        tweet_id: str,
        *,
        username: str,
        provider: str,
        send: bool,
        confidence: float,
        matched_topics: list[str],
        reason: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO classifier_decisions
                    (tweet_id, username, provider, send, confidence, matched_topics, reason, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tweet_id,
                    username,
                    provider,
                    int(send),
                    confidence,
                    json.dumps(matched_topics),
                    reason,
                    _now(),
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

