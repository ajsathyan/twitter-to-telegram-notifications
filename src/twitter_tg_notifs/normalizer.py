from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

from twitter_tg_notifs.models import (
    Link,
    MediaItem,
    NormalizedPost,
    Poll,
    PollOption,
    ReferencedPost,
    UserRef,
)


WHITESPACE_RE = re.compile(r"[ \t]+")


def normalize_timeline_response(
    payload: dict,
    *,
    watched_username: str,
    include_reposts: bool,
) -> list[NormalizedPost]:
    users = {item.get("id"): _user_ref(item) for item in payload.get("includes", {}).get("users", [])}
    tweets = {item.get("id"): item for item in payload.get("includes", {}).get("tweets", [])}
    media = {item.get("media_key"): _media_item(item) for item in payload.get("includes", {}).get("media", [])}
    polls = {item.get("id"): _poll(item) for item in payload.get("includes", {}).get("polls", [])}

    normalized: list[NormalizedPost] = []
    for tweet in payload.get("data", []) or []:
        referenced = tweet.get("referenced_tweets") or []
        if tweet.get("in_reply_to_user_id") or any(ref.get("type") == "replied_to" for ref in referenced):
            continue

        repost_ref = _first_reference(referenced, "retweeted")
        if repost_ref and not include_reposts:
            continue

        author = users.get(tweet.get("author_id")) or UserRef(
            id=str(tweet.get("author_id") or ""),
            username=watched_username,
            name=watched_username,
        )
        links = _links_for(tweet, "shared")
        text = _clean_text(_tweet_text(tweet), tweet)
        post_media = _media_for(tweet, media)
        post_poll = _poll_for(tweet, polls)

        quote_ref = _first_reference(referenced, "quoted")
        quoted_post = _referenced_post(quote_ref, tweets, users, media, polls) if quote_ref else None
        reposted_post = _referenced_post(repost_ref, tweets, users, media, polls) if repost_ref else None
        if reposted_post:
            kind = "repost"
        elif quoted_post:
            kind = "quote"
        elif post_poll:
            kind = "poll"
        else:
            kind = "post"

        normalized.append(
            NormalizedPost(
                id=str(tweet["id"]),
                kind=kind,
                author=author,
                text=text,
                created_at=_parse_datetime(tweet["created_at"]),
                links=links,
                media=post_media,
                poll=post_poll,
                quoted_post=quoted_post,
                reposted_post=reposted_post,
                watched_username=watched_username,
            )
        )
    return normalized


def _first_reference(references: list[dict], reference_type: str) -> str | None:
    for reference in references:
        if reference.get("type") == reference_type:
            return str(reference.get("id"))
    return None


def _referenced_post(
    tweet_id: str,
    tweets: dict[str, dict],
    users: dict[str, UserRef],
    media: dict[str, MediaItem],
    polls: dict[str, Poll],
) -> ReferencedPost | None:
    tweet = tweets.get(tweet_id)
    if not tweet:
        return None
    author = users.get(tweet.get("author_id")) or UserRef(
        id=str(tweet.get("author_id") or ""),
        username=str(tweet.get("author_id") or "unknown"),
        name="",
    )
    return ReferencedPost(
        id=str(tweet["id"]),
        author=author,
        text=_clean_text(_tweet_text(tweet), tweet),
        created_at=_parse_datetime(tweet["created_at"]) if tweet.get("created_at") else None,
        links=_links_for(tweet, "quoted"),
        media=_media_for(tweet, media),
        poll=_poll_for(tweet, polls),
    )


def _tweet_text(tweet: dict) -> str:
    note = tweet.get("note_tweet")
    if isinstance(note, dict) and isinstance(note.get("text"), str):
        return note["text"]
    return str(tweet.get("text") or "")


def _clean_text(text: str, tweet: dict) -> str:
    for url in _url_entities(tweet):
        short = url.get("url")
        if isinstance(short, str):
            text = text.replace(short, "")
    text = WHITESPACE_RE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _links_for(tweet: dict, source: str) -> list[Link]:
    links: list[Link] = []
    for url in _url_entities(tweet):
        final_url = url.get("unwound_url") or url.get("expanded_url") or url.get("url")
        if not isinstance(final_url, str) or not _is_http_url(final_url):
            continue
        label = url.get("title") or url.get("display_url") or urlsplit(final_url).netloc or final_url
        links.append(Link(url=final_url, label=str(label), source=source))
    return links


def _url_entities(tweet: dict) -> list[dict]:
    entities = tweet.get("entities") if isinstance(tweet.get("entities"), dict) else {}
    note = tweet.get("note_tweet") if isinstance(tweet.get("note_tweet"), dict) else {}
    note_entities = note.get("entities") if isinstance(note.get("entities"), dict) else {}
    urls = []
    urls.extend(entities.get("urls") or [])
    urls.extend(note_entities.get("urls") or [])
    return [url for url in urls if isinstance(url, dict)]


def _media_for(tweet: dict, media: dict[str, MediaItem]) -> list[MediaItem]:
    attachments = tweet.get("attachments") if isinstance(tweet.get("attachments"), dict) else {}
    return [media[key] for key in attachments.get("media_keys") or [] if key in media]


def _poll_for(tweet: dict, polls: dict[str, Poll]) -> Poll | None:
    attachments = tweet.get("attachments") if isinstance(tweet.get("attachments"), dict) else {}
    for poll_id in attachments.get("poll_ids") or []:
        if poll_id in polls:
            return polls[poll_id]
    return None


def _poll(item: dict) -> Poll:
    return Poll(
        id=str(item.get("id")),
        options=[
            PollOption(
                position=int(option.get("position") or index + 1),
                label=str(option.get("label") or ""),
                votes=option.get("votes"),
            )
            for index, option in enumerate(item.get("options") or [])
            if isinstance(option, dict)
        ],
        voting_status=item.get("voting_status"),
    )


def _media_item(item: dict) -> MediaItem:
    return MediaItem(
        media_key=str(item.get("media_key") or ""),
        type=str(item.get("type") or ""),
        url=item.get("url"),
        preview_image_url=item.get("preview_image_url"),
        alt_text=item.get("alt_text"),
        variants=item.get("variants") or [],
    )


def _user_ref(item: dict) -> UserRef:
    return UserRef(id=str(item.get("id") or ""), username=str(item.get("username") or ""), name=str(item.get("name") or ""))


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in {"http", "https"} and bool(parts.netloc)

