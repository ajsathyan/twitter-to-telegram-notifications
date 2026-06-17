from datetime import datetime, timezone

from twitter_tg_notifs.classifier import (
    ClassificationDecision,
    ClassifierError,
    HTTPJsonClassifier,
    NoneClassifier,
    OpenAICompatibleClassifier,
    classify_with_policy,
)
from twitter_tg_notifs.config import TopicFilterConfig
from twitter_tg_notifs.models import NormalizedPost, UserRef


def make_post():
    return NormalizedPost(
        id="123",
        kind="post",
        author=UserRef(id="u1", username="account", name="Account"),
        text="AI data centers are increasing electricity demand.",
        created_at=datetime(2026, 6, 16, 18, 14, tzinfo=timezone.utc),
        links=[],
        media=[],
        poll=None,
        quoted_post=None,
        reposted_post=None,
        watched_username="account",
    )


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(self.payload)


def test_none_classifier_always_allows_without_rewriting():
    decision = NoneClassifier().classify(
        make_post(),
        TopicFilterConfig(enabled=True, topics=["nuclear power"]),
    )

    assert decision.send is True
    assert decision.confidence == 1.0
    assert decision.reason == "Filtering disabled by provider."


def test_http_json_classifier_posts_normalized_tweet_and_topic_filter():
    session = FakeSession(
        {
            "send": True,
            "confidence": 0.86,
            "matched_topics": ["AI data center electricity demand"],
            "reason": "The post discusses electricity demand growth from AI infrastructure.",
        }
    )
    classifier = HTTPJsonClassifier("http://localhost:8787/classify", session=session)
    topic_filter = TopicFilterConfig(
        enabled=True,
        topics=["AI data center electricity demand"],
        confidence_threshold=0.7,
    )

    decision = classifier.classify(make_post(), topic_filter)

    assert decision.send is True
    assert decision.confidence == 0.86
    assert decision.matched_topics == ["AI data center electricity demand"]
    assert session.calls[0]["url"] == "http://localhost:8787/classify"
    assert session.calls[0]["json"]["tweet"]["id"] == "123"
    assert session.calls[0]["json"]["topic_filter"]["topics"] == ["AI data center electricity demand"]


def test_http_json_classifier_rejects_out_of_range_confidence():
    session = FakeSession(
        {
            "send": True,
            "confidence": 1.7,
            "matched_topics": ["AI data center electricity demand"],
            "reason": "overconfident",
        }
    )
    classifier = HTTPJsonClassifier("http://localhost:8787/classify", session=session)

    try:
        classifier.classify(make_post(), TopicFilterConfig(enabled=True))
    except ClassifierError as exc:
        assert "between 0 and 1" in str(exc)
    else:
        raise AssertionError("confidence outside 0..1 should fail")


def test_openai_compatible_classifier_parses_chat_completion_json_and_uses_api_key():
    session = FakeSession(
        {
            "choices": [
                {
                    "message": {
                        "content": '{"send": true, "confidence": 0.91, "matched_topics": ["coal exports"], "reason": "Coal export post."}'
                    }
                }
            ]
        }
    )
    classifier = OpenAICompatibleClassifier(
        base_url="https://api.x.ai/v1",
        model="grok-4.3",
        api_key="xai-token",
        session=session,
    )

    decision = classifier.classify(make_post(), TopicFilterConfig(enabled=True, topics=["coal exports"]))

    assert decision.send is True
    assert decision.confidence == 0.91
    call = session.calls[0]
    assert call["url"] == "https://api.x.ai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer xai-token"
    assert call["json"]["response_format"]["type"] == "json_object"
    assert "Do not summarize" in call["json"]["messages"][0]["content"]


def test_classify_with_policy_applies_threshold_and_failure_modes():
    post = make_post()
    topic_filter = TopicFilterConfig(
        enabled=True,
        topics=["AI data center electricity demand"],
        confidence_threshold=0.7,
        on_filter_error="skip",
    )

    low = classify_with_policy(
        post,
        topic_filter,
        primary=lambda *_: ClassificationDecision(
            send=True,
            confidence=0.5,
            matched_topics=["AI data center electricity demand"],
            reason="weak match",
        ),
    )
    skipped = classify_with_policy(
        post,
        topic_filter,
        primary=lambda *_: (_ for _ in ()).throw(RuntimeError("local classifier down")),
    )
    sent_on_error = classify_with_policy(
        post,
        TopicFilterConfig(enabled=True, topics=["utility capex"], on_filter_error="send"),
        primary=lambda *_: (_ for _ in ()).throw(RuntimeError("local classifier down")),
    )
    fallback = classify_with_policy(
        post,
        TopicFilterConfig(enabled=True, topics=["utility capex"], on_filter_error="fallback"),
        primary=lambda *_: (_ for _ in ()).throw(RuntimeError("local classifier down")),
        fallback=lambda *_: ClassificationDecision(
            send=True,
            confidence=0.8,
            matched_topics=["utility capex"],
            reason="fallback match",
        ),
    )

    assert low.send is False
    assert "below threshold" in low.reason
    assert skipped.send is False
    assert "local classifier down" in skipped.reason
    assert sent_on_error.send is True
    assert sent_on_error.reason == "Filter failed; configured to send."
    assert fallback.send is True
    assert fallback.reason == "fallback match"
