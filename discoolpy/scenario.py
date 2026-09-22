"""One-call entry points for a scenario file.

The pieces in :mod:`discoolpy.utils` are all separately useful, and a study
that wants to interleave its own control logic between snapshots should keep
reaching for them. Most of the time, though, you want the same six steps in the
same order: read the YAML, check the specification is well posed, generate a
profile, solve the design point, step through the profile, write the results
out. That is what :func:`run_scenario` does.

    from discoolpy import run_scenario

    result = run_scenario("configs/config_riyadh_heat_gains.yaml")
    print(result.report.to_markdown())

Where the scenario has a store enabled, the run is a paired one by default:
the same network, the same weather, once without the store and once with it,
followed by a flexibility assessment of the difference. That is the comparison
a storage study is really after, and doing it by hand means keeping two result
frames and a dispatch flag straight.

:func:`check_scenario` is the cheap counterpart. It solves the design point
only, prints the topology, the degrees of freedom, the heat-gain conductances
and the plant energy balance, and returns without touching the time series.
Run it first. A scenario that fails here will fail 336 times in a row if you
go straight to :func:`run_scenario`.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

import pandas as pd

from .branch import SatellitePlant, SubBranch
from .config_schema import branch_pipe_thermal, make_branch_specs, validate_scenario
from .errors import ScenarioError
from .flexibility import FlexibilityReport, assess_flexibility
from .hydraulics import analyse_network_topology, suggest_pressure_specification
from .utils import (
    build_system,
    ensure_output_dir,
    load_yaml_config,
    make_storage_comparison_plot,
    make_weather_and_load_profiles,
    network_design_mass_flows,
    network_topology,
    output_path,
    run_configured_case,
    write_storage_comparison_summary,
)

__all__ = [
    "ScenarioResult",
    "CheckResult",
    "load_scenario",
    "run_scenario",
    "check_scenario",
]

ConfigLike = Union[str, Path, Mapping[str, Any]]


def load_scenario(
    config: ConfigLike, validate: bool = True, strict: bool = False
) -> Dict[str, Any]:
    """Accept a path or an already-loaded mapping and return a mutable config.

    Checks the structure on the way in, because every entry point goes through
    here and a misspelled key is otherwise not an error but a key nobody reads.
    Warnings are attached as ``_warnings`` rather than printed, so the caller
    decides where they go; :func:`check_scenario` prints them and
    :func:`run_scenario` passes them to :mod:`warnings`.

    Pass ``validate=False`` for a config assembled in code that deliberately
    carries keys of its own.
    """
    loaded = load_yaml_config(config) if isinstance(config, (str, Path)) else dict(config)
    if validate:
        loaded["_warnings"] = validate_scenario(loaded, strict=strict)
    return loaded


def _safe_stem(name: str) -> str:
    """A scenario name turned into something safe to put in a filename."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))
    return cleaned.strip("_") or "scenario"


def _scenario_name(config: Mapping[str, Any]) -> str:
    meta = config.get("metadata") or {}
    if meta.get("name"):
        return str(meta["name"])
    path = config.get("_config_path")
    return Path(path).stem if path else "scenario"


@dataclass
class ScenarioResult:
    """Everything one :func:`run_scenario` call produced.

    The four steps of a study are define, check, run and report, and this is
    what the third hands to the fourth: the result frames, the system that
    produced them, and the paths of everything written. :meth:`print_report`
    and :meth:`plot_all` close the loop without rebuilding anything.
    """

    name: str
    config: Dict[str, Any]
    profile: pd.DataFrame
    #: Result frame per case. A paired run has ``without_storage`` and
    #: ``with_storage``; a single run has ``base``.
    cases: Dict[str, pd.DataFrame] = field(default_factory=dict)
    report: Optional[FlexibilityReport] = None
    files: Dict[str, Path] = field(default_factory=dict)
    #: The solved network behind :attr:`results`. Kept so the reporting layer
    #: can read design-point conductances and geometry without solving again.
    system: Any = None
    #: Structural warnings raised while the scenario was read.
    warnings: List[str] = field(default_factory=list)

    @property
    def results(self) -> pd.DataFrame:
        """The frame a caller most likely means: the flexible case if there is one."""
        for key in ("with_storage", "base", "without_storage"):
            if key in self.cases:
                return self.cases[key]
        raise KeyError("no case was run")

    @property
    def paired(self) -> bool:
        return "with_storage" in self.cases and "without_storage" in self.cases

    @property
    def output_dir(self) -> Path:
        """Where this scenario writes."""
        return ensure_output_dir(self.config)

    def print_report(self) -> Any:
        """Print the per-component report for this run, and return it."""
        from .reporting import print_report

        return print_report(self.system, self.results)

    def plot_all(self, output_dir: Optional[Union[str, Path]] = None, **kwargs: Any):
        """Write every result figure, and return the paths.

        Defaults to a ``figures`` directory beside the scenario's other output,
        so a run and its figures stay together without being asked where.
        """
        from .reporting import plot_all

        target = Path(output_dir) if output_dir else self.output_dir / "figures"
        paths = plot_all(self.system, target, results=self.results,
                         prefix=_safe_stem(self.name), **kwargs)
        self.files.update({f"figure_{k}": v for k, v in paths.items()})
        return paths

    def summary(self) -> str:
        """A few lines of plain text describing what came out."""
        frame = self.results
        lines = [
            f"{self.name}: {len(frame)} snapshots at "
            f"{frame['resolution_hours'].iloc[0]:g} h",
            f"  peak compressor power : {frame['compressor_power_W'].max() / 1e3:9.1f} kW",
            f"  compressor energy     : "
            f"{(frame['compressor_power_W'] * frame['resolution_hours']).sum() / 1e3:9.1f} kWh",
            f"  mean COP              : {frame['cop'].mean():9.3f}",
        ]
        if "pipe_heat_gain_W" in frame:
            share = frame["pipe_heat_gain_W"].sum() / max(
                frame["actual_building_total_Q_W"].sum(), 1e-9
            )
            lines.append(f"  distribution pipe gain: {100 * share:9.2f} % of demand")
        for key, path in self.files.items():
            lines.append(f"  {key:<22}: {path}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.summary()


def run_scenario(
    config: ConfigLike,
    periods: Optional[int] = None,
    compare_storage: Optional[bool] = None,
    progress: bool = True,
    plot: bool = True,
    report: bool = False,
    report_dir: Optional[Union[str, Path]] = None,
) -> ScenarioResult:
    """Run a scenario end to end and write its outputs.

    Parameters
    ----------
    config:
        Path to a YAML scenario, or a config mapping already loaded.
    periods:
        Override ``profiles.periods`` for a shorter run. Handy for a first
        look: 48 snapshots tell you whether the scenario behaves long before
        336 of them tell you what it costs.
    compare_storage:
        Run the scenario twice, without and with the store, and assess the
        difference. Defaults to True when the scenario enables a store and
        False when it does not. Set it False on a stored scenario to run the
        stored case alone.
    progress:
        Print per-snapshot progress while solving.
    plot:
        Write the comparison figure. Paired runs only.
    report:
        Also write the per-component result figures and the printed report,
        which is the fourth step of define, check, run, report. Off by default
        so a scripted sweep is not paying for figures nobody opens.
    report_dir:
        Where ``report`` writes. Defaults to ``figures/`` beside the scenario's
        other output.

    Returns
    -------
    ScenarioResult
        Result frames, the solved system, the flexibility report where there is
        one, and the paths of every file written.
    """
    cfg = load_scenario(config)
    structural = list(cfg.get("_warnings") or [])
    for note in structural:
        warnings.warn(f"{_scenario_name(cfg)}: {note}", stacklevel=2)
    if periods is not None:
        cfg.setdefault("profiles", {})["periods"] = int(periods)

    storage_enabled = bool((cfg.get("storage") or {}).get("enabled"))
    if compare_storage is None:
        compare_storage = storage_enabled
    if compare_storage and not storage_enabled:
        raise ValueError(
            f"{_scenario_name(cfg)} has no storage enabled, so there is nothing to compare. "
            "Add 'storage: {enabled: true}' to the scenario, or pass compare_storage=False."
        )

    profile = make_weather_and_load_profiles(cfg)
    files: Dict[str, Path] = {}
    files["profile"] = output_path(cfg, "profile_csv", "input_profile.csv")
    profile.to_csv(files["profile"], index=False)

    cases: Dict[str, pd.DataFrame] = {}
    flexibility: Optional[FlexibilityReport] = None
    system: Any = None

    if compare_storage:
        cases["without_storage"] = run_configured_case(
            cfg, profile, "without_storage", use_storage=False, progress=progress
        )
        files["without_storage"] = output_path(
            cfg, "without_storage_csv", "without_storage_results.csv"
        )
        cases["without_storage"].to_csv(files["without_storage"], index=False)

        cases["with_storage"], system = run_configured_case(
            cfg, profile, "with_storage", use_storage=True, progress=progress,
            return_system=True,
        )
        files["with_storage"] = output_path(cfg, "with_storage_csv", "with_storage_results.csv")
        cases["with_storage"].to_csv(files["with_storage"], index=False)

        write_storage_comparison_summary(cfg, cases["without_storage"], cases["with_storage"])
        files["comparison"] = output_path(
            cfg, "comparison_csv", "storage_comparison_summary_timeseries.csv"
        )
        files["summary"] = output_path(cfg, "summary_md", "storage_comparison_summary.md")
        if plot:
            make_storage_comparison_plot(cfg, cases["without_storage"], cases["with_storage"])
            files["plot"] = output_path(cfg, "plot_png", "storage_comparison_results.png")

        economics = cfg.get("economics") or {}
        flexibility = assess_flexibility(
            cases["without_storage"],
            cases["with_storage"],
            reference_name="without_storage",
            flexible_name="with_storage",
            tariff=economics.get("tariff"),
            carbon_intensity=economics.get("carbon_intensity_kg_kWh"),
        )
    else:
        cases["base"], system = run_configured_case(
            cfg, profile, "base", use_storage=storage_enabled, progress=progress,
            return_system=True,
        )
        files["results"] = output_path(cfg, "results_csv", "results.csv")
        cases["base"].to_csv(files["results"], index=False)

    result = ScenarioResult(
        name=_scenario_name(cfg),
        config=cfg,
        profile=profile,
        cases=cases,
        report=flexibility,
        files=files,
        system=system,
        warnings=structural,
    )
    if report:
        result.plot_all(report_dir)
    return result


@dataclass
class CheckResult:
    """What :func:`check_scenario` found.

    Truthy when the scenario is usable, so it drops straight into an ``assert``
    or a continuous-integration step.
    """

    name: str
    ok: bool
    #: Lines of the printed report, kept so a notebook or a test can read them.
    lines: List[str] = field(default_factory=list)
    system: Any = None
    layout_path: Optional[Path] = None
    #: Structural warnings raised while the scenario was read.
    warnings: List[str] = field(default_factory=list)
    #: What went wrong, when the scenario could not be read at all.
    error: Optional[ScenarioError] = None

    def __str__(self) -> str:
        return "\n".join(self.lines)

    def __bool__(self) -> bool:
        return self.ok


def check_scenario(
    config: ConfigLike,
    layout_path: Optional[Union[str, Path]] = None,
    verbose: bool = True,
    strict: bool = False,
) -> CheckResult:
    """Solve the design point and report on the scenario, without a time series.

    The second of the four steps. It covers, in the order a scenario usually
    goes wrong: the structure of the file itself, the branch tree and what
    hangs off it, the pressure degrees of freedom, the pipe heat-gain
    conductances, the design solve, the plant energy balance, the operating
    point, hydraulic feasibility, and the solved pressures branch by branch.

    A scenario that cannot be read at all comes back as a falsy
    :class:`CheckResult` carrying the :class:`~discoolpy.errors.ScenarioError`
    rather than raising it: reporting problems is the whole point of the step,
    and a malformed file is the most common problem there is.

    Pass ``layout_path`` to also draw the plan view, ``strict`` to treat
    structural warnings as failures, and ``verbose=False`` to collect the
    report without printing it.
    """
    lines: List[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        if verbose:
            print(text)

    name = (
        Path(config).stem if isinstance(config, (str, Path)) else "scenario"
    )
    try:
        cfg = load_scenario(config, strict=strict)
    except ScenarioError as error:
        say("=" * 78)
        say(name)
        say("=" * 78)
        say("")
        say(error.render())
        return CheckResult(name=name, ok=False, lines=lines, error=error)

    name = _scenario_name(cfg)
    structural = list(cfg.get("_warnings") or [])

    def fail() -> CheckResult:
        # Every check ends with a verdict, whichever stage it stopped at, so a
        # reader never has to work out from a wall of output whether it passed.
        say("")
        say("-- Verdict --")
        say(f"  {name} is not usable yet. Fix what is reported above, then"
            f" check it again.")
        say("")
        return CheckResult(name=name, ok=False, lines=lines, warnings=structural)

    say("=" * 78)
    say(name)
    say("=" * 78)

    if structural:
        say("")
        say("-- Structure --")
        for note in structural:
            say(f"  [warning] {note}")

    labels = [str(b["label"]) for b in cfg.get("buildings", [])]
    root = make_branch_specs(cfg)
    flows = network_design_mass_flows(cfg, root)

    say("")
    say("-- Network topology --")

    def walk(spec, depth: int) -> None:
        indent = "  " + "    " * depth
        say(f"{indent}[{spec.label}]  {spec.n_buildings} building(s), "
            f"{flows[spec.label]['inlet']:.2f} kg/s at the inlet")
        for order, index in enumerate(spec.building_indices, start=1):
            say(f"{indent}    {order}. {labels[index]}")
        for terminal in spec.terminals:
            if terminal.child is not None:
                walk(terminal.child, depth + 1)
                continue
            m = flows[spec.label]["terminals"][terminal.label]
            extra = ""
            if terminal.kind == "plant":
                duty = terminal.raw.get("Q_evap_kW")
                if duty is None and terminal.raw.get("Q_evap_W") is not None:
                    duty = float(terminal.raw["Q_evap_W"]) / 1e3
                extra = f", {duty} kW"
                if terminal.raw.get("storage"):
                    extra += " + store"
            say(f"{indent}    -> {terminal.kind}: {terminal.label} ({m:.2f} kg/s{extra})")

    walk(root, 0)

    # Structural check first. A sentence here beats a TESPy cycle dump.
    report = analyse_network_topology(network_topology(cfg))
    say("")
    say("-- Hydraulic degrees of freedom --")
    say(report.message())
    if not report.ok:
        if report.n_forks == 0:
            say("")
            say("A consistent specification for this branch would be:")
            bypass_fixed = root.terminals[0].pressure_spec() is not None
            for key, value in suggest_pressure_specification(
                root.n_buildings, bypass_fixed
            ).items():
                say(f"  {key}: {value}")
        return fail()

    say("")
    say("-- Pipe heat models --")
    total_ua = 0.0
    any_active = False
    for spec in root.walk():
        thermal = branch_pipe_thermal(spec, cfg)
        active = {k: v for k, v in thermal.items() if v.model != "adiabatic"}
        if not active:
            continue
        any_active = True
        say(f"  [{spec.label}]")
        for key, pipe in active.items():
            ua = pipe.resolved_UA()
            total_ua += ua
            per_m = f"{pipe.UA_per_m_W_mK:.3f} W/(m.K)" if pipe.UA_per_m_W_mK else "-"
            say(f"    {key:<10} {pipe.model:<14} UA={ua:8.1f} W/K  ({per_m}, "
                f"L={pipe.length_m}, ambient={pipe.ambient_source} @ "
                f"{pipe.ambient_temperature_degC} degC)")
    if any_active:
        say(f"  {'network total':<12} UA={total_ua:8.1f} W/K")
    else:
        say("  All pipes adiabatic (Q = 0). No distribution heat gain is represented.")

    system = build_system(cfg)
    for note in system.notes:
        say("")
        say(f"[note] {note}")
    say("")
    say(f"-- Design solve (plant control: {system.plant_control}) --")
    system.network.solve(
        mode="design", max_iter=int((cfg.get("solver") or {}).get("max_iter", 300))
    )
    say(f"  converged : {system.network.converged}")
    if not system.network.converged:
        return fail()

    branch = system.branch
    gains = branch.heat_gain_report()
    satellites = branch.satellite_report()
    q_evap = system.chiller.solved_Q_evap_W
    q_buildings = sum(b.component.Q.val for b in system.buildings)
    pump = branch.pump.P.val if branch.pump is not None else 0.0
    power = system.chiller.compressor.P.val

    say("")
    say("-- Plant energy balance --")
    say(f"  building cooling demand : {q_buildings / 1e3:9.2f} kW")
    say(f"  distribution pipe gain  : {gains['total_heat_gain_W'] / 1e3:9.2f} kW"
        f"   ({100 * gains['total_heat_gain_W'] / q_buildings:+.2f} % of demand)")
    say(f"    of which supply mains : {gains['supply_heat_gain_W'] / 1e3:9.2f} kW")
    say(f"    of which return mains : {gains['return_heat_gain_W'] / 1e3:9.2f} kW")
    say(f"  pump heat               : {pump / 1e3:9.2f} kW")
    if satellites["per_plant"]:
        say(f"  satellite plants        : {-satellites['total_Q_evap_W'] / 1e3:9.2f} kW"
            "   (duty carried away from the central machine)")
        for plant_name, values in satellites["per_plant"].items():
            say(f"    {plant_name:<22} {values['Q_evap_W'] / 1e3:7.2f} kW  "
                f"COP {values['cop']:.3f}  "
                f"delivering {values['delivered_Q_W'] / 1e3:7.2f} kW")
    say("  ------------------------------------")
    say(f"  chiller evaporator duty : {q_evap / 1e3:9.2f} kW")
    residual = (
        q_evap
        + satellites["total_Q_evap_W"]
        - satellites["total_storage_Q_W"]
        - q_buildings
        - gains["total_heat_gain_W"]
        - pump
    )
    say(f"  residual (must be ~0)   : {residual:9.4f} W")

    supply_T = branch.connections["branch_in"].T.val
    return_T = branch.connections["branch_out"].T.val
    say("")
    say("-- Operating point --")
    say(f"  compressor power  : {power / 1e3:8.2f} kW      COP: {q_evap / power:.3f}")
    if satellites["per_plant"]:
        fleet_power = power + satellites["total_compressor_power_W"]
        fleet_cooling = q_evap + satellites["total_Q_evap_W"]
        say(f"  fleet power       : {fleet_power / 1e3:8.2f} kW      "
            f"fleet COP: {fleet_cooling / fleet_power:.3f}")
    say(f"  CHW supply / return : {supply_T:6.2f} / {return_T:6.2f} degC  "
        f"(delta-T {return_T - supply_T:.2f} K)")
    say(f"  CHW mass flow       : {branch.connections['branch_in'].m.val_SI:6.2f} kg/s")
    say(f"  plant leaving water : "
        f"{system.chiller.internal_connections['chw_out'].T.val:6.2f} degC")

    feasibility = branch.pressure_feasibility()
    say("")
    say("-- Hydraulic feasibility --")
    say(f"  {feasibility['message']}")

    say("")
    say("-- Solved pressures and duties, branch by branch --")
    for node in system.branches:
        say(f"  [{node.label}]  inlet p={node.connections['branch_in'].p.val:6.3f} bar, "
            f"m={node.connections['branch_in'].m.val_SI:6.2f} kg/s, "
            f"T={node.connections['branch_in'].T.val:5.2f} degC")
        for building in node.buildings:
            drop = building.inlet.p.val - building.outlet.p.val
            say(f"      {building.label:<20} dp={drop:6.3f} bar "
                f"(pr={building.outlet.p.val / building.inlet.p.val:.4f})  "
                f"Q={building.component.Q.val / 1e3:7.1f} kW")
        for terminal in node.terminals:
            if isinstance(terminal, SubBranch):
                continue
            inlet, outlet = terminal.inlet_connection(), terminal.outlet_connection()
            kind = "satellite" if isinstance(terminal, SatellitePlant) else "bypass"
            say(f"      {terminal.label:<20} dp={inlet.p.val - outlet.p.val:6.3f} bar  "
                f"m={inlet.m.val_SI:6.2f} kg/s  [{kind}]")

    if system.storage is not None:
        say("")
        say("-- Central storage --")
        say(f"  coupling   : {system.storage.coupling}")
        say(f"  loss model : {system.storage.loss_model}")
        if system.storage.thermal is not None:
            th = system.storage.thermal
            say(f"  tank UA    : {th.UA_W_K:.1f} W/K  -> "
                f"{th.heat_gain_W(43.0, system.storage.soc) / 1e3:.2f} kW gain at 43 degC ambient")

    drawn: Optional[Path] = None
    if layout_path is not None:
        import matplotlib.pyplot as plt

        from .layout import plot_network

        drawn = Path(layout_path)
        drawn.parent.mkdir(parents=True, exist_ok=True)
        ax = plot_network(system, save_path=str(drawn), annotate_pipes=True)
        plt.close(ax.figure)
        say("")
        say("-- Layout --")
        say(f"  written to {drawn}")

    if not feasibility["feasible"]:
        result = fail()
        result.system = system
        result.layout_path = drawn
        return result

    # The check is the second of four steps, so it ends by naming the third.
    # A scenario that passes here is one somebody is about to run, and the
    # command to run it should not have to be looked up.
    source = cfg.get("_config_path")
    target = Path(source).name if source else "my_scenario.yaml"
    periods = (cfg.get("profiles") or {}).get("periods")
    say("")
    say("-- Verdict --")
    say(f"  {name} is usable at its design point.")
    if periods and int(periods) > 96:
        say(f"  Next: discoolpy run {target} -n 48     (a short look first;"
            f" the file asks for {int(periods)})")
    else:
        say(f"  Next: discoolpy run {target}")
    say(f"        discoolpy report {target}   (the results, as figures)")
    say("")

    return CheckResult(
        name=name,
        ok=True,
        lines=lines,
        system=system,
        layout_path=drawn,
        warnings=structural,
    )
