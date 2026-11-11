"""Command line: run the server or perform management tasks.

    python3 -m neuxmind_cloud [serve] [--host 127.0.0.1] [--port 8800]
    python3 -m neuxmind_cloud init-db
    python3 -m neuxmind_cloud create-operator <email> [--name N]   # recommended: new verified operator + set-password token
    python3 -m neuxmind_cloud verify-email <email> [--force]       # out-of-band verification; resets the password
    python3 -m neuxmind_cloud grant-operator <email>               # refuses unverified accounts
    python3 -m neuxmind_cloud revoke-operator <email>
    python3 -m neuxmind_cloud sweep                     # run the reservation/session clean-up once

Management commands need direct database access (``DATABASE_URL``); they are
the only way to create a platform operator besides the e-mail allow-list.
"""
from __future__ import annotations

import argparse
import json
import sys

COMMANDS = ("serve", "init-db", "create-operator", "verify-email", "grant-operator", "revoke-operator", "sweep")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuxmind_cloud")
    parser.add_argument("command", nargs="?", default="serve", choices=COMMANDS)
    parser.add_argument("email", nargs="?")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--name", help="create-operator: display name")
    parser.add_argument("--force", action="store_true",
                        help="verify-email: revoke the account's existing sessions and API keys")
    args = parser.parse_args(argv)

    from .config import Settings
    from .db import Database

    settings = Settings.from_env()
    if args.command == "init-db":
        Database(settings.database_url).create_all()
        print("database ready")
        return 0
    if args.command in ("create-operator", "verify-email", "grant-operator", "revoke-operator"):
        if not args.email:
            parser.error(f"{args.command} 需要邮箱")
        from sqlalchemy import select

        from . import auth, recovery
        from .errors import ApiError
        from .models import AuditLog

        db = Database(settings.database_url)
        db.create_all()
        reset_url = f"{settings.public_base_url}/api/v1/auth/password-reset"
        try:
            with db.transaction() as session:
                if args.command == "create-operator":
                    user, token = recovery.create_operator(session, args.email, settings, args.name)
                    print(f"platform operator created (e-mail verified): {user.email}")
                    print(f"one-time set-password token (valid {settings.email_verify_ttl_seconds // 3600} h, "
                          f"hand it to the owner over a trusted channel):\n  {token}")
                    print(f"  POST {reset_url}  {{\"token\": \"<token>\", \"password\": \"<new password>\"}}")
                elif args.command == "verify-email":
                    user, token, revoked = recovery.admin_verify(session, args.email, settings, force=args.force)
                    print(f"e-mail verified: {user.email} (password reset; revoked {revoked['sessions_revoked']} "
                          f"sessions, {revoked['api_keys_revoked']} API keys)")
                    print(f"one-time set-password token for the verified owner:\n  {token}")
                    print(f"  POST {reset_url}  {{\"token\": \"<token>\", \"password\": \"<new password>\"}}")
                elif args.command == "grant-operator":
                    user = auth.grant_operator(session, args.email)
                    reg = session.execute(select(AuditLog).where(AuditLog.action == "auth.register",
                                                                 AuditLog.target == user.email)
                                          .order_by(AuditLog.id)).scalars().first()
                    origin = f"registered publicly at {reg.created_at:.0f} from ip {reg.ip or '?'}" if reg \
                        else "no public registration record (created by command or claim)"
                    print(f"platform operator granted: {user.email} ({origin})")
                else:
                    user = auth.revoke_operator(session, args.email)
                    print(f"platform operator revoked: {user.email}")
                    if user.email in settings.operator_emails:
                        print("warning: address is still in NEUXMIND_OPERATOR_EMAILS", file=sys.stderr)
        except ApiError as exc:
            print(f"error: {exc.message}", file=sys.stderr)
            return 1
        return 0
    if args.command == "sweep":
        from .maintenance import sweep

        db = Database(settings.database_url)
        db.create_all()
        print(json.dumps(sweep(db, settings)))
        return 0

    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
