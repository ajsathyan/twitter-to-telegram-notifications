from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from twitter_tg_notifs.classifier import build_classifier_pair, classify_with_policy
from twitter_tg_notifs.config import NotifierConfig, load_notifier_config, load_notifier_secrets
from twitter_tg_notifs.models import NormalizedPost
from twitter_tg_notifs.state import SQLiteNotifierState
from twitter_tg_notifs.telegram import TelegramClient, TelegramFormatter
from twitter_tg_notifs.x_client import RateLimitError, XApiClient


@dataclass
class PollResult:
    checked_accounts: int = 0
    baselined: int = 0
    sent: int = 0
    would_send: int = 0
    skipped: int = 0
    errors: int = 0
    rate_limited_until: int | None = None
    status_lines: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"checked_accounts={self.checked_accounts} baselined={self.baselined} "
            f"sent={self.sent} would_send={self.would_send} skipped={self.skipped} errors={self.errors}"
        )


class TwitterTelegramService:
    def __init__(
        self,
        *,
        config: NotifierConfig,
        state: SQLiteNotifierState,
        x_client,
        telegram,
        classifier=None,
        fallback_classifier=None,
    ):
        self.config = config
        self.state = state
        self.x_client = x_client
        self.telegram = telegram
        self.classifier = classifier
        self.fallback_classifier = fallback_classifier

    def run_once(self, *, dry_run: bool = False) -> PollResult:
        self.state.initialize()
        result = PollResult()
        lock_owner = self.state.acquire_runtime_lock("poll", ttl_seconds=self._lock_ttl_seconds())
        if lock_owner is None:
            result.errors += 1
            result.status_lines.append("Another notifier instance is already polling. Skipping this cycle.")
            self._record_poll_result(result, dry_run=dry_run)
            return result
        try:
            self._deliver_pending(result, dry_run=dry_run)
            if not self.config.accounts:
                result.status_lines.append("No accounts configured. Add accounts in the web console or config.toml.")
                return result
            for account in self.config.accounts:
                result.checked_accounts += 1
                include_reposts = account.effective_include_reposts(self.config.x.default_include_reposts)
                self.state.upsert_account(account.username, include_reposts=include_reposts)
                state_account = self.state.get_account(account.username)
                try:
                    if not state_account.user_id:
                        user = self.x_client.resolve_user(account.username)
                        self.state.set_user(account.username, user_id=user.id, display_name=user.name)
                        state_account = self.state.get_account(account.username)

                    posts = self.x_client.get_normalized_posts(
                        state_account.user_id,
                        watched_username=account.username,
                        since_id=state_account.last_seen_tweet_id,
                        include_reposts=include_reposts,
                        exclude_replies=self.config.x.exclude_replies,
                        max_results=self.config.x.max_results,
                    )
                except RateLimitError as exc:
                    result.errors += 1
                    result.rate_limited_until = exc.reset_epoch
                    reset = f" until {exc.reset_epoch}" if exc.reset_epoch else ""
                    result.status_lines.append(f"X API rate limited while checking @{account.username}{reset}")
                    break
                except Exception as exc:
                    result.errors += 1
                    result.status_lines.append(f"Error checking @{account.username}: {exc}")
                    continue

                if not state_account.last_seen_tweet_id:
                    latest = _max_post_id(posts)
                    if latest:
                        self.state.set_last_seen(account.username, latest)
                    result.baselined += 1
                    result.status_lines.append(f"Baselined @{account.username} at {latest or 'no posts'}")
                    continue

                latest_seen = state_account.last_seen_tweet_id
                for post in sorted(posts, key=lambda item: int(item.id)):
                    candidate_seen = _max_id(latest_seen, post.id)
                    if self.state.was_sent(post.id):
                        result.skipped += 1
                        latest_seen = candidate_seen
                        continue
                    decision = classify_with_policy(
                        post,
                        account.topic_filter,
                        primary=self.classifier,
                        fallback=self.fallback_classifier,
                    )
                    if account.topic_filter and account.topic_filter.enabled:
                        self.state.record_classifier_decision(
                            post.id,
                            username=account.username,
                            provider=decision.provider,
                            send=decision.send,
                            confidence=decision.confidence,
                            matched_topics=decision.matched_topics,
                            reason=decision.reason,
                        )
                    if not decision.send:
                        result.skipped += 1
                        result.status_lines.append(f"Skipped @{account.username} status {post.id}: {decision.reason}")
                        latest_seen = candidate_seen
                        continue
                    if dry_run:
                        result.would_send += 1
                        result.status_lines.append(f"Would send @{account.username} status {post.id}")
                        preview = self._dry_run_preview(post)
                        if preview:
                            result.status_lines.append(preview)
                        latest_seen = candidate_seen
                        continue
                    self.state.enqueue_pending_delivery(post, username=account.username)
                    if self._deliver_post(post, username=account.username, result=result):
                        latest_seen = candidate_seen
                    else:
                        latest_seen = candidate_seen
                if latest_seen:
                    self.state.set_last_seen(account.username, latest_seen)
            return result
        finally:
            self._record_poll_result(result, dry_run=dry_run)
            self.state.release_runtime_lock("poll", lock_owner)

    def run_forever(self, *, dry_run: bool = False) -> None:
        while True:
            result = self.run_once(dry_run=dry_run)
            print(result.summary(), flush=True)
            for line in result.status_lines:
                print(line, flush=True)
            sleep_seconds = self._sleep_seconds(result)
            self.state.record_daemon_heartbeat(ttl_seconds=sleep_seconds + 30)
            time.sleep(sleep_seconds)

    def _deliver_pending(self, result: PollResult, *, dry_run: bool) -> None:
        if dry_run:
            return
        for pending in self.state.pending_deliveries():
            if self.state.was_sent(pending.tweet_id):
                self.state.delete_pending_delivery(pending.tweet_id)
                result.skipped += 1
                continue
            self._deliver_post(pending.post, username=pending.username, result=result, pending=True)

    def _deliver_post(
        self,
        post: NormalizedPost,
        *,
        username: str,
        result: PollResult,
        pending: bool = False,
    ) -> bool:
        try:
            delivery = self.telegram.send_post(post)
        except Exception as exc:
            result.errors += 1
            self.state.record_pending_error(post.id, str(exc))
            label = "pending " if pending else ""
            result.status_lines.append(f"Error sending {label}@{username} status {post.id}: {exc}")
            return False
        result.sent += 1
        method = getattr(delivery, "method", "sent")
        fallback = " with fallback" if getattr(delivery, "fallback_used", False) else ""
        self.state.mark_sent(post.id, username=username, delivery_status=f"{method}{fallback}")
        self.state.delete_pending_delivery(post.id)
        label = "pending " if pending else ""
        result.status_lines.append(f"Sent {label}@{username} status {post.id} via {method}")
        return True

    def _dry_run_preview(self, post: NormalizedPost) -> str:
        formatter = getattr(self.telegram, "formatter", None)
        if formatter is not None and hasattr(formatter, "format_post"):
            return formatter.format_post(post)
        return post.url

    def _sleep_seconds(self, result: PollResult) -> float:
        interval = float(self.config.polling.interval_seconds)
        if result.rate_limited_until is None:
            return interval
        reset_sleep = max(0.0, float(result.rate_limited_until) - time.time())
        return max(interval, reset_sleep)

    def _lock_ttl_seconds(self) -> float:
        return max(float(self.config.polling.interval_seconds) * 5, 300.0)

    def _record_poll_result(self, result: PollResult, *, dry_run: bool) -> None:
        self.state.record_poll_result(
            checked_accounts=result.checked_accounts,
            baselined=result.baselined,
            sent=result.sent,
            would_send=result.would_send,
            skipped=result.skipped,
            errors=result.errors,
            rate_limited_until=result.rate_limited_until,
            status_lines=result.status_lines,
            dry_run=dry_run,
        )


def build_service(
    *,
    config_path: Path,
    env_file: Path | None = None,
    state_path: Path | None = None,
    dry_run: bool = False,
) -> TwitterTelegramService:
    config = load_notifier_config(config_path)
    db_path = resolve_state_path(config, config_path=config_path, state_path=state_path)
    state = SQLiteNotifierState(db_path)
    if not config.accounts:
        return TwitterTelegramService(
            config=config,
            state=state,
            x_client=_UnavailableClient(),
            telegram=_UnavailableClient(),
            classifier=None,
            fallback_classifier=None,
        )
    secrets = load_notifier_secrets(env_file=env_file)
    x_client = XApiClient(
        secrets.x_bearer_token.get_secret_value(),
        timeout_seconds=config.x.request_timeout_seconds,
    )
    formatter = TelegramFormatter(timezone_name=config.polling.timezone)
    telegram = TelegramClient(
        bot_token=secrets.telegram_bot_token.get_secret_value(),
        chat_id=secrets.telegram_chat_id.get_secret_value(),
        formatter=formatter,
        parse_mode=config.telegram.parse_mode,
        disable_web_page_preview=config.telegram.disable_web_page_preview,
        request_timeout_seconds=config.telegram.request_timeout_seconds,
    )
    classifier, fallback = build_classifier_pair(config.classifier, secrets)
    return TwitterTelegramService(
        config=config,
        state=state,
        x_client=x_client,
        telegram=telegram,
        classifier=classifier,
        fallback_classifier=fallback,
    )


def run_service_forever(
    *,
    config_path: Path,
    env_file: Path | None = None,
    state_path: Path | None = None,
    dry_run: bool = False,
) -> None:
    while True:
        try:
            service = build_service(
                config_path=config_path,
                env_file=env_file,
                state_path=state_path,
                dry_run=dry_run,
            )
            result = service.run_once(dry_run=dry_run)
            sleep_seconds = service._sleep_seconds(result)
            service.state.record_daemon_heartbeat(ttl_seconds=sleep_seconds + 30)
        except Exception as exc:
            result = PollResult(errors=1, status_lines=[f"Error starting poll cycle: {exc}"])
            sleep_seconds = 60.0
        print(result.summary(), flush=True)
        for line in result.status_lines:
            print(line, flush=True)
        time.sleep(sleep_seconds)


def resolve_state_path(config: NotifierConfig, *, config_path: Path, state_path: Path | None = None) -> Path:
    if state_path is not None:
        return state_path
    configured = config.state.path
    if configured.is_absolute():
        return configured
    return config_path.parent / configured


class _UnavailableClient:
    def __getattr__(self, name):
        raise RuntimeError(f"{name} is unavailable because no accounts are configured")


def _max_post_id(posts: list[NormalizedPost]) -> str | None:
    latest: str | None = None
    for post in posts:
        latest = _max_id(latest, post.id)
    return latest


def _max_id(first: str | None, second: str) -> str:
    if first is None:
        return second
    return str(max(int(first), int(second)))
