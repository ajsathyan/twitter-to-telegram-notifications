from datetime import datetime, timezone

from twitter_tg_notifs.config import AccountConfig, NotifierConfig, XConfig
from twitter_tg_notifs.models import NormalizedPost, UserRef
from twitter_tg_notifs.service import TwitterTelegramService, build_service, resolve_state_path
from twitter_tg_notifs.state import SQLiteNotifierState
from twitter_tg_notifs.telegram import TelegramDelivery
from twitter_tg_notifs.x_client import RateLimitError


def make_post(tweet_id, text="Tweet text"):
    return NormalizedPost(
        id=str(tweet_id),
        kind="post",
        author=UserRef(id="u1", username="account", name="Account"),
        text=text,
        created_at=datetime(2026, 6, 16, 18, 14, tzinfo=timezone.utc),
        links=[],
        media=[],
        poll=None,
        quoted_post=None,
        reposted_post=None,
        watched_username="account",
    )


def test_sqlite_state_persists_user_last_seen_and_sent_dedupe(tmp_path):
    db_path = tmp_path / "notifier.sqlite3"
    state = SQLiteNotifierState(db_path)
    state.initialize()

    state.upsert_account("account", include_reposts=True)
    state.set_user("account", user_id="123", display_name="Account")
    state.set_last_seen("account", "100")
    assert state.get_account("account").user_id == "123"
    assert state.get_account("account").last_seen_tweet_id == "100"
    assert state.was_sent("200") is False
    state.mark_sent("200", username="account", delivery_status="sent")
    assert state.was_sent("200") is True

    reopened = SQLiteNotifierState(db_path)
    reopened.initialize()
    assert reopened.get_account("account").last_seen_tweet_id == "100"
    assert reopened.was_sent("200") is True


def test_resolve_state_path_uses_config_file_directory_for_relative_paths(tmp_path):
    config_path = tmp_path / "conf" / "config.toml"
    config_path.parent.mkdir()
    config = NotifierConfig(accounts=[])

    assert resolve_state_path(config, config_path=config_path) == config_path.parent / "twitter-tg-notifs.sqlite3"
    assert resolve_state_path(config, config_path=config_path, state_path=tmp_path / "explicit.sqlite3") == (
        tmp_path / "explicit.sqlite3"
    )


def test_build_service_with_no_accounts_does_not_require_secrets(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[polling]\ninterval_seconds = 60\n", encoding="utf-8")
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    service = build_service(config_path=config_path, state_path=tmp_path / "state.sqlite3")
    result = service.run_once()

    assert result.checked_accounts == 0
    assert result.errors == 0


class FakeXClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.resolve_calls = []
        self.timeline_calls = []

    def resolve_user(self, username):
        self.resolve_calls.append(username)
        return UserRef(id="u1", username=username, name="Account")

    def get_normalized_posts(
        self,
        user_id,
        *,
        watched_username,
        since_id,
        include_reposts,
        exclude_replies,
        max_results=None,
    ):
        self.timeline_calls.append(
            {
                "user_id": user_id,
                "watched_username": watched_username,
                "since_id": since_id,
                "include_reposts": include_reposts,
                "exclude_replies": exclude_replies,
                "max_results": max_results,
            }
        )
        return self.batches.pop(0)


class RateLimitedXClient:
    def resolve_user(self, username):
        return UserRef(id="u1", username=username, name="Account")

    def get_normalized_posts(self, *args, **kwargs):
        raise RateLimitError("X API rate limited", reset_epoch=1781716500)


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_post(self, post):
        self.sent.append(post)
        return TelegramDelivery(method="sendMessage", fallback_used=False)


class FailingTelegram:
    def send_post(self, post):
        raise OSError("telegram unavailable")


class ShouldNotBeCalled:
    def __getattr__(self, name):
        raise AssertionError(f"{name} should not be called")


def test_service_with_no_accounts_does_not_call_x_or_telegram(tmp_path):
    config = NotifierConfig(accounts=[])
    state = SQLiteNotifierState(tmp_path / "state.sqlite3")
    service = TwitterTelegramService(
        config=config,
        state=state,
        x_client=ShouldNotBeCalled(),
        telegram=ShouldNotBeCalled(),
    )

    result = service.run_once()

    assert result.checked_accounts == 0
    assert result.sent == 0
    assert result.errors == 0
    assert "No accounts configured" in result.status_lines[0]


def test_service_records_poll_status_and_honors_runtime_lock(tmp_path):
    config = NotifierConfig(accounts=[])
    state = SQLiteNotifierState(tmp_path / "state.sqlite3")
    state.initialize()
    owner = state.acquire_runtime_lock("poll", ttl_seconds=300)
    assert owner is not None
    service = TwitterTelegramService(
        config=config,
        state=state,
        x_client=ShouldNotBeCalled(),
        telegram=ShouldNotBeCalled(),
    )

    result = service.run_once()
    latest = state.latest_poll_result()
    state.release_runtime_lock("poll", owner)

    assert result.errors == 1
    assert "Another notifier instance" in result.status_lines[0]
    assert latest is not None
    assert latest.errors == 1


def test_service_first_run_sets_baseline_without_sending_then_sends_newer_posts(tmp_path):
    config = NotifierConfig(accounts=[AccountConfig(username="account")], x=XConfig(default_include_reposts=True))
    x_client = FakeXClient([[make_post(100), make_post(99)], [make_post(101)]])
    telegram = FakeTelegram()
    state = SQLiteNotifierState(tmp_path / "state.sqlite3")
    service = TwitterTelegramService(config=config, state=state, x_client=x_client, telegram=telegram)

    first = service.run_once()

    assert first.baselined == 1
    assert first.sent == 0
    assert telegram.sent == []
    assert x_client.timeline_calls[0]["since_id"] is None
    second = service.run_once()

    assert state.get_account("account").last_seen_tweet_id == "101"
    assert second.baselined == 0
    assert second.sent == 1
    assert telegram.sent[0].id == "101"
    assert x_client.timeline_calls[1]["since_id"] == "100"
    assert state.was_sent("101") is True


def test_service_dry_run_does_not_send_or_mark_sent_but_advances_last_seen(tmp_path):
    config = NotifierConfig(accounts=[AccountConfig(username="account")])
    x_client = FakeXClient([[make_post(100)], [make_post(101)]])
    telegram = FakeTelegram()
    state = SQLiteNotifierState(tmp_path / "state.sqlite3")
    service = TwitterTelegramService(config=config, state=state, x_client=x_client, telegram=telegram)

    service.run_once()
    result = service.run_once(dry_run=True)

    assert result.would_send == 1
    assert result.sent == 0
    assert telegram.sent == []
    assert state.was_sent("101") is False
    assert state.get_account("account").last_seen_tweet_id == "101"


def test_service_advances_x_cursor_and_queues_pending_when_delivery_fails(tmp_path):
    config = NotifierConfig(accounts=[AccountConfig(username="account")])
    x_client = FakeXClient([[make_post(100)], [make_post(101)]])
    state = SQLiteNotifierState(tmp_path / "state.sqlite3")
    service = TwitterTelegramService(
        config=config,
        state=state,
        x_client=x_client,
        telegram=FailingTelegram(),
    )

    service.run_once()
    result = service.run_once()

    assert result.errors == 1
    assert state.was_sent("101") is False
    assert state.get_account("account").last_seen_tweet_id == "101"
    pending = state.pending_deliveries()
    assert len(pending) == 1
    assert pending[0].tweet_id == "101"
    assert pending[0].attempts == 1


def test_service_retries_pending_delivery_without_refetching_from_x(tmp_path):
    config = NotifierConfig(accounts=[AccountConfig(username="account")])
    x_client = FakeXClient([[make_post(100)], [make_post(101)], []])
    state = SQLiteNotifierState(tmp_path / "state.sqlite3")
    failing = TwitterTelegramService(
        config=config,
        state=state,
        x_client=x_client,
        telegram=FailingTelegram(),
    )
    failing.run_once()
    failing.run_once()

    telegram = FakeTelegram()
    retrying = TwitterTelegramService(config=config, state=state, x_client=x_client, telegram=telegram)
    result = retrying.run_once()

    assert result.sent == 1
    assert telegram.sent[0].id == "101"
    assert state.was_sent("101") is True
    assert state.pending_deliveries() == []
    assert x_client.timeline_calls[-1]["since_id"] == "101"


def test_service_honors_account_repost_setting(tmp_path):
    config = NotifierConfig(
        accounts=[AccountConfig(username="account", include_reposts=False)],
        x=XConfig(default_include_reposts=True, max_results=25),
    )
    x_client = FakeXClient([[make_post(100)]])
    state = SQLiteNotifierState(tmp_path / "state.sqlite3")
    service = TwitterTelegramService(config=config, state=state, x_client=x_client, telegram=FakeTelegram())

    service.run_once()

    assert x_client.timeline_calls[0]["include_reposts"] is False
    assert x_client.timeline_calls[0]["exclude_replies"] is True
    assert x_client.timeline_calls[0]["max_results"] == 25


def test_service_surfaces_x_rate_limit_for_daemon_backoff(tmp_path):
    config = NotifierConfig(accounts=[AccountConfig(username="account")])
    state = SQLiteNotifierState(tmp_path / "state.sqlite3")
    service = TwitterTelegramService(
        config=config,
        state=state,
        x_client=RateLimitedXClient(),
        telegram=FakeTelegram(),
    )

    result = service.run_once()

    assert result.errors == 1
    assert result.rate_limited_until == 1781716500
    assert "rate limited" in result.status_lines[0]
