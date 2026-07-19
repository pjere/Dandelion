"""dispatch CLI: build-inputs, run, backtest, validate."""
from __future__ import annotations

import argparse
import sys

from .config import load_config

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def cmd_build_inputs(args):
    from .pipeline import build_inputs
    build_inputs(load_config(args.config))


def cmd_run(args):
    from .pipeline import run
    run(load_config(args.config), years=[args.year] if args.year else None)


def cmd_backtest(args):
    from .pipeline import backtest
    backtest(load_config(args.config), year=args.year or 2019)


def cmd_validate(args):
    from .pipeline import run_validation
    run_validation(load_config(args.config))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="dispatch", description=__doc__)
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("build-inputs", help="build commodity + neighbour + FR inputs").set_defaults(func=cmd_build_inputs)
    r = sub.add_parser("run", help="projection run → per-zone annual price stats")
    r.add_argument("--year", type=int, default=None, help="target projection year (default 2030)")
    r.set_defaults(func=cmd_run)
    b = sub.add_parser("backtest", help="run on a historical year with actual inputs")
    b.add_argument("--year", type=int, default=None)
    b.set_defaults(func=cmd_backtest)
    sub.add_parser("validate", help="run the validation suite").set_defaults(func=cmd_validate)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
