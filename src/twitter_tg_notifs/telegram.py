from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from twitter_tg_notifs.models import Link, MediaItem, NormalizedPost


class TelegramApiError(OSError):
    """Raised when Telegram delivery fails."""


SHORT_X_URL_RE = re.compile(r"https?://t\.co/\S+")


@dataclass(frozen=True)
class TelegramDelivery:
    method: str
    fallback_used: bool = False


class TelegramFormatter:
    message_limit = 4096
    caption_limit = 1024

    def __init__(self, timezone_name: str = "America/New_York"):
        self.timezone_name = timezone_name
        self.timezone = ZoneInfo(timezone_name)

    def format_post(self, post: NormalizedPost) -> str:
        if post.kind == "repost" and post.reposted_post:
            message = self._format_repost(post)
        elif post.kind == "quote" and post.quoted_post:
            message = self._format_quote(post)
        elif post.kind == "poll":
            message = self._format_poll(post)
        else:
            message = self._format_original(post)
        if len(message) <= self.message_limit:
            return message
        return self._format_original(post, truncate=True)

    def _format_original(self, post: NormalizedPost, *, truncate: bool = False) -> str:
        body = self._escaped_text(post.text)
        if truncate:
            body = self._escaped_text(_excerpt(post.text, 3200)) + "\n\ncontinued on X"
        lines = [f"🟦 {account_link(post.author.username)} posted", "", body]
        lines.extend(self._shared_lines(post.links, "Shared"))
        lines.extend(["", f"🕒 {self._format_time(post.created_at)}", f"🔗 {open_link(post.url)}"])
        return "\n".join(line for line in lines if line != "__DROP__")

    def _format_quote(self, post: NormalizedPost) -> str:
        quoted = post.quoted_post
        assert quoted is not None
        lines = [
            f"💬 {account_link(post.author.username)} quoted {account_link(quoted.author.username)}",
            "",
            self._escaped_text(post.text),
            "",
            "Quoted post:",
            f"“{self._escaped_text(quoted.text)}”",
        ]
        lines.extend(self._shared_lines(post.links, "Shared"))
        lines.extend(self._shared_lines(quoted.links, "Quoted link"))
        lines.extend(["", f"🕒 {self._format_time(post.created_at)}", f"🔗 {open_link(post.url)}"])
        return "\n".join(line for line in lines if line)

    def _format_repost(self, post: NormalizedPost) -> str:
        reposted = post.reposted_post
        assert reposted is not None
        lines = [
            f"🔁 {account_link(post.author.username)} reposted {account_link(reposted.author.username)}",
            "",
            self._escaped_text(reposted.text),
        ]
        lines.extend(self._shared_lines(reposted.links, "Shared"))
        if reposted.created_at:
            lines.extend(["", f"🕒 Original: {self._format_time(reposted.created_at)}"])
        lines.append(f"🔗 {open_link(reposted.url)}")
        return "\n".join(line for line in lines if line)

    def _format_poll(self, post: NormalizedPost) -> str:
        lines = [f"📊 {account_link(post.author.username)} posted a poll", "", self._escaped_text(post.text)]
        if post.poll:
            for option in post.poll.options:
                lines.append(f"{option.position}. {self._escaped_text(option.label)}")
        lines.extend(["", f"🕒 {self._format_time(post.created_at)}", f"🔗 {open_link(post.url)}"])
        return "\n".join(line for line in lines if line)

    def _shared_lines(self, links: list[Link], label: str) -> list[str]:
        if not links:
            return []
        return [""] + [f"🌐 {label}: {html_link(link.url, link.label)}" for link in links]

    def _format_time(self, value: datetime) -> str:
        local = value.astimezone(self.timezone)
        tz_label = "ET" if self.timezone_name == "America/New_York" else local.tzname()
        return f"{local:%b} {local.day}, {local.year} {local.hour % 12 or 12}:{local.minute:02d} {local:%p} {tz_label}"

    def _escaped_text(self, value: str) -> str:
        value = SHORT_X_URL_RE.sub("", value).strip()
        return html.escape(value, quote=True)


class TelegramClient:
    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        formatter: TelegramFormatter | None = None,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = False,
        request_timeout_seconds: float = 20.0,
        session: object | None = None,
        max_retries: int = 3,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.formatter = formatter or TelegramFormatter()
        self.parse_mode = parse_mode
        self.disable_web_page_preview = disable_web_page_preview
        self.request_timeout_seconds = request_timeout_seconds
        self.session = session or requests.Session()
        self.max_retries = max_retries

    def send_post(self, post: NormalizedPost) -> TelegramDelivery:
        message = self.formatter.format_post(post)
        usable_media = [media for media in post.media if media.best_url()]
        if not usable_media and post.kind == "repost" and post.reposted_post:
            usable_media = [media for media in post.reposted_post.media if media.best_url()]
        if not usable_media:
            self.send_message(message)
            return TelegramDelivery(method="sendMessage")

        caption = message if len(message) <= self.formatter.caption_limit else None
        text_sent_before_media = False
        if caption is None:
            self.send_message(message)
            text_sent_before_media = True
        try:
            delivery = self._send_media(usable_media, caption=caption)
        except TelegramApiError:
            if not text_sent_before_media:
                self.send_message(message)
            return TelegramDelivery(method="sendMessage", fallback_used=True)
        return delivery

    def send_message(self, text: str) -> dict:
        return self._post(
            "sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": self.parse_mode,
                "disable_web_page_preview": self.disable_web_page_preview,
            },
        )

    def _send_media(self, media: list[MediaItem], *, caption: str | None) -> TelegramDelivery:
        if len(media) == 1:
            item = media[0]
            if item.type == "photo":
                data = {"chat_id": self.chat_id, "photo": item.best_url(), "parse_mode": self.parse_mode}
                if caption:
                    data["caption"] = caption
                self._post("sendPhoto", data=data)
                return TelegramDelivery(method="sendPhoto")
            if item.type == "animated_gif":
                data = {"chat_id": self.chat_id, "animation": item.best_url(), "parse_mode": self.parse_mode}
                if caption:
                    data["caption"] = caption
                self._post("sendAnimation", data=data)
                return TelegramDelivery(method="sendAnimation")
            if item.type == "video":
                data = {"chat_id": self.chat_id, "video": item.best_url(), "parse_mode": self.parse_mode}
                if caption:
                    data["caption"] = caption
                self._post("sendVideo", data=data)
                return TelegramDelivery(method="sendVideo")
        media_payload = []
        for index, item in enumerate(media[:10]):
            media_type = "photo" if item.type == "photo" else "video"
            entry = {"type": media_type, "media": item.best_url()}
            if index == 0 and caption:
                entry["caption"] = caption
                entry["parse_mode"] = self.parse_mode
            media_payload.append(entry)
        self._post("sendMediaGroup", data={"chat_id": self.chat_id, "media": media_payload})
        return TelegramDelivery(method="sendMediaGroup")

    def _post(self, method: str, *, data: dict | None = None, json: dict | None = None) -> dict:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self.session.post(url, data=data, json=json, timeout=self.request_timeout_seconds)  # type: ignore[union-attr]
            except requests.RequestException as exc:
                if attempts >= self.max_retries:
                    raise TelegramApiError(f"Telegram request failed: {exc.__class__.__name__}") from exc
                time.sleep(min(2**attempts, 30))
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                if response.status_code >= 500 and attempts < self.max_retries:
                    time.sleep(min(2**attempts, 30))
                    continue
                raise TelegramApiError("Telegram returned a non-JSON response") from exc
            if response.status_code == 429 and attempts < self.max_retries:
                retry_after = payload.get("parameters", {}).get("retry_after", 1)
                time.sleep(int(retry_after))
                continue
            if response.status_code >= 500 and attempts < self.max_retries:
                time.sleep(min(2**attempts, 30))
                continue
            if response.status_code >= 400 or payload.get("ok") is False:
                raise TelegramApiError("Telegram API rejected request")
            return payload


def account_link(username: str) -> str:
    return html_link(f"https://x.com/{username}", f"@{username}")


def open_link(url: str) -> str:
    return html_link(url, "Open on X")


def html_link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label, quote=True)}</a>'


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
