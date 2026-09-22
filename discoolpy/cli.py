"""Command line interface: ``discoolpy <command> [scenario.yaml]``.

Four steps, and two extras. Define a scenario, check it, run it, report it::

    discoolpy new    my_scenario.yaml   # 1. define: a template to fill in
    discoolpy check  my_scenario.yaml   # 2. check:  design point, seconds
    discoolpy run    my_scenario.yaml   # 3. run:    the time series
    discoolpy report my_scenario.yaml   # 4. report: the results, as figures

Each step names the next when it finishes, so the sequence does not have to be
remembered. ``discoolpy run --report`` collapses the last two into one.

Six commands in full:

``discoolpy new my_scenario.yaml``
    Write a commented template you fill in. ``--minimal`` gives you the
    twenty-line version instead of the annotated one.
``discoolpy check my_scenario.yaml``
    Solve the design point and report on it. Seconds, not minutes.
``discoolpy plot my_scenario.yaml``
    Draw the plan view. No solve at all, so it works on a scenario that does
    not converge yet.
``discoolpy run my_scenario.yaml``
    The time series, plus a paired storage comparison where the scenario has
    a store.
``discoolpy report my_scenario.yaml``
    Run it, then write the printed report and every result figure, per
    component and for the system as a whole, to a directory.
``discoolpy list``
    The scenarios shipped with the package.

The same work is available from Python through
:func:`discoolpy.run_scenario` and :func:`discoolpy.check_scenario`, and the
CLI is a thin wrapper over both.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .errors import ScenarioError

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_CONFIGS = PACKAGE_ROOT.parent / "configs"
TEMPLATES = PACKAGE_ROOT / "templates"


def _shipped_configs() -> List[Path]:
    if REPO_CONFIGS.is_dir():
        return sorted(REPO_CONFIGS.glob("*.yaml"))
    return []


def _template(name: str) -> str:
    path = TEMPLATES / name
    if not path.exists():
        raise SystemExit(f"Template {name!r} is missing from the installed package ({path}).")
    return path.read_text(encoding="utf-8")


def cmd_new(args: argparse.Namespace) -> int:
    target = Path(args.output)
    if target.exists() and not args.force:
        print(f"{target} already exists. Pass --force to overwrite it.", file=sys.stderr)
        return 1
    body = _template("minimal_scenario.yaml" if args.minimal else "scenario_template.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    print(f"Wrote {target}")
    print()
    print("It runs as it stands. Edit the buildings and the pipe lengths, then:")
    print(f"  discoolpy check  {target}   # is the specification well posed?")
    print(f"  discoolpy run    {target}   # the time series")
    print(f"  discoolpy report {target}   # the results, as figures")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .scenario import check_scenario

    paths = _targets(args)
    failures = []
    for path in paths:
        layout = None
        if args.layout:
            layout = Path(args.layout_dir) / f"{path.stem}.png"
        result = check_scenario(path, layout_path=layout, strict=args.strict)
        if not result.ok:
            failures.append(path.name)
    if failures:
        print(f"Design-point check failed for: {', '.join(failures)}", file=sys.stderr)
        return 1
    if len(paths) > 1:
        print(f"All {len(paths)} scenario(s) checked.")
    return 0


def cmd_plot(args: argparse.Namespace) -> int:
    import matplotlib.pyplot as plt

    from .layout import plot_network
    from .scenario import load_scenario
    from .utils import build_system

    targets = _targets(args)
    for path in targets:
        target = Path(args.output) if args.output else Path(f"{path.stem}_layout.png")
        if len(targets) > 1:
            target = Path(args.output or ".") / f"{path.stem}_layout.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        system = build_system(load_scenario(path), strict_hydraulics=False)
        ax = plot_network(system, save_path=str(target), annotate_pipes=not args.plain)
        plt.close(ax.figure)
        print(f"Wrote {target}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .scenario import run_scenario

    compare: Optional[bool] = None
    if args.no_compare:
        compare = False
    for path in _targets(args):
        result = run_scenario(
            path,
            periods=args.periods,
            compare_storage=compare,
            progress=not args.quiet,
            plot=not args.no_plot,
            report=args.report,
        )
        print()
        print(result.summary())
        if result.report is not None:
            print()
            print(result.report.to_markdown())
        if not args.report:
            name = path.name
            print()
            print(f"Next: discoolpy report {name}   (the results, as figures)")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Run a scenario, or read a finished one, and document it in figures."""
    import pandas as pd

    from .reporting import plot_all, print_report
    from .scenario import load_scenario, run_scenario
    from .utils import build_system, ensure_output_dir

    for path in _targets(args):
        directory = (
            Path(args.output) if args.output
            else ensure_output_dir(load_scenario(path)) / "figures"
        )

        if args.results:
            # A finished run, redrawn. The system is still built, because the
            # design-point panels and the layout come from it rather than from
            # the frame, but nothing is stepped through time twice.
            config = load_scenario(path)
            system = build_system(config, strict_hydraulics=False)
            system.network.solve(mode="design", max_iter=300)
            frame = pd.read_csv(args.results)
            paths = plot_all(system, directory, results=frame,
                             prefix=path.stem, dpi=args.dpi)
            print()
            print_report(system, frame)
        else:
            result = run_scenario(
                path,
                periods=args.periods,
                compare_storage=False if args.no_compare else None,
                progress=not args.quiet,
                plot=False,
            )
            paths = result.plot_all(directory, prefix=path.stem, dpi=args.dpi)
            print()
            result.print_report()

        print()
        for name, written in paths.items():
            print(f"  {name:<14} {written}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    import yaml

    configs = _shipped_configs()
    if not configs:
        print("No shipped scenarios found. Run this from a source checkout to see them.")
        return 0
    print(f"{len(configs)} scenario(s) in {REPO_CONFIGS}:")
    for path in configs:
        description = ""
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            meta = data.get("metadata") or {}
            description = " ".join(str(meta.get("description", "")).split())
        except Exception:
            pass
        print(f"  {path.name}")
        if description:
            print(f"      {description[:140]}")
    return 0


def _targets(args: argparse.Namespace) -> List[Path]:
    if getattr(args, "all", False):
        configs = _shipped_configs()
        if not configs:
            raise SystemExit("--all needs the shipped configs/ directory, which is not installed.")
        return configs
    scenario = getattr(args, "scenario", None)
    if not scenario:
        raise SystemExit("Give a scenario file, or --all for every shipped scenario.")
    path = Path(scenario)
    if not path.exists():
        raise SystemExit(f"Scenario file not found: {path}")
    return [path]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discoolpy",
        description="Model and simulate a district cooling network from a YAML scenario.",
    )
    parser.add_argument("--version", action="version", version=f"discoolpy {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    new = sub.add_parser("new", help="write a scenario template to fill in")
    new.add_argument("output", nargs="?", default="scenario.yaml", help="file to write")
    new.add_argument("--minimal", action="store_true",
                     help="the short version, without the explanatory comments")
    new.add_argument("--force", action="store_true", help="overwrite an existing file")
    new.set_defaults(func=cmd_new)

    check = sub.add_parser("check", help="solve the design point and report on the scenario")
    check.add_argument("scenario", nargs="?", help="YAML scenario file")
    check.add_argument("--all", action="store_true", help="every shipped scenario")
    check.add_argument("--layout", action="store_true", help="also draw the plan view")
    check.add_argument("--layout-dir", default="outputs/layouts",
                       help="where --layout writes its figures")
    check.add_argument("--strict", action="store_true",
                       help="treat structural warnings as failures")
    check.set_defaults(func=cmd_check)

    plot = sub.add_parser("plot", help="draw the network plan view, without solving")
    plot.add_argument("scenario", nargs="?", help="YAML scenario file")
    plot.add_argument("--all", action="store_true", help="every shipped scenario")
    plot.add_argument("-o", "--output", help="output PNG (or directory, with --all)")
    plot.add_argument("--plain", action="store_true", help="omit the pipe annotations")
    plot.set_defaults(func=cmd_plot)

    run = sub.add_parser("run", help="run the time series and write the results")
    run.add_argument("scenario", nargs="?", help="YAML scenario file")
    run.add_argument("--all", action="store_true", help="every shipped scenario")
    run.add_argument("-n", "--periods", type=int,
                     help="override profiles.periods, for a shorter run")
    run.add_argument("--no-compare", action="store_true",
                     help="run the stored case alone instead of pairing it against no storage")
    run.add_argument("--no-plot", action="store_true", help="skip the comparison figure")
    run.add_argument("--report", action="store_true",
                     help="also write every result figure, as 'discoolpy report' would")
    run.add_argument("-q", "--quiet", action="store_true", help="no per-snapshot progress")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser(
        "report", help="run a scenario and write every result figure and the report")
    report.add_argument("scenario", nargs="?", help="YAML scenario file")
    report.add_argument("--all", action="store_true", help="every shipped scenario")
    report.add_argument("-o", "--output", help="directory to write into")
    report.add_argument("-n", "--periods", type=int,
                        help="override profiles.periods, for a shorter run")
    report.add_argument("--results",
                        help="use this finished results CSV instead of running the scenario")
    report.add_argument("--no-compare", action="store_true",
                        help="run the stored case alone instead of pairing it")
    report.add_argument("--dpi", type=int, default=160, help="figure resolution")
    report.add_argument("-q", "--quiet", action="store_true", help="no per-snapshot progress")
    report.set_defaults(func=cmd_report)

    listing = sub.add_parser("list", help="list the scenarios shipped with the package")
    listing.set_defaults(func=cmd_list)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    warnings.filterwarnings("ignore", category=FutureWarning)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args) or 0)
    except ScenarioError as error:
        # A scenario file is written by hand, so a problem in one is a message,
        # not a traceback. The exception is still available to the Python API.
        print(error.render(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
