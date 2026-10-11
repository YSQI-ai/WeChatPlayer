#!/usr/bin/env python3
"""Command line entry point for the Windows MFLAC helper.

Examples::

    python run_helper.py --check                 # validate config, key source, API
    python run_helper.py --register              # register (or re-register) this device
    python run_helper.py --once                  # process at most one job, then exit
    python run_helper.py                         # run as a polling agent
    python run_helper.py --status                # print device and queue status

Run ``--once`` from Task Scheduler for a pulled deployment, or run the polling
agent under a "at log on" trigger.  A ``LocalSystem`` service is deliberately not
supported: inspecting an authorized QQ Music session needs the interactive user.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helper.agent import HelperAgent  # noqa: E402
from helper.client import AuthenticationError, HelperApiError, HelperClient, TransientApiError  # noqa: E402
from helper.config import ConfigError, HelperConfig  # noqa: E402
from helper.keyprovider import Decoder, KeyUnavailable, build_key_provider, default_decoder_path  # noqa: E402
from helper.redact import Redactor, install_redaction  # noqa: E402


def configure_logging(level: str, redactor: Redactor) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    install_redaction(redactor, root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_helper.py",
        description="Windows MFLAC helper for the WeChatPlayer converter API.",
    )
    parser.add_argument("--config", type=Path, help="path to config.ini")
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    parser.add_argument("--register", action="store_true", help="register or re-register this device")
    parser.add_argument("--status", action="store_true", help="print device and queue status and exit")
    parser.add_argument("--once", action="store_true", help="process at most one job, then exit")
    parser.add_argument("--max-jobs", type=int, help="stop after this many jobs")
    parser.add_argument(
        "--forget-credentials", action="store_true", help="delete stored credentials and exit"
    )
    parser.add_argument("--log-level", help="override the configured log level")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    redactor = Redactor()

    try:
        config = HelperConfig.load(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.log_level:
        config.log_level = args.log_level
    configure_logging(config.log_level, redactor)
    logger = logging.getLogger("helper")

    agent = HelperAgent(config, redactor=redactor)

    try:
        if args.forget_credentials:
            agent.forget_credentials()
            logger.info("Stored credentials removed")
            return 0

        agent.prepare()

        if args.check:
            return _run_check(agent, config)

        if args.register:
            agent.ensure_registered()
            logger.info("Registration complete for device %s", agent.ensure_registered().device_id)
            return 0

        if args.status:
            return _run_status(agent, config)

        processed = agent.run(once=args.once, max_jobs=args.max_jobs)
        logger.info("Helper stopped after %s job(s)", processed)
        return 0
    except KeyUnavailable as exc:
        logger.error("Key source is unusable: %s", exc)
        return 3
    except AuthenticationError as exc:
        logger.error("Authentication failed: %s", exc)
        if "enrollment" in str(exc).lower() or "credentials" in str(exc).lower():
            logger.error("Set HELPER_ENROLLMENT_SECRET and run --register")
        return 4
    except (HelperApiError, TransientApiError) as exc:
        logger.error("The API could not be reached: %s", exc)
        return 5
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 0


def _run_check(agent: HelperAgent, config: HelperConfig) -> int:
    """Validate every local dependency without claiming work."""
    logger = logging.getLogger("helper.check")
    failures = 0

    logger.info("configuration: %s", json.dumps(config.describe(), ensure_ascii=False))

    usable, reason = agent.key_provider.check() if agent.key_provider else (False, "no provider")
    if usable:
        logger.info("key source %s: ready", config.key_source)
    else:
        logger.warning("key source %s: %s", config.key_source, reason)
        if config.key_source == "none":
            logger.warning(
                "Only files with an embedded EKey will decode. Configure key_source to "
                "ekey_file or player_process_db for PC MusicEx files."
            )

    decoder_path = config.decoder_path or default_decoder_path()
    if decoder_path is None:
        logger.error("offline decoder not found; set decoder_path in config.ini")
        failures += 1
    else:
        try:
            Decoder(decoder_path, agent.redactor).self_test()
            logger.info("offline decoder %s: self-test passed", decoder_path.name)
        except Exception as exc:
            logger.error("offline decoder self-test failed: %s", exc)
            failures += 1

    precondition = agent.check_preconditions()
    if precondition.ok:
        logger.info("host preconditions: ready")
    else:
        logger.warning("host preconditions: %s", precondition.reason)

    try:
        credentials = agent.ensure_registered()
        heartbeat = agent.client.device_heartbeat(credentials.device_id, credentials.device_token)
        logger.info("API reachable; device %s is %s", credentials.device_id, heartbeat.get("status"))
    except AuthenticationError as exc:
        logger.error("device authentication failed: %s", exc)
        failures += 1
    except (HelperApiError, TransientApiError) as exc:
        logger.error("API unreachable: %s", exc)
        failures += 1

    return 1 if failures else 0


def _run_status(agent: HelperAgent, config: HelperConfig) -> int:
    logger = logging.getLogger("helper.status")
    credentials = agent.ensure_registered()
    heartbeat = agent.client.device_heartbeat(credentials.device_id, credentials.device_token)
    entry = agent.journal.load()
    logger.info("device_id: %s", credentials.device_id)
    logger.info("token expires: %s", credentials.token_expires_at or "unknown")
    logger.info("queue: %s", json.dumps(heartbeat.get("queue") or {}, ensure_ascii=False))
    logger.info("in-flight journal: %s", entry.to_document() if entry else "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
