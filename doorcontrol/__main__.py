import argparse
import os
import sys

from . import __version__


def main():
    p = argparse.ArgumentParser(prog="doorcontrol", description="Door Control for 2N intercoms")
    p.add_argument("--data", default=os.environ.get("DOORCONTROL_DATA", "data"), help="data folder")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default=os.environ.get("DOORCONTROL_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("DOORCONTROL_PORT", "8080")))
    s.add_argument("--cert", default=os.environ.get("DOORCONTROL_CERT", ""))
    s.add_argument("--key", default=os.environ.get("DOORCONTROL_KEY", ""))

    a = sub.add_parser("reset-admin", help="create the admin user or give it a new password")
    a.add_argument("--username", default="admin")
    a.add_argument("--password", default=None)

    sub.add_parser("has-users", help="exit 0 when there is at least one user")

    r = sub.add_parser("restore", help="put a downloaded backup back")
    r.add_argument("file")

    sub.add_parser("version")

    args = p.parse_args()
    data = os.path.abspath(args.data)

    if args.cmd == "serve":
        from .server import serve
        serve(data, args.host, args.port, args.cert, args.key)
    elif args.cmd == "reset-admin":
        from .app import make_admin
        pw = make_admin(data, args.username, args.password)
        print(f"username: {args.username}")
        print(f"password: {pw}")
    elif args.cmd == "has-users":
        from .app import has_users
        sys.exit(0 if has_users(data) else 1)
    elif args.cmd == "restore":
        from .app import restore
        restore(data, args.file)
        print("restored, restart the service: systemctl restart doorcontrol")
    elif args.cmd == "version":
        print(__version__)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
