from __future__ import annotations

from dataclasses import dataclass

import requests

from twitter_tg_notifs.models import UserRef
from twitter_tg_notifs.normalizer import normalize_timeline_response


class RateLimitError(OSError):
    def __init__(self, message: str, *, reset_epoch: int | None = None):
        super().__init__(message)
        self.reset_epoch = reset_epoch


@dataclass(frozen=True)
class XApiClient:
    bearer_token: str
    base_url: str = "https://api.x.com/2"
    timeout_seconds: float = 20.0
    session: object | None = None

    def __post_init__(self) -> None:
        if self.session is None:
            object.__setattr__(self, "session", requests.Session())

    def resolve_user(self, username: str) -> UserRef:
        payload = self._get(
            f"{self.base_url}/users/by/username/{username}",
            params={"user.fields": "id,name,username"},
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("id"):
            raise ValueError(f"X user not found: {username}")
        return UserRef(id=str(data["id"]), username=str(data.get("username") or username), name=str(data.get("name") or ""))

    def get_user_posts(
        self,
        user_id: str,
        *,
        since_id: str | None = None,
        include_reposts: bool = True,
        exclude_replies: bool = True,
        max_results: int = 10,
    ) -> dict:
        excludes: list[str] = []
        if exclude_replies:
            excludes.append("replies")
        if not include_reposts:
            excludes.append("retweets")

        params: dict[str, str | int] = {
            "max_results": max_results,
            "tweet.fields": ",".join(
                [
                    "id",
                    "text",
                    "created_at",
                    "author_id",
                    "entities",
                    "referenced_tweets",
                    "attachments",
                    "conversation_id",
                    "in_reply_to_user_id",
                    "note_tweet",
                ]
            ),
            "expansions": ",".join(
                [
                    "author_id",
                    "attachments.media_keys",
                    "attachments.poll_ids",
                    "referenced_tweets.id",
                    "referenced_tweets.id.author_id",
                    "referenced_tweets.id.attachments.media_keys",
                ]
            ),
            "media.fields": ",".join(
                [
                    "media_key",
                    "type",
                    "url",
                    "preview_image_url",
                    "variants",
                    "alt_text",
                    "width",
                    "height",
                    "duration_ms",
                ]
            ),
            "poll.fields": "id,options,voting_status,end_datetime,duration_minutes",
            "user.fields": "id,name,username",
        }
        if excludes:
            params["exclude"] = ",".join(excludes)
        if since_id:
            params["since_id"] = since_id

        return self._get(f"{self.base_url}/users/{user_id}/tweets", params=params)

    def get_normalized_posts(
        self,
        user_id: str,
        *,
        watched_username: str,
        since_id: str | None,
        include_reposts: bool,
        exclude_replies: bool,
        max_results: int = 10,
    ):
        payload = self.get_user_posts(
            user_id,
            since_id=since_id,
            include_reposts=include_reposts,
            exclude_replies=exclude_replies,
            max_results=max_results,
        )
        return normalize_timeline_response(payload, watched_username=watched_username, include_reposts=include_reposts)

    def _get(self, url: str, *, params: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        try:
            response = self.session.get(url, headers=headers, params=params, timeout=self.timeout_seconds)  # type: ignore[union-attr]
        except requests.RequestException as exc:
            raise OSError(f"X API request failed: {exc.__class__.__name__}") from exc

        if response.status_code == 429:
            reset_value = response.headers.get("x-rate-limit-reset")
            reset_epoch = int(reset_value) if reset_value and reset_value.isdigit() else None
            raise RateLimitError("X API rate limited: rate limited", reset_epoch=reset_epoch)
        if response.status_code >= 400:
            raise OSError(f"X API returned HTTP {response.status_code}")
        return response.json()
