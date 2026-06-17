from datetime import datetime, timezone

from twitter_tg_notifs.models import (
    Link,
    MediaItem,
    NormalizedPost,
    Poll,
    PollOption,
    ReferencedPost,
    UserRef,
)
from twitter_tg_notifs.telegram import TelegramClient, TelegramFormatter


def make_user(username="account"):
    return UserRef(id="u1", username=username, name="Account")


def make_post(**overrides):
    data = {
        "id": "123",
        "kind": "post",
        "author": make_user(),
        "text": "Tweet text",
        "created_at": datetime(2026, 6, 16, 18, 14, tzinfo=timezone.utc),
        "links": [],
        "media": [],
        "poll": None,
        "quoted_post": None,
        "reposted_post": None,
        "watched_username": "account",
    }
    data.update(overrides)
    return NormalizedPost(**data)


def test_formatter_escapes_html_and_builds_account_and_open_links():
    formatter = TelegramFormatter(timezone_name="America/New_York")
    post = make_post(
        text='Power <demand> & "load" https://t.co/a',
        links=[Link(url="https://example.com/report?a=1&b=2", label="Grid <report>", source="shared")],
    )

    message = formatter.format_post(post)

    assert '<a href="https://x.com/account">@account</a> posted' in message
    assert "Power &lt;demand&gt; &amp; &quot;load&quot;" in message
    assert '<a href="https://example.com/report?a=1&amp;b=2">Grid &lt;report&gt;</a>' in message
    assert "🕒 Jun 16, 2026 2:14 PM ET" in message
    assert '<a href="https://x.com/account/status/123">Open on X</a>' in message
    assert "https://t.co/a" not in message


def test_formatter_renders_quote_repost_poll_and_long_post():
    formatter = TelegramFormatter(timezone_name="America/New_York")
    original = ReferencedPost(
        id="900",
        author=UserRef(id="u2", username="original", name="Original"),
        text="Original quoted post text.",
        created_at=datetime(2026, 6, 16, 17, 52, tzinfo=timezone.utc),
        links=[Link(url="https://example.com/quoted", label="example.com/quoted", source="quoted")],
        media=[],
        poll=None,
    )
    quote = make_post(kind="quote", text="Watched comment.", quoted_post=original)
    repost = make_post(kind="repost", text="", reposted_post=original)
    poll = make_post(
        kind="poll",
        text="Poll question",
        poll=Poll(
            id="p1",
            options=[
                PollOption(position=1, label="Option A"),
                PollOption(position=2, label="Option B"),
            ],
        ),
    )
    long_post = make_post(text="x" * 5000)

    quote_message = formatter.format_post(quote)
    repost_message = formatter.format_post(repost)
    poll_message = formatter.format_post(poll)
    long_message = formatter.format_post(long_post)

    assert "💬 <a href=\"https://x.com/account\">@account</a> quoted <a href=\"https://x.com/original\">@original</a>" in quote_message
    assert "Quoted post:\n“Original quoted post text.”" in quote_message
    assert "🌐 Quoted link:" in quote_message
    assert "🔁 <a href=\"https://x.com/account\">@account</a> reposted <a href=\"https://x.com/original\">@original</a>" in repost_message
    assert "🕒 Original: Jun 16, 2026 1:52 PM ET" in repost_message
    assert "📊 <a href=\"https://x.com/account\">@account</a> posted a poll" in poll_message
    assert "1. Option A\n2. Option B" in poll_message
    assert len(long_message) <= formatter.message_limit
    assert "continued on X" in long_message


class FakeTelegramSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, json=None, timeout=None):
        self.calls.append({"url": url, "data": data, "json": json, "timeout": timeout})
        return self.responses.pop(0)


class FakeTelegramResponse:
    def __init__(self, ok=True, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": ok, "result": {"message_id": 1}}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class NonJsonTelegramResponse:
    status_code = 502
    text = "<html>bad gateway</html>"

    def json(self):
        raise ValueError("not json")


def test_telegram_client_sends_photo_with_caption_and_falls_back_to_text_on_media_failure():
    formatter = TelegramFormatter(timezone_name="America/New_York")
    post = make_post(media=[MediaItem(media_key="m1", type="photo", url="https://pbs.twimg.com/media/photo.jpg")])
    session = FakeTelegramSession(
        [
            FakeTelegramResponse(ok=False, status_code=400, payload={"ok": False, "description": "bad photo"}),
            FakeTelegramResponse(ok=True),
        ]
    )
    client = TelegramClient(
        bot_token="telegram-token",
        chat_id="-100123",
        formatter=formatter,
        session=session,
    )

    result = client.send_post(post)

    assert result.method == "sendMessage"
    assert result.fallback_used is True
    assert session.calls[0]["url"].endswith("/bottelegram-token/sendPhoto")
    assert session.calls[0]["data"]["photo"] == "https://pbs.twimg.com/media/photo.jpg"
    assert session.calls[1]["url"].endswith("/bottelegram-token/sendMessage")


def test_telegram_client_sends_text_before_media_when_caption_is_too_long():
    formatter = TelegramFormatter(timezone_name="America/New_York")
    post = make_post(
        text="x" * 2000,
        media=[MediaItem(media_key="m1", type="photo", url="https://pbs.twimg.com/media/photo.jpg")],
    )
    session = FakeTelegramSession([FakeTelegramResponse(ok=True), FakeTelegramResponse(ok=True)])
    client = TelegramClient(
        bot_token="telegram-token",
        chat_id="-100123",
        formatter=formatter,
        session=session,
    )

    result = client.send_post(post)

    assert result.method == "sendPhoto"
    assert session.calls[0]["url"].endswith("/bottelegram-token/sendMessage")
    assert session.calls[1]["url"].endswith("/bottelegram-token/sendPhoto")
    assert "caption" not in session.calls[1]["data"]


def test_telegram_client_honors_retry_after_for_rate_limits(monkeypatch):
    sleeps = []
    session = FakeTelegramSession(
        [
            FakeTelegramResponse(
                ok=False,
                status_code=429,
                payload={"ok": False, "parameters": {"retry_after": 2}},
            ),
            FakeTelegramResponse(ok=True),
        ]
    )
    client = TelegramClient(bot_token="telegram-token", chat_id="-100123", session=session)
    monkeypatch.setattr("twitter_tg_notifs.telegram.time.sleep", sleeps.append)

    client.send_message("hello")

    assert sleeps == [2]
    assert len(session.calls) == 2


def test_telegram_client_retries_transient_non_json_gateway_errors(monkeypatch):
    sleeps = []
    session = FakeTelegramSession([NonJsonTelegramResponse(), FakeTelegramResponse(ok=True)])
    client = TelegramClient(bot_token="telegram-token", chat_id="-100123", session=session)
    monkeypatch.setattr("twitter_tg_notifs.telegram.time.sleep", sleeps.append)

    client.send_message("hello")

    assert sleeps == [2]
    assert len(session.calls) == 2
