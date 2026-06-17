from types import SimpleNamespace

from twitter_tg_notifs.cli import main


def test_twitter_notifs_validate_config_command_masks_secrets(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "notifier.toml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        """
[[accounts]]
username = "account"
""".strip(),
        encoding="utf-8",
    )
    env_path.write_text(
        "\n".join(
            [
                "X_BEARER_TOKEN=x-super-secret",
                "TELEGRAM_BOT_TOKEN=telegram-super-secret",
                "TELEGRAM_CHAT_ID=-100123456",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    exit_code = main(["validate-config", "--config", str(config_path), "--env-file", str(env_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "1 account" in captured.out
    assert "x-su...cret" in captured.out
    assert "x-super-secret" not in captured.out
    assert "telegram-super-secret" not in captured.out
    assert "-100123456" not in captured.out


def test_twitter_notifs_dry_run_uses_service_once(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "notifier.toml"
    env_path = tmp_path / ".env"
    state_path = tmp_path / "state.sqlite3"
    config_path.write_text("[[accounts]]\nusername = \"account\"\n", encoding="utf-8")
    env_path.write_text(
        "X_BEARER_TOKEN=x\nTELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=c\n",
        encoding="utf-8",
    )
    calls = {}

    class FakeService:
        def run_once(self, dry_run=False):
            calls["dry_run"] = dry_run
            return SimpleNamespace(
                checked_accounts=1,
                baselined=0,
                sent=0,
                would_send=2,
                skipped=0,
                errors=0,
                status_lines=["Would send @account status 123"],
            )

    def fake_build_service(*, config_path, env_file, state_path, dry_run=False):
        calls["config_path"] = config_path
        calls["env_file"] = env_file
        calls["state_path"] = state_path
        calls["build_dry_run"] = dry_run
        return FakeService()

    monkeypatch.setattr("twitter_tg_notifs.cli.build_service", fake_build_service)

    exit_code = main(
        [
            "dry-run",
            "--config",
            str(config_path),
            "--env-file",
            str(env_path),
            "--state-db",
            str(state_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls["dry_run"] is True
    assert calls["build_dry_run"] is True
    assert calls["state_path"] == state_path
    assert "Would send @account status 123" in captured.out
    assert "would_send=2" in captured.out


def test_twitter_notifs_dry_run_can_save_output_file(tmp_path, monkeypatch):
    config_path = tmp_path / "notifier.toml"
    env_path = tmp_path / ".env"
    output_path = tmp_path / "dry-run.txt"
    config_path.write_text("[[accounts]]\nusername = \"account\"\n", encoding="utf-8")
    env_path.write_text(
        "X_BEARER_TOKEN=x\nTELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=c\n",
        encoding="utf-8",
    )

    class FakeService:
        def run_once(self, dry_run=False):
            return SimpleNamespace(
                checked_accounts=1,
                baselined=0,
                sent=0,
                would_send=1,
                skipped=0,
                errors=0,
                status_lines=["Would send @account status 123", "formatted Telegram body"],
            )

    monkeypatch.setattr("twitter_tg_notifs.cli.build_service", lambda **_: FakeService())

    exit_code = main(
        [
            "dry-run",
            "--config",
            str(config_path),
            "--env-file",
            str(env_path),
            "--output-file",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert "formatted Telegram body" in output_path.read_text(encoding="utf-8")
    assert "would_send=1" in output_path.read_text(encoding="utf-8")


def test_twitter_notifs_run_once_command_reports_clean_errors(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text("[[accounts]]\nusername = \"account\"\n", encoding="utf-8")

    def fake_build_service(*args, **kwargs):
        raise ValueError("missing X_BEARER_TOKEN")

    monkeypatch.setattr("twitter_tg_notifs.cli.build_service", fake_build_service)

    exit_code = main(["run", "--config", str(config_path), "--once"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "missing X_BEARER_TOKEN" in captured.err
    assert "Traceback" not in captured.err


def test_twitter_notifs_web_command_prints_and_can_skip_browser(tmp_path, monkeypatch):
    config_path = tmp_path / "notifier.toml"
    env_path = tmp_path / ".env"
    config_path.write_text("[[accounts]]\nusername = \"account\"\n", encoding="utf-8")
    env_path.write_text("X_BEARER_TOKEN=x\nTELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=c\n", encoding="utf-8")
    captured = {}

    def fake_run_web_console(options):
        captured["options"] = options

    monkeypatch.setattr("twitter_tg_notifs.cli.run_web_console", fake_run_web_console)

    exit_code = main(
        ["web", "--config", str(config_path), "--env-file", str(env_path), "--port", "0", "--no-open"]
    )

    assert exit_code == 0
    assert captured["options"].config_path == config_path
    assert captured["options"].env_file == env_path
    assert captured["options"].port == 0
    assert captured["options"].open_browser is False
