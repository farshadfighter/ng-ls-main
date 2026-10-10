"""
Account maintenance on the server itself, for when the console cannot be used (no admin
account, every admin locked out or forgotten):

    docker compose exec backend python -m app.cli list-users
    docker compose exec backend python -m app.cli create-user NAME --role admin
    docker compose exec backend python -m app.cli reset-password NAME
    docker compose exec backend python -m app.cli unlock NAME

Passwords are read from the terminal (never from the command line, where they would end up in
the shell history and the process list) and must meet the policy (app/core/passwords.py); the
account must change it at its next login. Every change is written to the audit trail as actor
"system" with "via": "cli".
"""
import argparse
import getpass
import sys

from . import accounts, audit, models
from .database import SessionLocal


def _ask_password() -> str:
    first = getpass.getpass("Temporary password: ")
    if first != getpass.getpass("Repeat it: "):
        sys.exit("The two passwords differ.")
    return first


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-users")
    create = sub.add_parser("create-user")
    create.add_argument("username")
    create.add_argument("--role", default="admin", choices=[r.value for r in models.AdminRole])
    create.add_argument("--full-name", default=None)
    for name in ("reset-password", "unlock"):
        sub.add_parser(name).add_argument("username")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        if args.command == "list-users":
            for u in db.query(models.AdminUser).order_by(models.AdminUser.username):
                state = "active" if u.is_active else "disabled"
                print(f"{u.username:24} {u.role:9} {state:8} locked_until={u.locked_until} "
                      f"last_login={u.last_login_at}")
            return 0

        if args.command == "create-user":
            user = accounts.create_user(db, username=args.username, role=args.role,
                                        password=_ask_password(), full_name=args.full_name, email=None,
                                        created_by="cli")
            audit.record("user.create", audit.SUCCESS, actor="system", target=user.username,
                         detail={"role": user.role, "via": "cli"})
            print(f"Made {user.username} ({user.role}); the password must be changed at first login.")
            return 0

        user = accounts.get_by_username(db, args.username)
        if user is None:
            sys.exit(f"No account named {args.username}.")
        if args.command == "reset-password":
            accounts.reset_password(db, user, _ask_password())
            audit.record("user.password_reset", audit.SUCCESS, actor="system", target=user.username,
                         detail={"via": "cli"})
            print(f"New temporary password set for {user.username}; its sessions were ended.")
        else:
            accounts.unlock(db, user)
            audit.record("user.unlock", audit.SUCCESS, actor="system", target=user.username,
                         detail={"via": "cli"})
            print(f"{user.username} unlocked.")
        return 0
    except accounts.AccountError as exc:
        sys.exit(str(exc))
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
