from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from twitter_tg_notifs.models import (
    Link,
    MediaItem,
    NormalizedPost,
    Poll,
    PollOption,
    ReferencedPost,
    UserRef,
)


@dataclass(frozen=True)
class AccountState:
    username: str
    user_id: str | None
    display_name: str | None
    last_seen_tweet_id: str | None
    include_reposts: bool


@dataclass(frozen=True)
class PollRunState:
    ran_at: str
    checked_accounts: int
    baselined: int
    sent: int
    would_send: int
    skipped: int
    errors: int
    rate_limited_until: int | None
    status_lines: list[str]
    dry_run: bool


@dataclass(frozen=True)
class PendingDelivery:
    tweet_id: str
    username: str
    post: NormalizedPost
    attempts: int
    last_error: str | None


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS poll_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ran_at TEXT NOT NULL,
                    checked_accounts INTEGER NOT NULL,
                    baselined INTEGER NOT NULL,
                    sent INTEGER NOT NULL,
                    would_send INTEGER NOT NULL,
                    skipped INTEGER NOT NULL,
                    errors INTEGER NOT NULL,
                    rate_limited_until INTEGER,
                    status_lines TEXT NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_deliveries (
                    tweet_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_locks (
                    name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daemon_heartbeat (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    updated_at TEXT NOT NULL,
                    expires_at REAL NOT NULL
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

    def enqueue_pending_delivery(self, post: NormalizedPost, *, username: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO pending_deliveries (tweet_id, username, payload, queued_at)
                VALUES (?, ?, ?, ?)
                """,
                (post.id, username, _post_to_json(post), _now()),
            )

    def pending_deliveries(self, *, limit: int = 50) -> list[PendingDelivery]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT tweet_id, username, payload, attempts, last_error
                FROM pending_deliveries
                ORDER BY queued_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            PendingDelivery(
                tweet_id=row["tweet_id"],
                username=row["username"],
                post=_post_from_json(row["payload"]),
                attempts=int(row["attempts"]),
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def record_pending_error(self, tweet_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pending_deliveries
                SET attempts = attempts + 1, last_error = ?
                WHERE tweet_id = ?
                """,
                (error[:500], tweet_id),
            )

    def delete_pending_delivery(self, tweet_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_deliveries WHERE tweet_id = ?", (tweet_id,))

    def record_poll_result(
        self,
        *,
        checked_accounts: int,
        baselined: int,
        sent: int,
        would_send: int,
        skipped: int,
        errors: int,
        rate_limited_until: int | None,
        status_lines: list[str],
        dry_run: bool,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO poll_runs
                    (ran_at, checked_accounts, baselined, sent, would_send, skipped, errors,
                     rate_limited_until, status_lines, dry_run)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    checked_accounts,
                    baselined,
                    sent,
                    would_send,
                    skipped,
                    errors,
                    rate_limited_until,
                    json.dumps(status_lines),
                    int(dry_run),
                ),
            )

    def latest_poll_result(self) -> PollRunState | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT ran_at, checked_accounts, baselined, sent, would_send, skipped, errors,
                       rate_limited_until, status_lines, dry_run
                FROM poll_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return PollRunState(
            ran_at=row["ran_at"],
            checked_accounts=int(row["checked_accounts"]),
            baselined=int(row["baselined"]),
            sent=int(row["sent"]),
            would_send=int(row["would_send"]),
            skipped=int(row["skipped"]),
            errors=int(row["errors"]),
            rate_limited_until=row["rate_limited_until"],
            status_lines=json.loads(row["status_lines"]),
            dry_run=bool(row["dry_run"]),
        )

    def acquire_runtime_lock(self, name: str, *, ttl_seconds: float) -> str | None:
        owner = uuid.uuid4().hex
        now = time.time()
        expires_at = now + ttl_seconds
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner, expires_at FROM runtime_locks WHERE name = ?", (name,)).fetchone()
            if row is not None and float(row["expires_at"]) > now:
                conn.rollback()
                return None
            conn.execute(
                """
                INSERT INTO runtime_locks (name, owner, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at
                """,
                (name, owner, expires_at),
            )
            conn.commit()
        return owner

    def release_runtime_lock(self, name: str, owner: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM runtime_locks WHERE name = ? AND owner = ?", (name, owner))

    def record_daemon_heartbeat(self, *, ttl_seconds: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daemon_heartbeat (id, updated_at, expires_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (_now(), time.time() + ttl_seconds),
            )

    def daemon_heartbeat_alive(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT expires_at FROM daemon_heartbeat WHERE id = 1").fetchone()
        return row is not None and float(row["expires_at"]) > time.time()

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


def _post_to_json(post: NormalizedPost) -> str:
    return json.dumps(_post_to_data(post), separators=(",", ":"))


def _post_to_data(post: NormalizedPost) -> dict:
    return {
        "id": post.id,
        "kind": post.kind,
        "author": _user_to_data(post.author),
        "text": post.text,
        "created_at": post.created_at.isoformat(),
        "watched_username": post.watched_username,
        "links": [_link_to_data(link) for link in post.links],
        "media": [_media_to_data(media) for media in post.media],
        "poll": _poll_to_data(post.poll) if post.poll else None,
        "quoted_post": _referenced_to_data(post.quoted_post) if post.quoted_post else None,
        "reposted_post": _referenced_to_data(post.reposted_post) if post.reposted_post else None,
    }


def _post_from_json(payload: str) -> NormalizedPost:
    data = json.loads(payload)
    return NormalizedPost(
        id=str(data["id"]),
        kind=str(data["kind"]),
        author=_user_from_data(data["author"]),
        text=str(data["text"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        watched_username=str(data["watched_username"]),
        links=[_link_from_data(item) for item in data.get("links") or []],
        media=[_media_from_data(item) for item in data.get("media") or []],
        poll=_poll_from_data(data["poll"]) if data.get("poll") else None,
        quoted_post=_referenced_from_data(data["quoted_post"]) if data.get("quoted_post") else None,
        reposted_post=_referenced_from_data(data["reposted_post"]) if data.get("reposted_post") else None,
    )


def _referenced_to_data(post: ReferencedPost) -> dict:
    return {
        "id": post.id,
        "author": _user_to_data(post.author),
        "text": post.text,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "links": [_link_to_data(link) for link in post.links],
        "media": [_media_to_data(media) for media in post.media],
        "poll": _poll_to_data(post.poll) if post.poll else None,
    }


def _referenced_from_data(data: dict) -> ReferencedPost:
    return ReferencedPost(
        id=str(data["id"]),
        author=_user_from_data(data["author"]),
        text=str(data["text"]),
        created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        links=[_link_from_data(item) for item in data.get("links") or []],
        media=[_media_from_data(item) for item in data.get("media") or []],
        poll=_poll_from_data(data["poll"]) if data.get("poll") else None,
    )


def _user_to_data(user: UserRef) -> dict:
    return {"id": user.id, "username": user.username, "name": user.name}


def _user_from_data(data: dict) -> UserRef:
    return UserRef(id=str(data["id"]), username=str(data["username"]), name=str(data.get("name") or ""))


def _link_to_data(link: Link) -> dict:
    return {"url": link.url, "label": link.label, "source": link.source}


def _link_from_data(data: dict) -> Link:
    return Link(url=str(data["url"]), label=str(data["label"]), source=str(data.get("source") or "shared"))


def _media_to_data(media: MediaItem) -> dict:
    return {
        "media_key": media.media_key,
        "type": media.type,
        "url": media.url,
        "preview_image_url": media.preview_image_url,
        "alt_text": media.alt_text,
        "variants": media.variants,
    }


def _media_from_data(data: dict) -> MediaItem:
    return MediaItem(
        media_key=str(data["media_key"]),
        type=str(data["type"]),
        url=data.get("url"),
        preview_image_url=data.get("preview_image_url"),
        alt_text=data.get("alt_text"),
        variants=data.get("variants") or [],
    )


def _poll_to_data(poll: Poll) -> dict:
    return {
        "id": poll.id,
        "options": [
            {"position": option.position, "label": option.label, "votes": option.votes}
            for option in poll.options
        ],
        "voting_status": poll.voting_status,
    }


def _poll_from_data(data: dict) -> Poll:
    return Poll(
        id=str(data["id"]),
        options=[
            PollOption(
                position=int(option["position"]),
                label=str(option["label"]),
                votes=option.get("votes"),
            )
            for option in data.get("options") or []
        ],
        voting_status=data.get("voting_status"),
    )
