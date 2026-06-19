from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from twitter_tg_notifs import __version__
from twitter_tg_notifs.config import load_notifier_config, load_notifier_secrets
from twitter_tg_notifs.service import build_service, resolve_state_path, run_service_forever
from twitter_tg_notifs.web import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, WebServerOptions, run_web_console


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="twitter-tg-notifs")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    validate = subparsers.add_parser("validate-config", help="Validate notifier config and required secrets")
    _add_common_paths(validate)

    dry_run = subparsers.add_parser("dry-run", help="Poll once and print what would be sent")
    _add_common_paths(dry_run)
    dry_run.add_argument("--state-db", type=Path)
    dry_run.add_argument("--output-file", type=Path, help="Save dry-run output to a UTF-8 text file")
    dry_run.add_argument(
        "--write-state",
        action="store_true",
        help="Allow dry-run to update the configured state DB. By default a temporary state copy is used.",
    )

    run = subparsers.add_parser("run", help="Run the notifier daemon")
    _add_common_paths(run)
    run.add_argument("--state-db", type=Path)
    run.add_argument("--once", action="store_true")
    run.add_argument("--dry-run", action="store_true")

    web = subparsers.add_parser("web", help="Run the localhost account-management interface")
    web.add_argument("--config", type=Path, required=True)
    web.add_argument("--env-file", type=Path)
    web.add_argument("--state-db", type=Path)
    web.add_argument("--host", default=DEFAULT_WEB_HOST)
    web.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)
    web.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-config":
            config = load_notifier_config(args.config)
            secrets = load_notifier_secrets(env_file=args.env_file)
            account_word = "account" if len(config.accounts) == 1 else "accounts"
            print(
                f"Notifier config valid: {len(config.accounts)} {account_word}, "
                f"polling={config.polling.interval_seconds}s, "
                f"x_bearer_token={secrets.model_dump()['x_bearer_token']}, "
                f"telegram_bot_token={secrets.model_dump()['telegram_bot_token']}, "
                f"telegram_chat_id={secrets.model_dump()['telegram_chat_id']}"
            )
            return 0

        if args.command == "dry-run":
            with _dry_run_state_path(args.config, args.state_db, write_state=args.write_state) as dry_state_path:
                service = build_service(
                    config_path=args.config,
                    env_file=args.env_file,
                    state_path=dry_state_path,
                    dry_run=True,
                )
                result = service.run_once(dry_run=True)
                lines = [*result.status_lines, _result_summary(result)]
                if not args.write_state:
                    lines.append("Dry-run used a temporary state copy. Pass --write-state to update the real state DB.")
                for line in lines:
                    print(line)
                if args.output_file:
                    _write_output_file(args.output_file, lines)
            return 0

        if args.command == "run":
            with _dry_run_state_path(args.config, args.state_db, write_state=not args.dry_run) as run_state_path:
                if args.dry_run:
                    print("Dry-run used a temporary state copy. Production SQLite state will not be changed.")
                service = build_service(
                    config_path=args.config,
                    env_file=args.env_file,
                    state_path=run_state_path,
                    dry_run=args.dry_run,
                )
                if args.once:
                    result = service.run_once(dry_run=args.dry_run)
                    for line in result.status_lines:
                        print(line)
                    print(_result_summary(result))
                    return 0 if result.errors == 0 else 1
                run_service_forever(
                    config_path=args.config,
                    env_file=args.env_file,
                    state_path=run_state_path,
                    dry_run=args.dry_run,
                )
            return 0

        if args.command == "web":
            load_notifier_config(args.config)
            run_web_console(
                WebServerOptions(
                    config_path=args.config,
                    env_file=args.env_file,
                    state_path=args.state_db,
                    host=args.host,
                    port=args.port,
                    open_browser=not args.no_open,
                )
            )
            return 0
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)


def _result_summary(result) -> str:
    if hasattr(result, "summary"):
        return result.summary()
    return (
        f"checked_accounts={result.checked_accounts} baselined={result.baselined} "
        f"sent={result.sent} would_send={result.would_send} skipped={result.skipped} errors={result.errors}"
    )


def _write_output_file(path: Path, lines: list[str]) -> None:
    if path.parent and str(path.parent) != ".":
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _dry_run_state_path:
    def __init__(self, config_path: Path, state_path: Path | None, *, write_state: bool):
        self.config_path = config_path
        self.state_path = state_path
        self.write_state = write_state
        self.temp_dir: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path | None:
        if self.write_state:
            return self.state_path
        config = load_notifier_config(self.config_path)
        real_state_path = resolve_state_path(config, config_path=self.config_path, state_path=self.state_path)
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_state_path = Path(self.temp_dir.name) / "dry-run.sqlite3"
        if real_state_path.exists():
            shutil.copy2(real_state_path, temp_state_path)
        return temp_state_path

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.temp_dir is not None:
            self.temp_dir.cleanup()
