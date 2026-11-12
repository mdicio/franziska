"""python -m franziska <command>: setup and checks. The bot, the web UI and the CLIs keep their own modules.

    uv run python -m franziska init                       # write ~/.config/franziska/.env, link it here
    uv run --env-file .env python -m franziska doctor     # what works, what doesn't, and the fix
    uv run python -m franziska demo                       # the web UI on a made-up person's data
"""

import argparse
import sys


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m franziska", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="write a first configuration")
    i.add_argument("--force", action="store_true", help="overwrite an existing ~/.config/franziska/.env")
    i.add_argument("--yes", action="store_true", help="take every default, ask nothing")
    sub.add_parser("doctor", help="check the configuration and everything it uses")
    d = sub.add_parser("demo", help="the web UI on a made-up person's data, in a scratch directory")
    d.add_argument("--port", type=int, default=8766)
    a = p.parse_args()
    if a.cmd == "init":
        from franziska.setup import init

        return init(force=a.force, yes=a.yes)
    if a.cmd == "doctor":
        from franziska.setup import doctor

        return doctor()
    from franziska.demo import main as demo

    return demo(port=a.port)


if __name__ == "__main__":
    sys.exit(main())
