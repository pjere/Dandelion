"""Render the full per-function API reference from the docstrings, into docs/api/ (git-ignored).

The prose docs (docs/MODELLING.md, docs/ARCHITECTURE.md, per-package METHODOLOGY.md) are the curated
layer; this script renders the exhaustive, always-current per-function reference straight from the
docstrings with `pdoc` — so "every function is documented" stays true automatically and no generated
HTML is committed. Run it after any public-API change:

    pip install -r requirements-dev.txt      # or: pip install pdoc
    python scripts/build_docs.py

All seven packages must be importable (install them editable — see docs/INSTALL.md).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "api"

# the importable top-level package names, in dependency order
PACKAGES = [
    "powersim_core",
    "pricemodeling",
    "weathergen",
    "demand_model",
    "res_model",
    "availability_model",
    "dispatch_model",
]


def main() -> int:
    try:
        import pdoc  # noqa: F401
    except ImportError:
        print("pdoc is not installed. Run:  pip install -r requirements-dev.txt   (or: pip install pdoc)",
              file=sys.stderr)
        return 2

    missing = []
    for pkg in PACKAGES:
        try:
            __import__(pkg)
        except ImportError as exc:
            missing.append(f"{pkg} ({exc})")
    if missing:
        print("These packages are not importable — install them editable first "
              "(see docs/INSTALL.md):\n  " + "\n  ".join(missing), file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pdoc", "--output-directory", str(OUT), *PACKAGES]
    print("Rendering API docs:\n  " + " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode == 0:
        print(f"\nAPI reference written to {OUT / 'index.html'}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
