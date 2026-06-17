from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from twitter_tg_notifs.config import load_notifier_config
from twitter_tg_notifs.web import _make_handler, _render_page


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


def _csrf_token(base_url: str) -> str:
    with urlopen(base_url, timeout=5) as response:
        html = response.read().decode("utf-8")
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]
