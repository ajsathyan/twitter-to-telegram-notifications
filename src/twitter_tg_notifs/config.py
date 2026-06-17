from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer, field_validator, model_validator


def mask_notifier_secret(value: str | SecretStr | None) -> str:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


class TopicFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    instructions: str = ""
    topics: list[str] = Field(default_factory=list)
    mode: Literal["any", "all"] = "any"
    confidence_threshold: float = Field(default=0.70, ge=0, le=1)
    on_filter_error: Literal["skip", "send", "fallback"] = "skip"

    @field_validator("instructions")
    @classmethod
    def clean_instructions(cls, value: str) -> str:
        return value.strip()

    @field_validator("topics")
    @classmethod
    def validate_topics(cls, value: list[str]) -> list[str]:
        cleaned = [topic.strip() for topic in value if topic.strip()]
        if value and not cleaned:
            raise ValueError("topic_filter topics cannot be blank")
        return cleaned


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    include_reposts: bool | None = None
    topic_filter: TopicFilterConfig | None = None

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        username = value.strip().lstrip("@").lower()
        if not username:
            raise ValueError("account username cannot be blank")
        if any(char.isspace() for char in username):
            raise ValueError("account username cannot contain whitespace")
        return username

    def effective_include_reposts(self, default: bool) -> bool:
        return default if self.include_reposts is None else self.include_reposts


class PollingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_seconds: int = Field(default=60, ge=5)
    timezone: str = "America/New_York"


class TelegramConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_mode: Literal["HTML"] = "HTML"
    disable_web_page_preview: bool = False
    request_timeout_seconds: float = Field(default=20.0, gt=0)


class XConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclude_replies: bool = True
    default_include_reposts: bool = True
    max_results: int = Field(default=10, ge=5, le=100)
    request_timeout_seconds: float = Field(default=20.0, gt=0)


class ClassifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["none", "xai", "openai_compatible", "http_json"] = "none"
    fallback_provider: Literal["none", "xai", "openai_compatible", "http_json"] | None = None
    http_json_url: str | None = None
    openai_base_url: str | None = None
    xai_base_url: str = "https://api.x.ai/v1"
    model: str = "grok-4.3"
    timeout_seconds: float = Field(default=20.0, gt=0)


class StateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path = Path("twitter-tg-notifs.sqlite3")


class NotifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    polling: PollingConfig = Field(default_factory=PollingConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    x: XConfig = Field(default_factory=XConfig)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    accounts: list[AccountConfig]

    @model_validator(mode="after")
    def validate_accounts(self) -> "NotifierConfig":
        if not self.accounts:
            raise ValueError("at least one account is required")
        seen: set[str] = set()
        for account in self.accounts:
            if account.username in seen:
                raise ValueError(f"duplicate account username: {account.username}")
            seen.add(account.username)
        return self


def save_notifier_config(config: NotifierConfig, path: Path | str) -> None:
    config_path = Path(path)
    if config_path.parent and str(config_path.parent) != ".":
        config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config_path.with_name(f".{config_path.name}.tmp")
    temp_path.write_text(render_notifier_config(config), encoding="utf-8")
    if config_path.exists():
        os.chmod(temp_path, config_path.stat().st_mode)
    os.replace(temp_path, config_path)


def render_notifier_config(config: NotifierConfig) -> str:
    lines: list[str] = []
    _write_section(
        lines,
        "polling",
        {
            "interval_seconds": config.polling.interval_seconds,
            "timezone": config.polling.timezone,
        },
    )
    _write_section(
        lines,
        "telegram",
        {
            "parse_mode": config.telegram.parse_mode,
            "disable_web_page_preview": config.telegram.disable_web_page_preview,
            "request_timeout_seconds": config.telegram.request_timeout_seconds,
        },
    )
    _write_section(
        lines,
        "x",
        {
            "exclude_replies": config.x.exclude_replies,
            "default_include_reposts": config.x.default_include_reposts,
            "max_results": config.x.max_results,
            "request_timeout_seconds": config.x.request_timeout_seconds,
        },
    )
    classifier_values = {
        "provider": config.classifier.provider,
        "fallback_provider": config.classifier.fallback_provider,
        "http_json_url": config.classifier.http_json_url,
        "openai_base_url": config.classifier.openai_base_url,
        "xai_base_url": config.classifier.xai_base_url,
        "model": config.classifier.model,
        "timeout_seconds": config.classifier.timeout_seconds,
    }
    _write_section(lines, "classifier", {key: value for key, value in classifier_values.items() if value is not None})
    _write_section(lines, "state", {"path": str(config.state.path)})
    for account in config.accounts:
        lines.append("")
        lines.append("[[accounts]]")
        lines.append(f"username = {_toml_value(account.username)}")
        if account.include_reposts is not None:
            lines.append(f"include_reposts = {_toml_value(account.include_reposts)}")
        if account.topic_filter is not None:
            topic_filter = account.topic_filter
            lines.append("")
            lines.append("[accounts.topic_filter]")
            lines.append(f"enabled = {_toml_value(topic_filter.enabled)}")
            if topic_filter.instructions:
                lines.append(f"instructions = {_toml_value(topic_filter.instructions)}")
            lines.append(f"topics = {_toml_value(topic_filter.topics)}")
            lines.append(f"mode = {_toml_value(topic_filter.mode)}")
            lines.append(f"confidence_threshold = {_toml_value(topic_filter.confidence_threshold)}")
            lines.append(f"on_filter_error = {_toml_value(topic_filter.on_filter_error)}")
    return "\n".join(lines).strip() + "\n"


def _write_section(lines: list[str], name: str, values: dict[str, object]) -> None:
    if lines:
        lines.append("")
    lines.append(f"[{name}]")
    for key, value in values.items():
        lines.append(f"{key} = {_toml_value(value)}")


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return _quote_toml_string(str(value))


def _quote_toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\f", "\\f")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


class NotifierSecrets(BaseModel):
    x_bearer_token: SecretStr = Field(repr=False)
    telegram_bot_token: SecretStr = Field(repr=False)
    telegram_chat_id: SecretStr = Field(repr=False)
    xai_api_key: SecretStr | None = Field(default=None, repr=False)
    openai_api_key: SecretStr | None = Field(default=None, repr=False)

    @field_serializer("x_bearer_token", "telegram_bot_token", "telegram_chat_id", "xai_api_key", "openai_api_key")
    def serialize_secret(self, value: SecretStr | None) -> str:
        return mask_notifier_secret(value)


def load_notifier_config(path: Path | str) -> NotifierConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return NotifierConfig.model_validate(data)


def load_notifier_secrets(env_file: Path | str | None = None) -> NotifierSecrets:
    values: dict[str, str] = {}
    if env_file is not None:
        path = Path(env_file)
        if path.exists() and not path.is_file():
            raise ValueError(f"Env path must be a file: {path}")
        if path.is_file():
            values.update({key: value for key, value in dotenv_values(path).items() if isinstance(value, str)})

    for key in ("X_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "XAI_API_KEY", "OPENAI_API_KEY"):
        env_value = os.environ.get(key)
        if env_value:
            values[key] = env_value

    missing = [
        key
        for key in ("X_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        if not values.get(key)
    ]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")

    return NotifierSecrets(
        x_bearer_token=values["X_BEARER_TOKEN"],
        telegram_bot_token=values["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=values["TELEGRAM_CHAT_ID"],
        xai_api_key=values.get("XAI_API_KEY"),
        openai_api_key=values.get("OPENAI_API_KEY"),
    )
