"""Design-point sanity check for a scenario: hydraulics, temperatures, heat gains.

Solves the design point only, so it takes seconds, and prints what you need to
decide whether the scenario is set up correctly before committing to a time
series. It walks the whole branch tree, so a network that forks into spurs and
satellite plants is reported branch by branch.

    python validate_scenario.py
    python validate_scenario.py --config ../configs/config_branching_grid.yaml
    python validate_scenario.py --all           # every shipped scenario
    python validate_scenario.py --all --layout  # and draw each layout

The same check is `discoolpy check`, and `discoolpy.check_scenario` from
Python. This script is the argparse wrapper that predates the CLI, kept
because the examples and the docs point at it.
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

from discoolpy import check_scenario

warnings.filterwarnings("ignore", category=FutureWarning)

HERE = Path(__file__).resolve().parent
CONFIG_DIR = HERE.parent / "configs"
DEFAULT_CONFIG = CONFIG_DIR / "config_length_derived_pr.yaml"


def validate(config_path: Path, draw_layout: bool = False) -> bool:
    """Check one scenario and return whether it is usable."""
    layout = None
    if draw_layout:
        layout = HERE.parent / "outputs" / "layouts" / f"{config_path.stem}.png"
    return check_scenario(config_path, layout_path=layout).ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a scenario at its design point.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--all", action="store_true", help="Validate every scenario in configs/.")
    parser.add_argument("--layout", action="store_true", help="Also draw the network layout.")
    args = parser.parse_args()

    paths = sorted(CONFIG_DIR.glob("*.yaml")) if args.all else [Path(args.config)]
    failures = [p.name for p in paths if not validate(p, draw_layout=args.layout)]
    if failures:
        raise SystemExit(f"Validation failed for: {', '.join(failures)}")
    print(f"All {len(paths)} scenario(s) validated.")


if __name__ == "__main__":
    main()
