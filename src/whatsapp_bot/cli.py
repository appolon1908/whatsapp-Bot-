"""Explicit process entrypoints and privileged operator commands.

Runtime commands never read operator credentials or run schema migrations.
Credentials are supplied through the process environment, never CLI arguments.
"""

import argparse
import json
import os
import signal
import sys
from threading import Event
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from . import __version__
from .config import Settings
from .errors import CommerceError


class CommandError(Exception):
    def __init__(self, code):
        self.code = code


def _required_env(name):
    value = os.environ.get(name, "")
    if not value.strip():
        raise CommandError("operator_configuration_missing")
    return value


def _parser():
    parser = argparse.ArgumentParser(prog="commerce", description="Codestra WhatsApp commerce operations")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    api = commands.add_parser("api", help="Run the staff API and authenticated internal ingress")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8783)
    worker = commands.add_parser("worker", help="Process explicitly selected businesses' durable queues")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--interval", type=float, default=1.0)
    provision = commands.add_parser(
        "provision", help="Explicitly create the dedicated commerce database/roles"
    )
    provision.add_argument("--database", required=True)
    commands.add_parser("migrate", help="Explicitly apply migrations with an operator connection")
    onboard = commands.add_parser(
        "onboard-business", help="Create a business and its initial verified subject"
    )
    onboard.add_argument("--name", required=True)
    onboard.add_argument("--currency", choices=("USD", "DOP"), required=True)
    onboard.add_argument("--timezone", required=True)
    onboard.add_argument("--owner-subject", required=True)
    onboard.add_argument("--tax-bps", type=int, required=True)
    channel = commands.add_parser(
        "register-channel", help="Record operator verification of a channel binding"
    )
    channel.add_argument("--tenant-id", type=UUID, required=True)
    channel.add_argument("--provider", choices=("meta", "evolution"), required=True)
    channel.add_argument("--account-id", required=True)
    channel.add_argument("--phone-number-id", required=True)
    channel.add_argument("--verification-reference", required=True)
    channel.add_argument("--policy-campaign-id", required=True)
    return parser


def _run_worker(settings, args):
    from .db import make_engine
    from .policy import PolicyClient
    from .worker import Worker

    if not settings.database_url.get_secret_value() or not settings.worker_tenant_ids:
        raise CommandError("worker_configuration_missing")
    if not 0.1 <= args.interval <= 30:
        raise CommandError("invalid_configuration")
    policy = None
    if settings.outbound_enabled:
        if not settings.policy_base_url or not settings.policy_bearer_token.get_secret_value():
            raise CommandError("policy_configuration_missing")
        policy = PolicyClient(
            settings.policy_base_url,
            settings.policy_bearer_token.get_secret_value(),
            allow_local_http=settings.allow_insecure_local_http,
        )
    engine = make_engine(settings.database_url.get_secret_value())
    stop = Event()
    previous = {}
    try:
        worker = Worker(
            engine,
            tenant_ids=settings.worker_tenant_ids,
            policy_client=policy,
            outbound_enabled=settings.outbound_enabled,
        )
        if not args.once:
            for name in (signal.SIGINT, signal.SIGTERM):
                previous[name] = signal.signal(name, lambda *_: stop.set())
        while not stop.is_set():
            result = worker.tick()
            if args.once or any(result.values()):
                print(json.dumps(result), flush=True)
            if args.once:
                break
            stop.wait(args.interval)
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)
        engine.dispose()
        if policy:
            policy.close()


def _execute(args):
    if args.command in {"api", "worker"}:
        settings = Settings()
        if args.command == "worker":
            _run_worker(settings, args)
            return None
        if not 1 <= args.port <= 65535:
            raise CommandError("invalid_configuration")
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
        return None
    from .provision import bootstrap_business, migrate_database, provision_database, register_channel

    if args.command == "provision":
        return provision_database(
            _required_env("COMMERCE_ADMIN_DATABASE_URL"),
            args.database,
            _required_env("COMMERCE_APP_PASSWORD"),
        )
    owner_url = _required_env("COMMERCE_OWNER_DATABASE_URL")
    if args.command == "migrate":
        migrate_database(owner_url)
        return {"status": "migrated"}
    if args.command == "onboard-business":
        return bootstrap_business(
            owner_url, args.name, args.currency, args.timezone, args.owner_subject, args.tax_bps
        )
    return register_channel(
        owner_url,
        args.tenant_id,
        args.provider,
        args.account_id,
        args.phone_number_id,
        args.verification_reference,
        args.policy_campaign_id,
    )


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        result = _execute(args)
        if result is not None:
            print(json.dumps(result, default=str))
        return 0
    except CommandError as exc:
        code = exc.code
    except ValidationError:
        code = "invalid_configuration"
    except CommerceError as exc:
        code = exc.code
    except ValueError:
        code = "invalid_configuration"
    except RuntimeError:
        code = "operator_action_refused"
    except SQLAlchemyError:
        code = "database_action_failed"
    except KeyboardInterrupt:
        return 130
    # Database exceptions and validation inputs may contain credentials. Only emit
    # stable public codes; never print exception repr/traceback from these commands.
    print(json.dumps({"error": code}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
