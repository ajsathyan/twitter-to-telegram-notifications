import os
import stat
from pathlib import Path

import pytest

from twitter_tg_notifs.config import (
    NotifierSecrets,
    load_classifier_secrets,
    load_notifier_config,
    load_notifier_secrets,
    mask_notifier_secret,
    save_notifier_config,
)


def test_load_notifier_config_reads_toml_accounts_and_topic_filter(tmp_path):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text(
        """
[polling]
interval_seconds = 60
timezone = "America/New_York"

[telegram]
parse_mode = "HTML"
disable_web_page_preview = false

[x]
exclude_replies = true
default_include_reposts = true

[classifier]
provider = "http_json"
fallback_provider = "xai"
http_json_url = "http://localhost:8787/classify"
model = "grok-4.3"

[[accounts]]
username = "@account_a"

[[accounts]]
username = "account_b"
include_reposts = false

[[accounts]]
username = "noisy_account"
include_reposts = true

[accounts.topic_filter]
enabled = true
topics = [
  "nuclear power",
  "AI data center electricity demand"
]
mode = "any"
confidence_threshold = 0.70
on_filter_error = "fallback"
""".strip(),
        encoding="utf-8",
    )

    config = load_notifier_config(config_path)

    assert config.polling.interval_seconds == 60
    assert config.polling.timezone == "America/New_York"
    assert config.telegram.parse_mode == "HTML"
    assert config.x.exclude_replies is True
    assert config.x.default_include_reposts is True
    assert [account.username for account in config.accounts] == [
        "account_a",
        "account_b",
        "noisy_account",
    ]
    assert config.accounts[0].effective_include_reposts(config.x.default_include_reposts) is True
    assert config.accounts[1].effective_include_reposts(config.x.default_include_reposts) is False
    assert config.accounts[2].topic_filter is not None
    assert config.accounts[2].topic_filter.enabled is True
    assert config.accounts[2].topic_filter.topics == [
        "nuclear power",
        "AI data center electricity demand",
    ]
    assert config.accounts[2].topic_filter.on_filter_error == "fallback"
    assert config.classifier.provider == "http_json"
    assert config.classifier.fallback_provider == "xai"
    assert config.classifier.http_json_url == "http://localhost:8787/classify"


def test_save_notifier_config_round_trips_instruction_filter(tmp_path):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text(
        """
[[accounts]]
username = "noisy_account"
include_reposts = false

[accounts.topic_filter]
enabled = true
instructions = "Send only posts about utility capex.\\nSkip broad market commentary."
confidence_threshold = 0.8
on_filter_error = "skip"
""".strip(),
        encoding="utf-8",
    )
    config = load_notifier_config(config_path)

    save_notifier_config(config, config_path)
    loaded = load_notifier_config(config_path)

    assert loaded.accounts[0].include_reposts is False
    assert loaded.accounts[0].topic_filter is not None
    assert loaded.accounts[0].topic_filter.instructions == (
        "Send only posts about utility capex.\nSkip broad market commentary."
    )
    assert loaded.accounts[0].topic_filter.confidence_threshold == 0.8


def test_load_notifier_config_allows_empty_account_list(tmp_path):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text(
        """
[polling]
interval_seconds = 60
""".strip(),
        encoding="utf-8",
    )

    config = load_notifier_config(config_path)

    assert config.accounts == []
    rendered = config_path.with_name("rendered.toml")
    save_notifier_config(config, rendered)
    assert "[[accounts]]" not in rendered.read_text(encoding="utf-8")


def test_example_config_starts_without_accounts():
    config = load_notifier_config(Path("examples/config.toml"))

    assert config.accounts == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable to Windows")
def test_save_notifier_config_preserves_existing_file_mode(tmp_path):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text(
        """
[[accounts]]
username = "noisy_account"
""".strip(),
        encoding="utf-8",
    )
    config_path.chmod(0o640)
    config = load_notifier_config(config_path)

    save_notifier_config(config, config_path)

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640


def test_load_notifier_config_rejects_duplicate_accounts(tmp_path):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text(
        """
[[accounts]]
username = "Account_A"

[[accounts]]
username = "@account_a"
""".strip(),
        encoding="utf-8",
    )

    try:
        load_notifier_config(config_path)
    except ValueError as exc:
        assert "duplicate account username" in str(exc)
    else:
        raise AssertionError("duplicate usernames should fail clearly")


def test_load_notifier_secrets_prefers_environment_over_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "X_BEARER_TOKEN=x-from-file",
                "TELEGRAM_BOT_TOKEN=telegram-from-file",
                "TELEGRAM_CHAT_ID=-100123",
                "XAI_API_KEY=xai-from-file",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("X_BEARER_TOKEN", "x-from-env")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    secrets = load_notifier_secrets(env_file=env_file)

    assert isinstance(secrets, NotifierSecrets)
    assert secrets.x_bearer_token.get_secret_value() == "x-from-env"
    assert secrets.telegram_bot_token.get_secret_value() == "telegram-from-file"
    assert secrets.telegram_chat_id.get_secret_value() == "-100123"
    assert secrets.xai_api_key is not None
    assert secrets.xai_api_key.get_secret_value() == "xai-from-file"


def test_load_classifier_secrets_does_not_require_x_or_telegram(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("XAI_API_KEY=xai-from-file\n", encoding="utf-8")
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    secrets = load_classifier_secrets(env_file=env_file)

    assert secrets.xai_api_key is not None
    assert secrets.xai_api_key.get_secret_value() == "xai-from-file"


def test_notifier_secret_repr_and_dump_are_masked():
    secrets = NotifierSecrets(
        x_bearer_token="x-secret-token",
        telegram_bot_token="telegram-secret-token",
        telegram_chat_id="-100123456789",
        xai_api_key="xai-secret-token",
    )

    rendered = f"{secrets!r} {secrets} {secrets.model_dump()}"
    assert "x-secret-token" not in rendered
    assert "telegram-secret-token" not in rendered
    assert "-100123456789" not in rendered
    assert "xai-secret-token" not in rendered
    assert mask_notifier_secret("abcdefghijkl") == "abcd...ijkl"
    assert mask_notifier_secret("short") == "****"
