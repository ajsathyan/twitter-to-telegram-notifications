from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class UserRef:
    id: str
    username: str
    name: str = ""

    @property
    def profile_url(self) -> str:
        return f"https://x.com/{self.username}"


@dataclass(frozen=True)
class Link:
    url: str
    label: str
    source: str = "shared"


@dataclass(frozen=True)
class MediaItem:
    media_key: str
    type: str
    url: str | None = None
    preview_image_url: str | None = None
    alt_text: str | None = None
    variants: list[dict] = field(default_factory=list)

    def best_url(self) -> str | None:
        if self.url:
            return self.url
        mp4_variants = [
            variant
            for variant in self.variants
            if isinstance(variant, dict)
            and isinstance(variant.get("url"), str)
            and variant.get("content_type") == "video/mp4"
        ]
        if mp4_variants:
            return max(mp4_variants, key=lambda item: int(item.get("bit_rate") or 0))["url"]
        if self.preview_image_url:
            return self.preview_image_url
        return None


@dataclass(frozen=True)
class PollOption:
    position: int
    label: str
    votes: int | None = None


@dataclass(frozen=True)
class Poll:
    id: str
    options: list[PollOption]
    voting_status: str | None = None


@dataclass(frozen=True)
class ReferencedPost:
    id: str
    author: UserRef
    text: str
    created_at: datetime | None
    links: list[Link] = field(default_factory=list)
    media: list[MediaItem] = field(default_factory=list)
    poll: Poll | None = None

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author.username}/status/{self.id}"


@dataclass(frozen=True)
class NormalizedPost:
    id: str
    kind: str
    author: UserRef
    text: str
    created_at: datetime
    watched_username: str
    links: list[Link] = field(default_factory=list)
    media: list[MediaItem] = field(default_factory=list)
    poll: Poll | None = None
    quoted_post: ReferencedPost | None = None
    reposted_post: ReferencedPost | None = None

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author.username}/status/{self.id}"

    def classifier_payload(self) -> dict:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "author": {
                "id": self.author.id,
                "username": self.author.username,
                "name": self.author.name,
            },
            "text": self.text,
            "created_at": self.created_at.isoformat(),
            "url": self.url,
            "links": [{"url": link.url, "label": link.label, "source": link.source} for link in self.links],
            "media": [
                {"type": media.type, "url": media.best_url(), "alt_text": media.alt_text}
                for media in self.media
            ],
        }
        if self.poll:
            payload["poll"] = {
                "id": self.poll.id,
                "options": [
                    {"position": option.position, "label": option.label, "votes": option.votes}
                    for option in self.poll.options
                ],
                "voting_status": self.poll.voting_status,
            }
        if self.quoted_post:
            payload["quoted_post"] = _referenced_payload(self.quoted_post)
        if self.reposted_post:
            payload["reposted_post"] = _referenced_payload(self.reposted_post)
        return payload


def _referenced_payload(post: ReferencedPost) -> dict:
    return {
        "id": post.id,
        "author": {
            "id": post.author.id,
            "username": post.author.username,
            "name": post.author.name,
        },
        "text": post.text,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "url": post.url,
        "links": [{"url": link.url, "label": link.label, "source": link.source} for link in post.links],
        "media": [{"type": media.type, "url": media.best_url(), "alt_text": media.alt_text} for media in post.media],
    }
