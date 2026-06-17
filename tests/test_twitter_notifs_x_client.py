import requests

from twitter_tg_notifs.x_client import RateLimitError, XApiClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
            }
        )
        return self.response


def test_x_client_resolves_username_with_bearer_token():
    session = FakeSession(
        FakeResponse(payload={"data": {"id": "2244994945", "username": "XDevelopers", "name": "X Dev"}})
    )
    client = XApiClient("bearer-token", session=session)

    user = client.resolve_user("XDevelopers")

    assert user.id == "2244994945"
    assert user.username == "XDevelopers"
    call = session.calls[0]
    assert call["url"] == "https://api.x.com/2/users/by/username/XDevelopers"
    assert call["headers"]["Authorization"] == "Bearer bearer-token"
    assert "id,name,username" == call["params"]["user.fields"]


def test_x_client_builds_timeline_params_for_since_id_and_exclusions():
    session = FakeSession(FakeResponse(payload={"data": [], "meta": {"result_count": 0}}))
    client = XApiClient("bearer-token", session=session)

    payload = client.get_user_posts(
        "123",
        since_id="100",
        include_reposts=False,
        exclude_replies=True,
        max_results=10,
    )

    assert payload["meta"]["result_count"] == 0
    params = session.calls[0]["params"]
    assert session.calls[0]["url"] == "https://api.x.com/2/users/123/tweets"
    assert params["since_id"] == "100"
    assert params["max_results"] == 10
    assert params["exclude"] == "replies,retweets"
    assert "referenced_tweets" in params["tweet.fields"]
    assert "attachments.media_keys" in params["expansions"]
    assert "attachments.poll_ids" in params["expansions"]
    assert "referenced_tweets.id" in params["expansions"]
    assert "entities.mentions.username" not in params["expansions"]
    assert "variants" in params["media.fields"]
    assert "options" in params["poll.fields"]


def test_x_client_raises_rate_limit_error_with_reset_header():
    session = FakeSession(
        FakeResponse(
            status_code=429,
            payload={"title": "Too Many Requests"},
            headers={"x-rate-limit-reset": "1781716500"},
            text="rate limited",
        )
    )
    client = XApiClient("bearer-token", session=session)

    try:
        client.get_user_posts("123")
    except RateLimitError as exc:
        assert exc.reset_epoch == 1781716500
        assert "rate limited" in str(exc)
    else:
        raise AssertionError("429 should raise RateLimitError")


def test_x_client_wraps_network_errors_without_leaking_token():
    class BrokenSession:
        def get(self, *args, **kwargs):
            raise requests.Timeout("bearer-token should not appear")

    client = XApiClient("bearer-token", session=BrokenSession())

    try:
        client.resolve_user("account")
    except OSError as exc:
        assert "bearer-token" not in str(exc)
        assert "X API request failed" in str(exc)
    else:
        raise AssertionError("network failure should raise OSError")
