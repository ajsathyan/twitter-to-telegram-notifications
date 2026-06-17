from twitter_tg_notifs.normalizer import normalize_timeline_response


def test_normalizer_extracts_original_text_links_media_and_poll():
    payload = {
        "data": [
            {
                "id": "101",
                "author_id": "u1",
                "created_at": "2026-06-16T18:14:00.000Z",
                "text": "Demand is rising https://t.co/a",
                "entities": {
                    "urls": [
                        {
                            "url": "https://t.co/a",
                            "expanded_url": "https://example.com/report?utm=tracking",
                            "display_url": "example.com/report",
                            "title": "Grid report",
                        }
                    ]
                },
                "attachments": {"media_keys": ["m1"], "poll_ids": ["p1"]},
            }
        ],
        "includes": {
            "users": [{"id": "u1", "username": "watched", "name": "Watched Account"}],
            "media": [
                {
                    "media_key": "m1",
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/photo.jpg",
                    "alt_text": "Power lines",
                }
            ],
            "polls": [
                {
                    "id": "p1",
                    "options": [
                        {"position": 1, "label": "Option A", "votes": 10},
                        {"position": 2, "label": "Option B", "votes": 12},
                    ],
                    "voting_status": "open",
                }
            ],
        },
    }

    posts = normalize_timeline_response(payload, watched_username="watched", include_reposts=True)

    assert len(posts) == 1
    post = posts[0]
    assert post.kind == "poll"
    assert post.id == "101"
    assert post.author.username == "watched"
    assert "https://t.co/a" not in post.text
    assert post.links[0].url == "https://example.com/report?utm=tracking"
    assert post.links[0].label == "Grid report"
    assert post.media[0].url == "https://pbs.twimg.com/media/photo.jpg"
    assert post.poll is not None
    assert [option.label for option in post.poll.options] == ["Option A", "Option B"]


def test_normalizer_extracts_quote_and_repost_original_links():
    payload = {
        "data": [
            {
                "id": "201",
                "author_id": "u1",
                "created_at": "2026-06-16T18:14:00.000Z",
                "text": "Important context https://t.co/q",
                "entities": {
                    "urls": [
                        {
                            "url": "https://t.co/q",
                            "expanded_url": "https://x.com/original/status/900",
                            "display_url": "x.com/original/status/900",
                        }
                    ]
                },
                "referenced_tweets": [{"type": "quoted", "id": "900"}],
            },
            {
                "id": "202",
                "author_id": "u1",
                "created_at": "2026-06-16T18:15:00.000Z",
                "text": "RT @original: Original text https://t.co/o",
                "referenced_tweets": [{"type": "retweeted", "id": "901"}],
            },
        ],
        "includes": {
            "users": [
                {"id": "u1", "username": "watched", "name": "Watched Account"},
                {"id": "u2", "username": "original", "name": "Original Author"},
            ],
            "tweets": [
                {
                    "id": "900",
                    "author_id": "u2",
                    "created_at": "2026-06-16T17:52:00.000Z",
                    "text": "Quoted original https://t.co/quoted",
                    "entities": {
                        "urls": [
                            {
                                "url": "https://t.co/quoted",
                                "expanded_url": "https://example.com/quoted",
                                "display_url": "example.com/quoted",
                            }
                        ]
                    },
                },
                {
                    "id": "901",
                    "author_id": "u2",
                    "created_at": "2026-06-16T17:53:00.000Z",
                    "text": "Original text https://t.co/original",
                    "entities": {
                        "urls": [
                            {
                                "url": "https://t.co/original",
                                "expanded_url": "https://example.com/original",
                                "display_url": "example.com/original",
                            }
                        ]
                    },
                },
            ],
        },
    }

    posts = normalize_timeline_response(payload, watched_username="watched", include_reposts=True)

    quote = posts[0]
    repost = posts[1]
    assert quote.kind == "quote"
    assert quote.quoted_post is not None
    assert quote.quoted_post.author.username == "original"
    assert quote.quoted_post.links[0].url == "https://example.com/quoted"
    assert repost.kind == "repost"
    assert repost.reposted_post is not None
    assert repost.reposted_post.text == "Original text"
    assert repost.reposted_post.links[0].label == "example.com/original"


def test_normalizer_drops_replies_and_optionally_drops_reposts():
    payload = {
        "data": [
            {
                "id": "301",
                "author_id": "u1",
                "created_at": "2026-06-16T18:14:00.000Z",
                "text": "reply",
                "in_reply_to_user_id": "u2",
            },
            {
                "id": "302",
                "author_id": "u1",
                "created_at": "2026-06-16T18:15:00.000Z",
                "text": "RT @original: Original",
                "referenced_tweets": [{"type": "retweeted", "id": "901"}],
            },
        ],
        "includes": {
            "users": [
                {"id": "u1", "username": "watched", "name": "Watched Account"},
                {"id": "u2", "username": "original", "name": "Original Author"},
            ],
            "tweets": [
                {
                    "id": "901",
                    "author_id": "u2",
                    "created_at": "2026-06-16T17:53:00.000Z",
                    "text": "Original",
                }
            ],
        },
    }

    posts = normalize_timeline_response(payload, watched_username="watched", include_reposts=False)

    assert posts == []
