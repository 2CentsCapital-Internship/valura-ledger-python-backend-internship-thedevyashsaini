"""Configuration: the local ``.env``, CLI overrides, and validation.

The API key is read from the environment and never printed, logged, or written
to the database. Nothing in this module renders it, including in exceptions.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

MODES = ("practice", "submission", "final")

MODE_MAX_SECONDS = {
    "practice": 40 * 60,
    "submission": 90 * 60,
    "final": 110 * 60,
}

DEFAULT_BASE_URL = "https://hiring-arena.twocc.in"
DEFAULT_DB_PATH = "data/ledger.sqlite3"
DEFAULT_BATCH_SIZE = 100
DEFAULT_FLUSH_MS = 400
DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    db_path: Path
    mode: str
    batch_size: int
    flush_ms: int
    max_seconds: float
    log_level: str


@dataclass(frozen=True)
class Options:
    new_run: bool
    status: bool


class ConfigError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Valura Ledger Arena consumer",
    )
    parser.add_argument("--mode", choices=list(MODES))
    parser.add_argument("--url")
    parser.add_argument("--db")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--flush-ms", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument(
        "--new-run",
        action="store_true",
        help="deliberately start a new scarce attempt",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print live rules and standings without consuming an attempt",
    )
    parser.add_argument("--log-level")
    # Supported for compatibility with the starter kit. Prefer ARENA_API_KEY:
    # a key on the command line lands in shell history and process listings.
    parser.add_argument("--key", help=argparse.SUPPRESS)
    return parser


def _pick(cli_value, env_name: str, default):
    if cli_value is not None:
        return cli_value

    env_value = os.environ.get(env_name)
    if env_value is not None and env_value != "":
        return env_value

    return default


def load_settings(argv: list[str] | None = None) -> tuple[Settings, Options]:
    load_dotenv()

    parser = build_parser()
    args = parser.parse_args(argv)

    mode = _pick(args.mode, "ARENA_MODE", "practice")
    if mode not in MODES:
        raise ConfigError(f"mode must be one of {', '.join(MODES)}")

    base_url = str(_pick(args.url, "ARENA_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
    if not base_url:
        raise ConfigError("base url is empty")
    if not base_url.startswith("https://"):
        raise ConfigError("base url must start with https://")

    api_key = str(_pick(args.key, "ARENA_API_KEY", "")).strip()
    if not api_key:
        raise ConfigError(
            "no API key: set ARENA_API_KEY in your local .env file"
        )
    if not api_key.startswith("ak_"):
        raise ConfigError("the API key does not look like an arena key")

    db_path = Path(str(_pick(args.db, "ARENA_DB_PATH", DEFAULT_DB_PATH)))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    batch_size = int(_pick(args.batch_size, "ARENA_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    if not 1 <= batch_size <= 500:
        raise ConfigError("batch size must be between 1 and 500")

    flush_ms = int(_pick(args.flush_ms, "ARENA_FLUSH_MS", DEFAULT_FLUSH_MS))
    if flush_ms <= 0:
        raise ConfigError("flush interval must be positive")

    max_seconds = float(
        _pick(args.max_seconds, "ARENA_MAX_SECONDS", MODE_MAX_SECONDS[mode])
    )
    if max_seconds <= 0:
        raise ConfigError("max seconds must be positive")

    log_level = str(_pick(args.log_level, "ARENA_LOG_LEVEL", DEFAULT_LOG_LEVEL)).upper()

    settings = Settings(
        base_url=base_url,
        api_key=api_key,
        db_path=db_path,
        mode=mode,
        batch_size=batch_size,
        flush_ms=flush_ms,
        max_seconds=max_seconds,
        log_level=log_level,
    )
    return settings, Options(new_run=args.new_run, status=args.status)
