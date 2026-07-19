"""res-model CLI: init-workbook, calibrate, project, validate.

Phase 0 wires the commands; calibrate/project/validate are implemented in later phases.
"""
from __future__ import annotations

import argparse
import sys

from .config import load_config

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")     # Windows cp1252 safety
    except (AttributeError, ValueError):
        pass


def cmd_init_workbook(args):
    from .io.assumptions import build_template
    cfg = load_config(args.config)
    out = cfg.path.parent / "assumptions_template.xlsx"      # never the live merged scenarios.xlsx
    build_template(out)
    print(f"[init-workbook] reference template -> {out}\n"
          "  The live source is the merged ../scenarios.xlsx; copy tabs across with a 'res_' prefix.")


def cmd_calibrate(args):
    from .pipeline import calibrate
    calibrate(load_config(args.config))


def cmd_project(args):
    from .pipeline import project
    project(load_config(args.config))


def cmd_validate(args):
    from .pipeline import run_validation
    run_validation(load_config(args.config))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="res-model", description=__doc__)
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init-workbook", help="write the assumptions workbook template").set_defaults(func=cmd_init_workbook)
    sub.add_parser("calibrate", help="calibrate the conversion chains on history").set_defaults(func=cmd_calibrate)
    sub.add_parser("project", help="project RES production from weather draws").set_defaults(func=cmd_project)
    sub.add_parser("validate", help="run the validation suite").set_defaults(func=cmd_validate)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
