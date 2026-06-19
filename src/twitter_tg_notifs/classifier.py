from __future__ import annotations

import json
from dataclasses import dataclass, field

import requests

from twitter_tg_notifs.config import ClassifierConfig, ClassifierSecrets, NotifierSecrets, TopicFilterConfig
from twitter_tg_notifs.models import NormalizedPost


class ClassifierError(OSError):
    """Raised when a topic classifier cannot return a valid decision."""


@dataclass(frozen=True)
class ClassificationDecision:
    send: bool
    confidence: float
    matched_topics: list[str] = field(default_factory=list)
    reason: str = ""
    provider: str = "unknown"


class NoneClassifier:
    provider = "none"

    def classify(self, post: NormalizedPost, topic_filter: TopicFilterConfig) -> ClassificationDecision:
        return ClassificationDecision(
            send=True,
            confidence=1.0,
            matched_topics=[],
            reason="Filtering disabled by provider.",
            provider=self.provider,
        )


class HTTPJsonClassifier:
    provider = "http_json"

    def __init__(self, url: str, *, session: object | None = None, timeout_seconds: float = 20.0):
        self.url = url
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds

    def classify(self, post: NormalizedPost, topic_filter: TopicFilterConfig) -> ClassificationDecision:
        payload = self._post(
            self.url,
            json={"tweet": post.classifier_payload(), "topic_filter": topic_filter.model_dump()},
        )
        return _decision_from_payload(payload, provider=self.provider)

    def _post(self, url: str, *, json: dict) -> dict:
        try:
            response = self.session.post(url, json=json, timeout=self.timeout_seconds)  # type: ignore[union-attr]
        except requests.RequestException as exc:
            raise ClassifierError(f"Classifier request failed: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise ClassifierError(f"Classifier returned HTTP {response.status_code}")
        return response.json()


class OpenAICompatibleClassifier:
    provider = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        session: object | None = None,
        timeout_seconds: float = 20.0,
        provider: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        if provider:
            self.provider = provider

    def classify(self, post: NormalizedPost, topic_filter: TopicFilterConfig) -> ClassificationDecision:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Classify whether a normalized X post is relevant to the configured topic filter. "
                        "The filter may include freeform instructions and/or topic names. "
                        "Do not summarize, rewrite, or add commentary. Return only strict JSON with "
                        "send, confidence, matched_topics, and reason."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"tweet": post.classifier_payload(), "topic_filter": topic_filter.model_dump()},
                        separators=(",", ":"),
                    ),
                },
            ],
        }
        try:
            response = self.session.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout_seconds,
            )  # type: ignore[union-attr]
        except requests.RequestException as exc:
            raise ClassifierError(f"Classifier request failed: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise ClassifierError(f"Classifier returned HTTP {response.status_code}")
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ClassifierError("Classifier response missing message content") from exc
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ClassifierError("Classifier returned non-JSON content") from exc
        return _decision_from_payload(parsed, provider=self.provider)


def classify_with_policy(
    post: NormalizedPost,
    topic_filter: TopicFilterConfig | None,
    *,
    primary,
    fallback=None,
) -> ClassificationDecision:
    if topic_filter is None or not topic_filter.enabled:
        return ClassificationDecision(send=True, confidence=1.0, reason="Topic filter disabled.", provider="none")

    try:
        decision = _call_classifier(primary, post, topic_filter)
    except Exception as exc:
        if topic_filter.on_filter_error == "send":
            return ClassificationDecision(
                send=True,
                confidence=0.0,
                matched_topics=[],
                reason="Filter failed; configured to send.",
                provider="error-policy",
            )
        if topic_filter.on_filter_error == "fallback" and fallback is not None:
            try:
                return _threshold(_call_classifier(fallback, post, topic_filter), topic_filter)
            except Exception as fallback_exc:
                return ClassificationDecision(
                    send=False,
                    confidence=0.0,
                    matched_topics=[],
                    reason=f"Filter failed and fallback failed: {fallback_exc}",
                    provider="error-policy",
                )
        return ClassificationDecision(
            send=False,
            confidence=0.0,
            matched_topics=[],
            reason=f"Filter failed; configured to skip: {exc}",
            provider="error-policy",
        )
    return _threshold(decision, topic_filter)


def build_classifier_pair(config: ClassifierConfig, secrets: NotifierSecrets | ClassifierSecrets):
    return _build_classifier(config.provider, config, secrets), (
        _build_classifier(config.fallback_provider, config, secrets) if config.fallback_provider else None
    )


def _build_classifier(provider: str | None, config: ClassifierConfig, secrets: NotifierSecrets | ClassifierSecrets):
    if provider in (None, "none"):
        return NoneClassifier()
    if provider == "http_json":
        if not config.http_json_url:
            raise ValueError("classifier.http_json_url is required for http_json provider")
        return HTTPJsonClassifier(config.http_json_url, timeout_seconds=config.timeout_seconds)
    if provider == "xai":
        if secrets.xai_api_key is None:
            raise ValueError("XAI_API_KEY is required for xai classifier provider")
        return OpenAICompatibleClassifier(
            base_url=config.xai_base_url,
            model=config.model,
            api_key=secrets.xai_api_key.get_secret_value(),
            timeout_seconds=config.timeout_seconds,
            provider="xai",
        )
    if provider == "openai_compatible":
        if not config.openai_base_url:
            raise ValueError("classifier.openai_base_url is required for openai_compatible provider")
        api_key = secrets.openai_api_key.get_secret_value() if secrets.openai_api_key else None
        return OpenAICompatibleClassifier(
            base_url=config.openai_base_url,
            model=config.model,
            api_key=api_key,
            timeout_seconds=config.timeout_seconds,
        )
    raise ValueError(f"unknown classifier provider: {provider}")


def _call_classifier(classifier, post: NormalizedPost, topic_filter: TopicFilterConfig) -> ClassificationDecision:
    if hasattr(classifier, "classify"):
        return classifier.classify(post, topic_filter)
    return classifier(post, topic_filter)


def _threshold(decision: ClassificationDecision, topic_filter: TopicFilterConfig) -> ClassificationDecision:
    if decision.send and decision.confidence < topic_filter.confidence_threshold:
        return ClassificationDecision(
            send=False,
            confidence=decision.confidence,
            matched_topics=decision.matched_topics,
            reason=f"Classifier confidence {decision.confidence:.2f} below threshold {topic_filter.confidence_threshold:.2f}.",
            provider=decision.provider,
        )
    return decision


def _decision_from_payload(payload: dict, *, provider: str) -> ClassificationDecision:
    try:
        send = payload["send"]
        confidence = payload["confidence"]
        matched_topics = payload["matched_topics"]
        reason = payload["reason"]
    except KeyError as exc:
        raise ClassifierError(f"Classifier JSON missing key: {exc.args[0]}") from exc
    if not isinstance(send, bool):
        raise ClassifierError("Classifier JSON send must be boolean")
    if not isinstance(confidence, int | float):
        raise ClassifierError("Classifier JSON confidence must be numeric")
    if not 0 <= float(confidence) <= 1:
        raise ClassifierError("Classifier JSON confidence must be between 0 and 1")
    if not isinstance(matched_topics, list) or not all(isinstance(topic, str) for topic in matched_topics):
        raise ClassifierError("Classifier JSON matched_topics must be a list of strings")
    if not isinstance(reason, str):
        raise ClassifierError("Classifier JSON reason must be a string")
    return ClassificationDecision(
        send=send,
        confidence=float(confidence),
        matched_topics=matched_topics,
        reason=reason,
        provider=provider,
    )
