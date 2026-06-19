from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from twitter_tg_notifs.config import load_notifier_config
from twitter_tg_notifs.web import _make_handler, _process_list_commands, _render_page


def test_web_page_renders_radio_reposts_and_filter_yes_no(tmp_path, monkeypatch):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text(
        """
[[accounts]]
username = "grid_status"

[[accounts]]
username = "energythreader"
include_reposts = false

[accounts.topic_filter]
enabled = true
instructions = "Only send AI power demand posts."

[[accounts]]
username = "coaldesk"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("twitter_tg_notifs.web._is_daemon_process_running", lambda: True)

    html = _render_page(load_notifier_config(config_path), expanded="energythreader")

    assert 'type="radio"' in html
    assert "include_reposts:grid_status" in html
    assert "include_reposts:energythreader" in html
    assert "> Include<" not in html
    assert "> Exclude<" not in html
    assert "Only send AI power demand posts." in html
    assert html.index("Only send AI power demand posts.") < html.index("@coaldesk")
    assert "Daemon ready" in html
    assert ">Yes<" in html
    assert ">No<" in html


def test_web_page_renders_empty_account_state(tmp_path, monkeypatch):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text("[polling]\ninterval_seconds = 60\n", encoding="utf-8")
    monkeypatch.setattr("twitter_tg_notifs.web._is_daemon_process_running", lambda: False)

    html = _render_page(load_notifier_config(config_path))

    assert "No accounts yet" in html
    assert "Add your first @username above" in html
    assert "Test selected filter" in html
    assert "disabled" in html


def test_web_page_renders_config_error_page(tmp_path):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text("[[accounts]\nusername = \"broken\"\n", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert "Fix config.toml" in html
    assert "No settings were changed" in html


def test_web_save_updates_reposts_and_filter_instructions(tmp_path):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text(
        """
[[accounts]]
username = "energythreader"
include_reposts = true
""".strip(),
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        csrf_token = _csrf_token(base_url)
        url = f"{base_url}/accounts/save"
        body = urlencode(
            {
                "csrf_token": csrf_token,
                "expanded": "energythreader",
                "include_reposts:energythreader": "false",
                "filter_instructions": "Send only utility capex posts.",
                "on_filter_error": "skip",
            }
        ).encode("utf-8")
        request = Request(url, data=body, method="POST")
        with urlopen(request, timeout=5) as response:
            assert response.status in (200, 303)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    config = load_notifier_config(config_path)
    account = config.accounts[0]
    assert account.include_reposts is False
    assert account.topic_filter is not None
    assert account.topic_filter.enabled is True
    assert account.topic_filter.instructions == "Send only utility capex posts."


def test_web_can_add_first_account_and_remove_last_account(tmp_path):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text("[polling]\ninterval_seconds = 60\n", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        csrf_token = _csrf_token(base_url)
        add_body = urlencode({"csrf_token": csrf_token, "username": "energythreader"}).encode("utf-8")
        with urlopen(Request(f"{base_url}/accounts/add", data=add_body, method="POST"), timeout=5) as response:
            assert response.status in (200, 303)

        config = load_notifier_config(config_path)
        assert [account.username for account in config.accounts] == ["energythreader"]

        csrf_token = _csrf_token(base_url)
        remove_body = urlencode({"csrf_token": csrf_token, "username": "energythreader"}).encode("utf-8")
        with urlopen(Request(f"{base_url}/accounts/remove", data=remove_body, method="POST"), timeout=5) as response:
            assert response.status in (200, 303)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert load_notifier_config(config_path).accounts == []


def test_web_classifier_test_with_none_provider_does_not_require_notifier_secrets(tmp_path, monkeypatch):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text(
        """
[classifier]
provider = "none"

[[accounts]]
username = "energythreader"

[accounts.topic_filter]
enabled = true
instructions = "Send AI power posts."
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        csrf_token = _csrf_token(base_url)
        body = urlencode(
            {
                "csrf_token": csrf_token,
                "expanded": "energythreader",
                "classifier_sample": "AI data center load growth.",
            }
        ).encode("utf-8")
        with urlopen(Request(f"{base_url}/classifier/test", data=body, method="POST"), timeout=5) as response:
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert "Classifier test complete" in html
    assert "send (1.00)" in html


def test_web_rejects_posts_without_csrf_token(tmp_path):
    config_path = tmp_path / "notifier.toml"
    config_path.write_text(
        """
[[accounts]]
username = "energythreader"
include_reposts = true
""".strip(),
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/accounts/save"
        body = urlencode(
            {
                "expanded": "energythreader",
                "include_reposts:energythreader": "false",
                "filter_instructions": "Send only utility capex posts.",
                "on_filter_error": "skip",
            }
        ).encode("utf-8")
        request = Request(url, data=body, method="POST")
        with urlopen(request, timeout=5) as response:
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    config = load_notifier_config(config_path)
    assert "Invalid form token" in html
    assert config.accounts[0].include_reposts is True
    assert config.accounts[0].topic_filter is None


def test_process_list_commands_support_windows_and_posix():
    windows_commands = _process_list_commands("nt")

    assert windows_commands[0][0] == "powershell"
    assert "Get-CimInstance Win32_Process" in windows_commands[0][-1]
    assert windows_commands[1][0] == "pwsh"
    assert _process_list_commands("posix") == [["ps", "-axo", "command"]]


def _csrf_token(base_url: str) -> str:
    with urlopen(base_url, timeout=5) as response:
        html = response.read().decode("utf-8")
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]
