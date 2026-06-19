from __future__ import annotations

import html
import os
import secrets
import subprocess
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from twitter_tg_notifs.classifier import build_classifier_pair, classify_with_policy
from twitter_tg_notifs.config import (
    AccountConfig,
    NotifierConfig,
    TopicFilterConfig,
    load_classifier_secrets,
    load_notifier_config,
    load_notifier_secrets,
    save_notifier_config,
)
from twitter_tg_notifs.models import NormalizedPost, UserRef
from twitter_tg_notifs.service import resolve_state_path
from twitter_tg_notifs.state import PollRunState, SQLiteNotifierState


DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 4319


@dataclass(frozen=True)
class WebServerOptions:
    config_path: Path
    env_file: Path | None = None
    state_path: Path | None = None
    host: str = DEFAULT_WEB_HOST
    port: int = DEFAULT_WEB_PORT
    open_browser: bool = True


def run_web_console(options: WebServerOptions) -> None:
    handler = _make_handler(options.config_path, env_file=options.env_file, state_path=options.state_path)
    server = ThreadingHTTPServer((options.host, options.port), handler)
    url = f"http://{options.host}:{server.server_port}"
    print(f"Web console: {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if options.open_browser:
        threading.Timer(0.2, webbrowser.open, args=[url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web console.", flush=True)
    finally:
        server.server_close()


def _make_handler(config_path: Path, *, env_file: Path | None = None, state_path: Path | None = None):
    csrf_token = secrets.token_urlsafe(24)

    class WebConsoleHandler(BaseHTTPRequestHandler):
        server_version = "TwitterTgNotifsWeb/0.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                query = parse_qs(parsed.query)
                try:
                    config = _load_config()
                except (OSError, ValueError) as exc:
                    self._send_html(_render_config_error_page(config_path, exc))
                    return
                self._send_html(
                    _render_page(
                        config,
                        csrf_token=csrf_token,
                        expanded=_first(query.get("expanded")),
                        notice=_first(query.get("notice")),
                        error=_first(query.get("error")),
                        classifier_result=_first(query.get("classifier_result")),
                        classifier_reason=_first(query.get("classifier_reason")),
                        secrets_status=_secrets_status(),
                        classifier_status=_classifier_status(config),
                        latest_poll=_latest_poll(config),
                        daemon_alive=_daemon_heartbeat_alive(config),
                    )
                )
                return
            if parsed.path == "/health":
                self._send_text("ok\n", content_type="text/plain")
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            form = self._read_form()
            try:
                _validate_csrf(form, csrf_token)
                if parsed.path == "/accounts/add":
                    config = _load_config()
                    username = _first(form.get("username")).strip().lstrip("@").lower()
                    if not username:
                        raise ValueError("Username is required.")
                    if username in {account.username for account in config.accounts}:
                        raise ValueError(f"@{username} is already watched.")
                    config.accounts.append(AccountConfig(username=username, include_reposts=True))
                    _save_config(config)
                    self._redirect(notice=f"Added @{username}.")
                    return
                if parsed.path == "/accounts/remove":
                    config = _load_config()
                    username = _first(form.get("username"))
                    config.accounts = [account for account in config.accounts if account.username != username]
                    _save_config(config)
                    self._redirect(notice=f"Removed @{username}.")
                    return
                if parsed.path == "/accounts/save":
                    config = _load_config()
                    expanded = _first(form.get("expanded"))
                    for account in config.accounts:
                        value = _first(form.get(f"include_reposts:{account.username}"))
                        if value:
                            account.include_reposts = value == "true"
                    if expanded:
                        account = _account_by_username(config, expanded)
                        instructions = _first(form.get("filter_instructions")).strip()
                        on_error = _first(form.get("on_filter_error")) or "skip"
                        if on_error not in {"skip", "send", "fallback"}:
                            raise ValueError("Filter error behavior must be skip, send, or fallback.")
                        if instructions:
                            account.topic_filter = TopicFilterConfig(
                                enabled=True,
                                instructions=instructions,
                                topics=account.topic_filter.topics if account.topic_filter else [],
                                confidence_threshold=(
                                    account.topic_filter.confidence_threshold if account.topic_filter else 0.70
                                ),
                                on_filter_error=on_error,
                            )
                        else:
                            account.topic_filter = None
                    _save_config(config)
                    self._redirect(expanded=expanded, notice="Saved config.")
                    return
                if parsed.path == "/classifier/test":
                    config = _load_config()
                    expanded = _first(form.get("expanded"))
                    account = _account_by_username(config, expanded)
                    sample_text = _first(form.get("classifier_sample")).strip()
                    if not sample_text:
                        raise ValueError("Classifier sample text is required.")
                    decision = _run_classifier_test(config, account, sample_text)
                    summary = f"{'send' if decision.send else 'skip'} ({decision.confidence:.2f})"
                    self._redirect(
                        expanded=expanded,
                        notice="Classifier test complete.",
                        classifier_result=summary,
                        classifier_reason=decision.reason,
                    )
                    return
            except ValueError as exc:
                self._redirect(error=str(exc), expanded=_first(form.get("expanded")))
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_form(self) -> dict[str, list[str]]:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length else ""
            return parse_qs(raw_body, keep_blank_values=True)

        def _send_html(self, body: str) -> None:
            self._send_text(body, content_type="text/html; charset=utf-8")

        def _send_text(self, body: str, *, content_type: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _redirect(
            self,
            *,
            notice: str = "",
            error: str = "",
            expanded: str = "",
            classifier_result: str = "",
            classifier_reason: str = "",
        ) -> None:
            params = {
                key: value
                for key, value in {
                    "notice": notice,
                    "error": error,
                    "expanded": expanded,
                    "classifier_result": classifier_result,
                    "classifier_reason": classifier_reason,
                }.items()
                if value
            }
            target = "/" + (f"?{urlencode(params)}" if params else "")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", target)
            self.end_headers()

    def _load_config() -> NotifierConfig:
        return load_notifier_config(config_path)

    def _save_config(config: NotifierConfig) -> None:
        save_notifier_config(config, config_path)

    def _run_classifier_test(config: NotifierConfig, account: AccountConfig, sample_text: str):
        if account.topic_filter is None or not account.topic_filter.enabled:
            raise ValueError(f"@{account.username} does not have filter instructions enabled.")
        secrets_for_classifier = load_classifier_secrets(env_file=env_file)
        primary, fallback = build_classifier_pair(config.classifier, secrets_for_classifier)
        post = NormalizedPost(
            id="web-test",
            kind="post",
            author=UserRef(id="web", username=account.username, name=account.username),
            text=sample_text,
            created_at=datetime.now(timezone.utc),
            watched_username=account.username,
        )
        return classify_with_policy(
            post,
            account.topic_filter,
            primary=primary,
            fallback=fallback,
        )

    def _state(config: NotifierConfig) -> SQLiteNotifierState:
        state = SQLiteNotifierState(resolve_state_path(config, config_path=config_path, state_path=state_path))
        state.initialize()
        return state

    def _latest_poll(config: NotifierConfig) -> PollRunState | None:
        try:
            return _state(config).latest_poll_result()
        except OSError:
            return None

    def _daemon_heartbeat_alive(config: NotifierConfig) -> bool:
        try:
            return _state(config).daemon_heartbeat_alive()
        except OSError:
            return False

    def _secrets_status() -> dict[str, str]:
        try:
            load_notifier_secrets(env_file=env_file)
        except ValueError as exc:
            return {"class": "bad", "label": f"missing secrets: {exc}"}
        except OSError as exc:
            return {"class": "bad", "label": f"env error: {exc}"}
        return {"class": "ok", "label": "configured"}

    def _classifier_status(config: NotifierConfig) -> dict[str, str]:
        try:
            secrets_for_classifier = load_classifier_secrets(env_file=env_file)
            build_classifier_pair(config.classifier, secrets_for_classifier)
        except (OSError, ValueError) as exc:
            return {"class": "bad", "label": str(exc)}
        if config.classifier.provider == "none":
            return {"class": "warn", "label": "off"}
        return {"class": "ok", "label": "configured"}

    return WebConsoleHandler


def _render_config_error_page(config_path: Path, error: BaseException) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Config error</title>
  <style>{_CSS}</style>
</head>
<body>
  <main class="page narrow-page">
    <div class="config-error">
      <div class="eyebrow">Config error</div>
      <h1>Fix config.toml</h1>
      <p>The web console could not load <span class="path-chip">{_e(config_path)}</span>.</p>
      <pre>{_e(error)}</pre>
      <p>Fix the TOML file, then refresh this page. No settings were changed.</p>
    </div>
  </main>
</body>
</html>"""


def _render_page(
    config: NotifierConfig,
    *,
    csrf_token: str = "",
    expanded: str = "",
    notice: str = "",
    error: str = "",
    classifier_result: str = "",
    classifier_reason: str = "",
    secrets_status: dict[str, str] | None = None,
    classifier_status: dict[str, str] | None = None,
    latest_poll: PollRunState | None = None,
    daemon_alive: bool = False,
) -> str:
    if expanded and expanded not in {account.username for account in config.accounts}:
        expanded = ""
    if not expanded and config.accounts:
        filtered = [account.username for account in config.accounts if _has_filter(account)]
        expanded = filtered[0] if filtered else ""
    daemon = _daemon_status(daemon_alive=daemon_alive)
    expanded_account = _account_by_username(config, expanded) if expanded else None
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Twitter to Telegram</title>
  <style>{_CSS}</style>
</head>
<body>
  <main class="page">
    <header class="topbar">
      <div>
        <div class="eyebrow">localhost:4319</div>
        <h1>Twitter to Telegram</h1>
        <p class="lede">Manage watched accounts. Reposts are a row-level choice; filters expand only when you need to edit instructions.</p>
      </div>
      <div class="top-actions">
        <span class="daemon {daemon['class']}"><span></span>{daemon['label']}</span>
        <span class="path-chip">config.toml</span>
        <button form="accounts-form" class="button primary" type="submit">Save config</button>
      </div>
    </header>
    {_render_notice(notice, error)}
    <section class="layout">
      <section class="accounts-panel">
        <div class="panel-heading">
          <h2>Watched accounts</h2>
          <div class="add-wrap">
            <form method="post" action="/accounts/add" class="add-form">
              <input type="hidden" name="csrf_token" value="{_e(csrf_token)}">
              <input name="username" placeholder="@username" autocomplete="off">
              <button class="button dark" type="submit">Add account</button>
            </form>
          </div>
        </div>
        <form id="accounts-form" method="post" action="/accounts/save">
          <input type="hidden" name="csrf_token" value="{_e(csrf_token)}">
          <input type="hidden" name="expanded" value="{_e(expanded)}">
          <div class="table">
            <div class="table-row table-head">
              <div>Account</div>
              <div>Reposts</div>
              <div>Filter</div>
              <div>Status</div>
              <div class="actions">Action</div>
            </div>
            {_render_account_rows(config, expanded, csrf_token)}
          </div>
        </form>
      </section>
      <aside class="rail">
        {_render_service_status(config, daemon, latest_poll, secrets_status or {'class': 'bad', 'label': 'unknown'})}
        {_render_classifier_card(config, expanded_account, csrf_token, classifier_result, classifier_reason, classifier_status or {'class': 'bad', 'label': 'unknown'})}
      </aside>
    </section>
  </main>
</body>
</html>"""


def _render_account_rows(config: NotifierConfig, expanded: str, csrf_token: str) -> str:
    if not config.accounts:
        return """
<div class="empty-state">
  <h3>No accounts yet</h3>
  <p>Add your first @username above. The first daemon run will baseline that account before sending anything to Telegram.</p>
</div>"""
    rows: list[str] = []
    for account in config.accounts:
        rows.append(_render_account_row(account, config, expanded, csrf_token))
        if account.username == expanded:
            rows.append(_render_filter_editor(account))
    return "".join(rows)


def _render_account_row(account: AccountConfig, config: NotifierConfig, expanded: str, csrf_token: str) -> str:
    include = account.effective_include_reposts(config.x.default_include_reposts)
    selected = account.username == expanded
    filter_text = "Yes" if _has_filter(account) else "No"
    status = "Editing" if selected else "Configured"
    action = "Close" if selected else "Open"
    action_href = "/" if selected else f"/?{urlencode({'expanded': account.username})}"
    return f"""
<div class="table-row account-row {'selected' if selected else ''}">
  <div class="username">@{_e(account.username)}</div>
  <div class="radio-group">
    <label><input type="radio" name="include_reposts:{_e(account.username)}" value="true" {'checked' if include else ''}> Yes</label>
    <label><input type="radio" name="include_reposts:{_e(account.username)}" value="false" {'checked' if not include else ''}> No</label>
  </div>
  <div class="filter-flag {'yes' if filter_text == 'Yes' else 'no'}">{filter_text}</div>
  <div class="status {'warn' if selected else 'ok'}">{status}</div>
  <div class="actions">
    <a href="{action_href}">{action}</a>
    <form method="post" action="/accounts/remove">
      <input type="hidden" name="csrf_token" value="{_e(csrf_token)}">
      <input type="hidden" name="username" value="{_e(account.username)}">
      <button type="submit">Remove</button>
    </form>
  </div>
</div>"""


def _render_filter_editor(account: AccountConfig | None) -> str:
    if account is None:
        return ""
    topic_filter = account.topic_filter
    instructions = topic_filter.instructions if topic_filter else ""
    on_error = topic_filter.on_filter_error if topic_filter else "skip"
    return f"""
<div class="filter-editor">
  <div class="filter-note">
    <div class="eyebrow">Filter instructions</div>
    <p>Applies only to @{_e(account.username)} before Telegram delivery.</p>
  </div>
  <div class="filter-fields">
    <textarea name="filter_instructions" aria-label="Filter instructions">{_e(instructions)}</textarea>
    <div class="filter-controls">
      <label>Filter error behavior
        <select name="on_filter_error">
          {_option('skip', on_error)}
          {_option('send', on_error)}
          {_option('fallback', on_error)}
        </select>
      </label>
      <div class="field-actions">
        <a class="button secondary" href="/">Cancel</a>
        <button class="button primary" type="submit">Save filter</button>
      </div>
    </div>
  </div>
</div>"""


def _render_service_status(
    config: NotifierConfig,
    daemon: dict[str, str],
    latest_poll: PollRunState | None,
    secrets_status: dict[str, str],
) -> str:
    poll_label = "never"
    error_label = "0"
    rate_limit_row = ""
    if latest_poll is not None:
        poll_label = latest_poll.ran_at
        error_label = str(latest_poll.errors)
        if latest_poll.rate_limited_until is not None:
            reset_time = datetime.fromtimestamp(latest_poll.rate_limited_until, tz=timezone.utc).isoformat()
            rate_limit_row = f"<div><dt>X backoff</dt><dd>{_e(reset_time)}</dd></div>"
    return f"""
<section class="rail-card light">
  <div class="rail-heading"><div class="eyebrow">Service status</div><span class="mini-status {daemon['class']}"><span></span>{daemon['short']}</span></div>
  <dl>
    <div><dt>Poll interval</dt><dd>{config.polling.interval_seconds}s</dd></div>
    <div><dt>Last poll</dt><dd>{_e(poll_label)}</dd></div>
    <div><dt>Last errors</dt><dd>{_e(error_label)}</dd></div>
    {rate_limit_row}
    <div><dt>Secrets</dt><dd class="{_e(secrets_status['class'])}">{_e(secrets_status['label'])}</dd></div>
  </dl>
</section>"""


def _render_classifier_card(
    config: NotifierConfig,
    account: AccountConfig | None,
    csrf_token: str,
    classifier_result: str,
    classifier_reason: str,
    classifier_status: dict[str, str],
) -> str:
    target = f"@{account.username}" if account else "selected account"
    disabled = "" if account and _has_filter(account) else "disabled"
    result = classifier_result or "not run"
    reason = classifier_reason or "Run a sample against the selected account filter."
    return f"""
<section class="rail-card dark-card">
  <div class="rail-heading"><div><div class="eyebrow amber">Classifier test</div><h3>{_e(config.classifier.provider)}</h3></div><span class="mini-status {classifier_status['class']}"><span></span>{_e(classifier_status['label'])}</span></div>
  <form method="post" action="/classifier/test" class="classifier-form">
    <input type="hidden" name="csrf_token" value="{_e(csrf_token)}">
    <input type="hidden" name="expanded" value="{_e(account.username if account else '')}">
    <textarea name="classifier_sample" aria-label="Classifier sample post">Load growth from AI data centers is changing PJM reserve margins and transmission assumptions.</textarea>
    <button class="button primary full" type="submit" {disabled}>Test selected filter</button>
  </form>
  <div class="result-box">
    <div><span>Target</span><b>{_e(target)}</b></div>
    <div><span>Result</span><b class="green">{_e(result)}</b></div>
    <p>{_e(reason)}</p>
  </div>
</section>"""


def _render_notice(notice: str, error: str) -> str:
    if error:
        return f'<div class="notice error">{_e(error)}</div>'
    if notice:
        return f'<div class="notice success">{_e(notice)}</div>'
    return ""


def _daemon_status(*, daemon_alive: bool = False) -> dict[str, str]:
    running = daemon_alive or _is_daemon_process_running()
    return {
        "class": "ok" if running else "bad",
        "label": "Daemon ready" if running else "Daemon stopped",
        "short": "running" if running else "stopped",
    }


def _is_daemon_process_running() -> bool:
    for line in _process_command_lines():
        if "twitter-tg-notifs" in line and " run" in line and " web" not in line:
            return True
    return False


def _process_command_lines() -> list[str]:
    for command in _process_list_commands():
        try:
            output = subprocess.check_output(command, text=True, timeout=1.5)
        except (OSError, subprocess.SubprocessError):
            continue
        return output.splitlines()
    return []


def _process_list_commands(os_name: str | None = None) -> list[list[str]]:
    if (os_name or os.name) == "nt":
        powershell_command = "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"
        return [
            ["powershell", "-NoProfile", "-Command", powershell_command],
            ["pwsh", "-NoProfile", "-Command", powershell_command],
        ]
    return [["ps", "-axo", "command"]]


def _account_by_username(config: NotifierConfig, username: str) -> AccountConfig:
    for account in config.accounts:
        if account.username == username:
            return account
    raise ValueError(f"Unknown account: @{username}")


def _has_filter(account: AccountConfig) -> bool:
    return bool(account.topic_filter and account.topic_filter.enabled and account.topic_filter.instructions)


def _option(value: str, selected: str) -> str:
    return f'<option value="{value}" {"selected" if value == selected else ""}>{value}</option>'


def _first(values: list[str] | None) -> str:
    return values[0] if values else ""


def _validate_csrf(form: dict[str, list[str]], expected: str) -> None:
    if _first(form.get("csrf_token")) != expected:
        raise ValueError("Invalid form token. Refresh the page and try again.")


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


_CSS = """
:root {
  color-scheme: light;
  --ink: #111315;
  --muted: #555d66;
  --line: #dde2e7;
  --soft: #f4f6f8;
  --amber: #ffb547;
  --green: #2f9e63;
  --red: #b33a3a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #fff;
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.page { padding: 42px 48px; }
.topbar, .layout, .panel-heading, .top-actions, .rail-heading, .filter-controls, .field-actions {
  display: flex;
}
.topbar { align-items: flex-start; justify-content: space-between; gap: 28px; }
.top-actions { align-items: center; gap: 10px; flex-shrink: 0; }
.eyebrow {
  color: #6f7782;
  font-size: 13px;
  font-weight: 850;
  letter-spacing: .12em;
  line-height: 18px;
  text-transform: uppercase;
}
.amber { color: var(--amber); }
h1 { margin: 8px 0 0; font-size: 42px; line-height: 46px; letter-spacing: 0; }
h2 { margin: 0; font-size: 22px; line-height: 28px; }
h3 { margin: 2px 0 0; font-size: 22px; line-height: 28px; }
.lede { margin: 12px 0 0; max-width: 720px; color: var(--muted); font-size: 15px; line-height: 22px; }
.layout { margin-top: 24px; gap: 28px; align-items: stretch; }
.accounts-panel {
  width: min(900px, calc(100vw - 560px));
  min-height: 720px;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.panel-heading {
  height: 68px;
  align-items: center;
  justify-content: space-between;
  padding: 0 18px;
  border-bottom: 1px solid var(--line);
}
.add-form { display: flex; gap: 8px; }
input, textarea, select, button { font: inherit; }
input, textarea, select {
  border: 1px solid #c8d0d8;
  border-radius: 7px;
  background: #fff;
  color: var(--ink);
}
input { height: 36px; padding: 0 10px; width: 150px; }
.button, button {
  border: 0;
  border-radius: 7px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-weight: 900;
  min-height: 36px;
  padding: 0 12px;
  text-decoration: none;
}
.button.primary, .primary { background: var(--amber); color: var(--ink); }
.button.secondary, .secondary { background: #fff; border: 1px solid #d0d5da; color: var(--ink); }
.button.dark, .dark { background: var(--ink); color: #fff; }
.full { width: 100%; }
.button:disabled, button:disabled { cursor: not-allowed; opacity: .55; }
.path-chip {
  display: inline-flex;
  align-items: center;
  height: 40px;
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: #fff;
  color: #5f6872;
  font-size: 13px;
  font-weight: 850;
}
.daemon, .mini-status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border-radius: 7px;
  font-size: 14px;
  font-weight: 900;
}
.daemon { height: 40px; padding: 0 13px; background: var(--soft); border: 1px solid var(--line); }
.daemon span, .mini-status span {
  width: 9px;
  height: 9px;
  border-radius: 99px;
  background: currentColor;
}
.ok { color: var(--green); }
.bad { color: var(--red); }
.warn { color: #80510a; }
.narrow-page { max-width: 820px; }
.config-error {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 24px;
  background: var(--soft);
}
.config-error h1 { margin-top: 8px; }
.config-error p { color: var(--muted); font-size: 15px; line-height: 23px; }
.config-error pre {
  overflow: auto;
  border: 1px solid #c8d0d8;
  border-radius: 7px;
  background: #fff;
  padding: 14px;
  white-space: pre-wrap;
}
.table-row {
  display: grid;
  grid-template-columns: 178px 136px 70px 96px 108px;
  align-items: center;
  column-gap: 12px;
  min-height: 62px;
  padding: 0 18px;
  border-bottom: 1px solid #eef1f4;
}
.table-head {
  min-height: 42px;
  background: var(--soft);
  color: #6f7782;
  font-size: 12px;
  font-weight: 900;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.account-row.selected { background: #fff7e7; }
.username { font-size: 16px; font-weight: 900; }
.muted { color: #6f7782; }
.empty-state {
  padding: 56px 24px;
  border-bottom: 1px solid #eef1f4;
  color: var(--muted);
  text-align: center;
}
.empty-state h3 { margin: 0; color: var(--ink); font-size: 22px; line-height: 28px; }
.empty-state p { max-width: 460px; margin: 10px auto 0; font-size: 14px; line-height: 21px; }
.radio-group { display: flex; align-items: center; gap: 9px; }
.radio-group label { display: inline-flex; align-items: center; gap: 4px; color: #6f7782; font-size: 13px; font-weight: 750; }
.radio-group label:has(input:checked) { color: var(--ink); font-weight: 900; }
.radio-group input { width: 14px; height: 14px; accent-color: var(--ink); }
.filter-flag { font-size: 14px; font-weight: 900; }
.filter-flag.yes { color: #ff8a00; }
.filter-flag.no { color: #6f7782; }
.status { font-size: 14px; font-weight: 900; }
.status.ok { color: var(--green); }
.status.warn { color: #80510a; }
.actions { display: flex; justify-content: flex-end; align-items: center; gap: 14px; font-size: 13px; font-weight: 900; }
.actions a { color: var(--ink); text-decoration: none; }
.actions form { margin: 0; }
.actions button {
  background: transparent;
  color: var(--red);
  min-height: auto;
  padding: 0;
}
.filter-editor {
  display: grid;
  grid-template-columns: 172px 1fr;
  gap: 16px;
  padding: 18px;
  border-bottom: 1px solid #eef1f4;
}
.filter-note p { margin: 8px 0 0; color: var(--muted); font-size: 13px; line-height: 19px; }
.filter-fields { display: flex; flex-direction: column; gap: 10px; }
.filter-fields textarea {
  min-height: 154px;
  padding: 13px;
  background: var(--soft);
  color: var(--ink);
  font-size: 14px;
  line-height: 21px;
  resize: vertical;
}
.filter-controls { align-items: center; justify-content: space-between; gap: 14px; }
.filter-controls label { display: flex; align-items: center; gap: 8px; color: #6f7782; font-size: 13px; }
.field-actions { gap: 8px; }
.rail { flex: 1; min-width: 330px; display: flex; flex-direction: column; gap: 14px; }
.rail-card { border-radius: 8px; padding: 18px; }
.rail-card.light { background: var(--soft); border: 1px solid var(--line); }
.rail-card.dark-card { background: var(--ink); color: #fff; }
.rail-heading { align-items: flex-start; justify-content: space-between; gap: 12px; }
dl { margin: 14px 0 0; display: flex; flex-direction: column; gap: 12px; }
dl div { display: flex; justify-content: space-between; gap: 18px; }
dt { color: #31363b; }
dd { margin: 0; font-weight: 800; }
.dark-card textarea {
  width: 100%;
  min-height: 112px;
  margin-top: 13px;
  padding: 12px;
  background: #202326;
  border: 1px solid #3d444b;
  color: #fff;
  font-size: 14px;
  line-height: 20px;
  resize: vertical;
}
.result-box {
  margin-top: 12px;
  padding: 12px;
  border-radius: 7px;
  background: #202326;
  border: 1px solid #3d444b;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.result-box div { display: flex; justify-content: space-between; }
.green { color: var(--green); }
.amber-text { color: var(--amber); }
.rail-card p { color: #3d4247; font-size: 14px; line-height: 20px; }
.dark-card .result-box p { margin: 0; color: #c8d0d8; font-size: 13px; line-height: 19px; }
.notice {
  margin-top: 18px;
  border-radius: 8px;
  padding: 12px 14px;
  font-weight: 800;
}
.notice.success { background: #edf8f1; color: #1c7144; }
.notice.error { background: #fff0f0; color: var(--red); }
@media (max-width: 1080px) {
  .page { padding: 26px 18px; }
  .topbar, .layout { flex-direction: column; }
  .top-actions { flex-wrap: wrap; }
  .accounts-panel { width: 100%; overflow-x: auto; }
  .table { min-width: 900px; }
  .rail { width: 100%; min-width: 0; }
}
"""
