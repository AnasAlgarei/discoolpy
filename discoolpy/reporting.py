"""Printed and plotted results for any DisCoolPy district cooling system.

:func:`~discoolpy.layout.plot_network` draws what a scenario *is*. This module
covers what it *did*: one pair of functions per component, plus a whole-system
pair, all of which take whatever you happen to be holding.

    from discoolpy import run_scenario, print_report, plot_all

    result = run_scenario("configs/config_riyadh_heat_gains.yaml")
    print_report(result)
    paths = plot_all(result, "outputs/figures")

Everything accepts the same ``source`` argument: a
:class:`~discoolpy.scenario.ScenarioResult`, a
:class:`~discoolpy.scenario.CheckResult`, a
:class:`~discoolpy.utils.DistrictCoolingSystem`, a results
:class:`~pandas.DataFrame`, or a path to a results CSV. A system carries the
design point and the component objects; a frame carries the time series. Pass
both, as ``source=system, results=frame``, and every report uses both.

What each function can say therefore depends on what it was given, and on what
the scenario actually contains. A scenario with no store produces no storage
figure, and says so rather than drawing an empty one. This is deliberate: these
functions are meant to be pointed at an arbitrary DisCoolPy network without
first being told what is in it.

Figures
-------
=========================  ===============================================
:func:`plot_pipes`         Distribution gain: per pipe, over time, vs soil
:func:`plot_buildings`     Delivered duty, envelope split, indoor temperature
:func:`plot_chiller`       Duty, power, COP, lift, duration curve
:func:`plot_cooling_tower` Rejection, condenser water, approach, closure
:func:`plot_storage`       Charge/discharge, state of charge, gain, thermocline
:func:`plot_balance`       Where the plant's cooling goes, and what it costs
:func:`plot_system`        One-page dashboard of the whole district
:func:`plot_all`           Every figure above, plus the layout, to a directory
=========================  ===============================================

Colours are fixed per quantity across every figure, so supply is the same blue
in the pipe figure and in the dashboard. The palette is a published
colourblind-safe categorical order, used in that order and not cycled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

__all__ = [
    "PALETTE",
    "ComponentReport",
    "SystemReport",
    "resolve_source",
    "report_system",
    "print_report",
    "report_pipes",
    "report_buildings",
    "report_chiller",
    "report_cooling_tower",
    "report_storage",
    "report_satellites",
    "report_balance",
    "compare_pipe_heat_models",
    "plot_pipes",
    "plot_buildings",
    "plot_chiller",
    "plot_cooling_tower",
    "plot_storage",
    "plot_balance",
    "plot_system",
    "plot_all",
    "FIGURES",
]


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

#: Categorical slots, in their published order. Assigned by entity below and
#: never cycled: a ninth series folds into "other" rather than repeating a hue.
PALETTE: Tuple[str, ...] = (
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
)

# One colour per quantity, held across every figure in the module. Chilled water
# is blue wherever it appears, heat rejected is red wherever it appears.
C = {
    "supply": PALETTE[0],
    "return": PALETTE[1],
    "demand": PALETTE[0],
    "pipe_gain": PALETTE[1],
    "pump": PALETTE[3],
    "storage": PALETTE[2],
    "storage_charge": PALETTE[0],
    "storage_discharge": PALETTE[1],
    "plant": PALETTE[6],
    "power": PALETTE[6],
    "cop": PALETTE[2],
    "ambient": PALETTE[7],
    "ground": PALETTE[6],
    "rejection": PALETTE[7],
    "satellite": PALETTE[4],
    "solar": PALETTE[3],
    "grid": "#d5d4cf",
    "ink": "#0b0b0b",
    "ink2": "#52514e",
    "muted": "#8a8a85",
    "surface": "#fcfcfb",
}

_SEQUENTIAL_BLUE = (
    "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b",
)

W_PER_KW = 1e3

#: Below this, a solved pipe duty is solver noise rather than a heat gain. The
#: same tolerance the chilled-water loop closure check uses.
ADIABATIC_TOLERANCE_W = 1.0


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

@dataclass
class _Source:
    """Whatever the caller had, sorted into the three things reporting needs."""

    name: str
    system: Any = None
    results: Optional[pd.DataFrame] = None
    cases: Dict[str, pd.DataFrame] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)
    report: Any = None

    @property
    def has_results(self) -> bool:
        return self.results is not None and not self.results.empty

    @property
    def dt_h(self) -> float:
        if not self.has_results or "resolution_hours" not in self.results:
            return 1.0
        value = float(self.results["resolution_hours"].iloc[0])
        return value if value > 0 else 1.0

    def col(self, name: str) -> Optional[pd.Series]:
        """One results column, or ``None`` when this run did not produce it."""
        if not self.has_results or name not in self.results.columns:
            return None
        series = pd.to_numeric(self.results[name], errors="coerce")
        return None if series.isna().all() else series

    def energy_kWh(self, name: str) -> Optional[float]:
        series = self.col(name)
        if series is None:
            return None
        return float(series.fillna(0.0).sum()) * self.dt_h / W_PER_KW

    @property
    def time(self) -> Sequence[Any]:
        """The x axis: timestamps where the frame has them, else snapshot index."""
        if not self.has_results:
            return []
        if "timestamp" in self.results.columns:
            stamps = pd.to_datetime(self.results["timestamp"], errors="coerce")
            if not stamps.isna().all():
                return stamps
        if "snapshot_index" in self.results.columns:
            return self.results["snapshot_index"]
        return range(len(self.results))


def resolve_source(source: Any, results: Any = None) -> _Source:
    """Normalise any of the accepted inputs into one internal record.

    Accepts a :class:`~discoolpy.scenario.ScenarioResult`, a
    :class:`~discoolpy.scenario.CheckResult`, a
    :class:`~discoolpy.utils.DistrictCoolingSystem`, a results frame, a path to
    a results CSV, or a ``(system, frame)`` pair. ``results`` supplies the time
    series separately when ``source`` is a system.

    Exposed because a study that builds its own reporting on top of this module
    should not have to repeat the type-sorting.
    """
    # Idempotent, so every public function can call it on its own argument and
    # still be cheap to chain: plot_all resolves once and hands the record down.
    if isinstance(source, _Source):
        if results is None:
            return source
        record = _Source(**{k: getattr(source, k) for k in
                            ("name", "system", "results", "cases", "config", "report")})
        record.results = (results if isinstance(results, pd.DataFrame)
                          else pd.read_csv(results))
        return record

    record = _Source(name="district cooling system")

    def take_results(value: Any) -> Optional[pd.DataFrame]:
        if value is None:
            return None
        if isinstance(value, pd.DataFrame):
            return value
        if isinstance(value, (str, Path)):
            return pd.read_csv(value)
        raise TypeError(
            f"Cannot read results from {type(value).__name__}. Pass a DataFrame, a path "
            "to a results CSV, or a ScenarioResult."
        )

    if isinstance(source, tuple) and len(source) == 2:
        source, results = source

    if hasattr(source, "cases") and hasattr(source, "profile"):        # ScenarioResult
        record.name = getattr(source, "name", record.name)
        record.cases = dict(getattr(source, "cases", {}) or {})
        record.config = dict(getattr(source, "config", {}) or {})
        record.report = getattr(source, "report", None)
        try:
            record.results = source.results
        except KeyError:
            record.results = None
    elif hasattr(source, "lines") and hasattr(source, "system"):        # CheckResult
        record.name = getattr(source, "name", record.name)
        record.system = source.system
    elif hasattr(source, "network") and hasattr(source, "branch"):      # the system
        record.system = source
    elif source is not None:
        record.results = take_results(source)

    if results is not None:
        supplied = take_results(results)
        if supplied is not None:
            record.results = supplied

    if record.system is None and hasattr(source, "system"):
        record.system = source.system
    if record.system is not None and not record.config:
        record.config = dict(getattr(record.system, "config", {}) or {})
    if record.name == "district cooling system" and record.system is not None:
        name = getattr(record.system, "name", None)
        if not name:
            root = getattr(record.system, "branch", None)
            name = None if root is None else getattr(root, "label", None)
        if name:
            record.name = str(name)
    return record


# ---------------------------------------------------------------------------
# Report containers
# ---------------------------------------------------------------------------

@dataclass
class ComponentReport:
    """One component's results as ordered label/value rows plus the raw numbers.

    ``rows`` is what gets printed; ``data`` is what a notebook or a test should
    read, because it holds floats rather than formatted strings.
    """

    title: str
    rows: List[Tuple[str, str]] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.rows

    def add(self, label: str, value: Any, unit: str = "", fmt: str = ".2f",
            key: Optional[str] = None) -> "ComponentReport":
        """Append one row, skipping it when the value is missing or not a number."""
        if value is None:
            return self
        if isinstance(value, float) and value != value:
            return self
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = f"{value:{fmt}}{(' ' + unit) if unit else ''}"
        else:
            text = f"{value}{(' ' + unit) if unit else ''}"
        self.rows.append((label, text))
        self.data[key or label] = value
        return self

    def to_text(self, width: int = 34) -> str:
        if self.empty and not self.notes:
            return ""
        lines = [f"-- {self.title} --"]
        lines += [f"  {label:<{width}} {value}" for label, value in self.rows]
        lines += [f"  * {note}" for note in self.notes]
        return "\n".join(lines)


@dataclass
class SystemReport:
    """Every component report for one run, in the order they are worth reading."""

    name: str
    sections: List[ComponentReport] = field(default_factory=list)

    def to_text(self) -> str:
        blocks = [f"{'=' * 72}\n{self.name}\n{'=' * 72}"]
        blocks += [text for text in (s.to_text() for s in self.sections) if text]
        return "\n\n".join(blocks)

    def to_dict(self) -> Dict[str, Dict[str, Any]]:
        return {section.title: section.data for section in self.sections}

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.to_text()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _series_stats(series: Optional[pd.Series]) -> Dict[str, float]:
    if series is None:
        return {}
    clean = series.dropna()
    if clean.empty:
        return {}
    return {"mean": float(clean.mean()), "max": float(clean.max()), "min": float(clean.min())}


def _building_labels_from_frame(frame: pd.DataFrame) -> List[str]:
    """Recover the building names a results frame was written with."""
    suffix = "_effective_Q_W"
    return [c[: -len(suffix)] for c in frame.columns if c.endswith(suffix)]


def _pipe_keys_from_frame(frame: pd.DataFrame) -> List[str]:
    """Recover the per-pipe duty columns, supply first then return."""
    keys = [c[len("pipe_"): -len("_Q_W")] for c in frame.columns
            if c.startswith("pipe_") and c.endswith("_Q_W")]
    aggregate = {"heat_gain", "supply_heat_gain", "return_heat_gain"}
    keys = [k for k in keys if k not in aggregate]
    return sorted(keys, key=lambda k: (0 if "supply" in k else 1, k))


def _satellite_names_from_frame(frame: pd.DataFrame) -> List[str]:
    suffix = "_Q_evap_W"
    return [
        c[len("satellite_"): -len(suffix)]
        for c in frame.columns
        if c.startswith("satellite_") and c.endswith(suffix)
        and c != "satellite_Q_evap_W"
    ]


def _share_of(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or abs(denominator) < 1e-9:
        return None
    return 100.0 * numerator / denominator


# ---------------------------------------------------------------------------
# Component reports
# ---------------------------------------------------------------------------

def report_pipes(source: Any, results: Any = None) -> ComponentReport:
    """Distribution pipe heat gain: conductances, duties and what they cost.

    From a solved system this reports each pipe's conductance and the
    temperature it lost across it at the design point. From a time series it
    reports the energy the gain added over the run and its share of metered
    demand, which is the figure a feasibility study argues over.
    """
    src = resolve_source(source, results)
    out = ComponentReport("Distribution pipes")

    system = src.system
    if system is not None:
        branch = getattr(system, "branch", None)
        gains = branch.heat_gain_report() if branch is not None else {}
        detail = gains.get("per_pipe", {})
        active = {k: v for k, v in detail.items() if v["model"] != "adiabatic"}
        out.add("pipes in network", len(detail), fmt="d", key="n_pipes")
        out.add("pipes with a heat model", len(active), fmt="d", key="n_pipes_conducting")
        if active:
            models = sorted({v["model"] for v in active.values()})
            out.add("heat models in use", ", ".join(models), key="models")
            out.add("network conductance", gains.get("network_UA_W_K"), "W/K",
                    ".1f", key="network_UA_W_K")
            out.add("design supply gain", gains.get("supply_heat_gain_W", 0.0) / W_PER_KW,
                    "kW", key="design_supply_gain_kW")
            out.add("design return gain", gains.get("return_heat_gain_W", 0.0) / W_PER_KW,
                    "kW", key="design_return_gain_kW")
            out.add("design total gain", gains.get("total_heat_gain_W", 0.0) / W_PER_KW,
                    "kW", key="design_total_gain_kW")
            lengths = [v["length_m"] for v in active.values() if v["length_m"]]
            if lengths:
                out.add("routed length", sum(lengths), "m", ".0f", key="length_m")
            warmest = max(
                (v for v in active.values()
                 if v["T_in_degC"] is not None and v["T_out_degC"] is not None),
                key=lambda v: v["T_out_degC"] - v["T_in_degC"],
                default=None,
            )
            if warmest is not None:
                rise = warmest["T_out_degC"] - warmest["T_in_degC"]
                out.add("worst single-pipe rise", rise, "K", ".3f", key="worst_rise_K")
            out.data["per_pipe"] = detail
        else:
            out.notes.append(
                "Every pipe is adiabatic. Distribution gain is not represented in this "
                "scenario, so plant duty will equal building demand plus pump heat."
            )

    if src.has_results:
        gain_kWh = src.energy_kWh("pipe_heat_gain_W")
        demand_kWh = src.energy_kWh("actual_building_total_Q_W")
        out.add("gain over the run", gain_kWh, "kWh", ".0f", key="gain_kWh")
        out.add("  supply mains", src.energy_kWh("pipe_supply_heat_gain_W"), "kWh",
                ".0f", key="supply_gain_kWh")
        out.add("  return mains", src.energy_kWh("pipe_return_heat_gain_W"), "kWh",
                ".0f", key="return_gain_kWh")
        out.add("share of building demand", _share_of(gain_kWh, demand_kWh), "%",
                ".2f", key="gain_share_pct")
        stats = _series_stats(src.col("pipe_heat_gain_W"))
        if stats:
            out.add("peak gain", stats["max"] / W_PER_KW, "kW", key="peak_gain_kW")
            out.add("mean gain", stats["mean"] / W_PER_KW, "kW", key="mean_gain_kW")
            swing = stats["max"] - stats["min"]
            out.add("swing over the run", swing / W_PER_KW, "kW", key="gain_swing_kW")
            if stats["mean"] > 0 and swing / stats["mean"] < 0.1:
                out.notes.append(
                    "Gain barely moves over the run, which is what a buried network driven "
                    "by soil temperature should do: the ground damps the daily air swing "
                    "almost entirely."
                )
    return out


def report_buildings(source: Any, results: Any = None) -> ComponentReport:
    """Delivered cooling per building, and the weather split behind it."""
    src = resolve_source(source, results)
    out = ComponentReport("Buildings")

    system = src.system
    if system is not None:
        buildings = list(getattr(system, "buildings", []) or [])
        out.add("buildings", len(buildings), fmt="d", key="n_buildings")
        design = [getattr(b, "Q_design_W", None) for b in buildings]
        design = [float(q) for q in design if q is not None]
        if design:
            out.add("design load", sum(design) / W_PER_KW, "kW", key="design_load_kW")
        with_envelope = [b for b in buildings if getattr(b, "envelope", None) is not None]
        if with_envelope:
            out.add("with an envelope model", len(with_envelope), fmt="d",
                    key="n_with_envelope")
        with_mass = [b for b in buildings if getattr(b, "thermal_mass", None) is not None]
        if with_mass:
            out.add("with thermal mass", len(with_mass), fmt="d", key="n_with_mass")

    if src.has_results:
        labels = _building_labels_from_frame(src.results)
        out.add("buildings in the results", len(labels), fmt="d", key="n_reported")
        total_kWh = src.energy_kWh("actual_building_total_Q_W")
        out.add("cooling delivered", total_kWh, "kWh", ".0f", key="delivered_kWh")
        stats = _series_stats(src.col("actual_building_total_Q_W"))
        if stats:
            out.add("peak district demand", stats["max"] / W_PER_KW, "kW", key="peak_kW")
            out.add("mean district demand", stats["mean"] / W_PER_KW, "kW", key="mean_kW")
            if stats["max"] > 0:
                out.add("demand load factor", stats["mean"] / stats["max"], "-", ".3f",
                        key="load_factor")
        per_building: Dict[str, float] = {}
        for label in labels:
            energy = src.energy_kWh(f"{label}_effective_Q_W")
            if energy is not None:
                per_building[label] = energy
        if per_building:
            out.data["per_building_kWh"] = per_building
            for label, energy in sorted(per_building.items(), key=lambda kv: -kv[1]):
                out.add(f"  {label.replace('_', ' ')}", energy, "kWh", ".0f",
                        key=f"{label}_kWh")
        # The split is only worth reporting when the weather drives part of the
        # load. Without an envelope model every joule lands in "internal", which
        # says nothing except that the building followed its profile.
        split = {}
        for part, pretty in (("conduction_W", "conduction"), ("solar_W", "solar"),
                             ("infiltration_W", "infiltration"), ("internal_W", "internal")):
            total = None
            for label in labels:
                value = src.energy_kWh(f"{label}_{part}")
                if value is not None:
                    total = value if total is None else total + value
            if total is not None:
                split[pretty] = total
        weather_driven = sum(abs(split.get(k, 0.0))
                             for k in ("conduction", "solar", "infiltration"))
        if weather_driven > 1e-6:
            for pretty, total in split.items():
                out.add(f"envelope: {pretty}", total, "kWh", ".0f",
                        key=f"envelope_{pretty}_kWh")
        elif split:
            out.notes.append(
                "No envelope model: these buildings follow a demand profile, so the load "
                "carries no weather sensitivity and pre-cooling has nothing to act on."
            )
        violations = [c for c in src.results.columns if c.endswith("_band_violation_K")]
        if violations:
            worst = max(
                float(pd.to_numeric(src.results[c], errors="coerce").abs().max() or 0.0)
                for c in violations
            )
            out.add("worst comfort band violation", worst, "K", ".3f",
                    key="worst_band_violation_K")
            if worst > 1e-6:
                out.notes.append(
                    "At least one building left its comfort band. Pre-cooling that violates "
                    "the band is not flexibility the occupants agreed to."
                )
    return out


def report_chiller(source: Any, results: Any = None) -> ComponentReport:
    """Central plant: duty, power, efficiency and the water it delivers."""
    src = resolve_source(source, results)
    out = ComponentReport("Central chiller")

    system = src.system
    if system is not None:
        chiller = getattr(system, "chiller", None)
        if chiller is not None:
            out.add("label", getattr(chiller, "label", "chiller"), key="label")
            out.add("refrigerant", getattr(chiller, "refrigerant", None), key="refrigerant")
            out.add("plant control", getattr(system, "plant_control", None),
                    key="plant_control")
            duty = getattr(chiller, "solved_Q_evap_W", None)
            if duty:
                out.add("design evaporator duty", float(duty) / W_PER_KW, "kW",
                        key="design_duty_kW")
            power = getattr(getattr(chiller, "compressor", None), "P", None)
            power = None if power is None else getattr(power, "val", None)
            if power and power == power:
                out.add("design compressor power", float(power) / W_PER_KW, "kW",
                        key="design_power_kW")
                if duty:
                    out.add("design COP", float(duty) / float(power), "-", ".3f",
                            key="design_cop")

    if src.has_results:
        out.add("cooling produced", src.energy_kWh("chiller_Q_evap_W"), "kWh", ".0f",
                key="cooling_kWh")
        out.add("compressor energy", src.energy_kWh("compressor_power_W"), "kWh", ".0f",
                key="compressor_kWh")
        stats = _series_stats(src.col("compressor_power_W"))
        if stats:
            out.add("peak compressor power", stats["max"] / W_PER_KW, "kW", key="peak_power_kW")
            out.add("mean compressor power", stats["mean"] / W_PER_KW, "kW",
                    key="mean_power_kW")
            if stats["max"] > 0:
                out.add("electric load factor", stats["mean"] / stats["max"], "-", ".3f",
                        key="electric_load_factor")
        cop = _series_stats(src.col("cop"))
        if cop:
            out.add("COP mean / min / max",
                    f"{cop['mean']:.3f} / {cop['min']:.3f} / {cop['max']:.3f}", key="cop")
            out.data["cop_mean"] = cop["mean"]
        fleet = _series_stats(src.col("fleet_cop"))
        if fleet:
            out.add("fleet COP mean", fleet["mean"], "-", ".3f", key="fleet_cop_mean")
        supply = _series_stats(src.col("chw_supply_T_degC"))
        if supply:
            out.add("CHW supply into the district",
                    f"{supply['mean']:.2f} degC mean, {supply['max']:.2f} peak",
                    key="chw_supply_degC")
        plant = _series_stats(src.col("chw_plant_supply_T_degC"))
        if plant and supply:
            out.add("plant leaving water", plant["mean"], "degC", key="plant_leaving_degC")
            if plant["min"] < supply["min"] - 0.05:
                out.notes.append(
                    f"Plant leaving water reaches {plant['min']:.2f} degC, below the "
                    f"{supply['min']:.2f} degC the district receives. That is the store "
                    "charging in series, and the COP penalty for it is already in these "
                    "numbers."
                )
        dt = _series_stats(src.col("chw_delta_T_K"))
        if dt:
            out.add("distribution delta-T", dt["mean"], "K", key="delta_T_K")
            if dt["min"] < 0.6 * dt["max"]:
                out.notes.append(
                    f"Delta-T falls to {dt['min']:.2f} K against a best of {dt['max']:.2f} K. "
                    "A collapsing delta-T means the network is moving water rather than "
                    "cooling, and pumping energy rises with it."
                )
    return out


def report_cooling_tower(source: Any, results: Any = None) -> ComponentReport:
    """Condenser-side heat rejection, and whether it closes against the plant."""
    src = resolve_source(source, results)
    out = ComponentReport("Cooling tower")

    system = src.system
    tower = getattr(system, "cooling_tower", None) if system is not None else None
    if tower is not None:
        out.add("label", getattr(tower, "label", "cooling tower"), key="label")
        rejection = getattr(tower, "heat_rejection", None)
        if rejection is not None and rejection == rejection:
            out.add("design heat rejection", float(rejection) / W_PER_KW, "kW",
                    key="design_rejection_kW")
        approach = getattr(tower, "approach_temperature_K", None)
        if approach is not None:
            out.add("approach to ambient", approach, "K", key="approach_K")

    if src.has_results:
        out.add("heat rejected", src.energy_kWh("heat_rejection_W"), "kWh", ".0f",
                key="rejected_kWh")
        stats = _series_stats(src.col("heat_rejection_W"))
        if stats:
            out.add("peak heat rejection", stats["max"] / W_PER_KW, "kW",
                    key="peak_rejection_kW")
        cw_in = _series_stats(src.col("cw_in_T_degC"))
        cw_out = _series_stats(src.col("cw_out_T_degC"))
        if cw_in and cw_out:
            out.add("condenser water in / out",
                    f"{cw_in['mean']:.2f} / {cw_out['mean']:.2f} degC mean",
                    key="cw_temperatures_degC")
            out.add("condenser range", cw_out["mean"] - cw_in["mean"], "K",
                    key="cw_range_K")
        flow = _series_stats(src.col("cw_m_kg_s"))
        if flow:
            out.add("condenser water flow", flow["mean"], "kg/s", key="cw_flow_kg_s")
        # The tower must carry the evaporator duty plus the work put in. If it
        # does not, the condenser loop is not closing and nothing downstream of
        # it means anything.
        rejected = src.col("heat_rejection_W")
        duty = src.col("chiller_Q_evap_W")
        power = src.col("compressor_power_W")
        if rejected is not None and duty is not None and power is not None:
            residual = (rejected - duty - power).abs().max()
            out.add("worst closure residual", float(residual), "W", ".3f",
                    key="closure_residual_W")
            if float(residual) > 1.0:
                out.notes.append(
                    "Rejected heat does not match evaporator duty plus compressor work. "
                    "The condenser loop is not closing; treat the efficiency numbers as "
                    "unreliable until it does."
                )
    return out


def report_storage(source: Any, results: Any = None) -> ComponentReport:
    """Cold store: what it shifted, what it lost, and what the shift cost."""
    src = resolve_source(source, results)
    out = ComponentReport("Cold storage")

    system = src.system
    store = getattr(system, "storage", None) if system is not None else None
    if store is not None:
        out.add("label", getattr(store, "label", "storage"), key="label")
        out.add("type", getattr(store, "storage_type", None), key="storage_type")
        out.add("coupling", getattr(store, "coupling", None), key="coupling")
        out.add("loss model", getattr(store, "loss_model", None), key="loss_model")
        out.add("capacity", getattr(store, "capacity_kWh", None), "kWh", ".0f",
                key="capacity_kWh")
        out.add("stratified", "yes" if getattr(store, "is_stratified", False) else "no",
                key="stratified")
        thermal = getattr(store, "thermal", None)
        if thermal is not None and getattr(thermal, "UA_W_K", 0.0):
            out.add("tank conductance", thermal.UA_W_K, "W/K", ".1f", key="tank_UA_W_K")

    if src.has_results and "storage_mode" in src.results.columns:
        modes = src.results["storage_mode"].astype(str)
        if not (modes == "none").all():
            charge = src.energy_kWh("storage_charge_power_W")
            discharge = src.energy_kWh("storage_discharge_power_W")
            out.add("charged", charge, "kWh", ".0f", key="charged_kWh")
            out.add("discharged", discharge, "kWh", ".0f", key="discharged_kWh")
            if charge and discharge:
                out.add("thermal round trip", discharge / charge, "-", ".3f",
                        key="thermal_round_trip")
                soc_series = src.col("storage_soc_after")
                if soc_series is not None and len(soc_series.dropna()) > 1:
                    drift = float(soc_series.dropna().iloc[-1] - soc_series.dropna().iloc[0])
                    if abs(drift) > 0.02:
                        out.notes.append(
                            f"The store ends the run {drift:+.2f} in state of charge from "
                            "where it started, so the round trip above is not a cyclic "
                            "figure. Run a whole number of charge cycles before quoting it."
                        )
            out.add("ambient gain into the tank",
                    src.energy_kWh("storage_ambient_heat_gain_W"), "kWh", ".1f",
                    key="ambient_gain_kWh")
            demand_kWh = src.energy_kWh("actual_building_total_Q_W")
            out.add("  share of building demand",
                    _share_of(src.energy_kWh("storage_ambient_heat_gain_W"), demand_kWh),
                    "%", ".2f", key="ambient_gain_share_pct")
            soc = _series_stats(src.col("storage_soc_after"))
            if soc:
                out.add("state of charge min / max",
                        f"{soc['min']:.3f} / {soc['max']:.3f}", key="soc_range")
            curtailed = src.energy_kWh("storage_curtailed_request_W")
            if curtailed:
                out.add("curtailed request", curtailed, "kWh", ".1f", key="curtailed_kWh")
                out.notes.append(
                    "Part of the dispatch request could not be met. The store hit a power "
                    "or state-of-charge limit, so the strategy is larger than the asset."
                )
            gain = src.col("storage_ambient_heat_gain_W")
            ambient = src.col("ambient_temperature_degC")
            if gain is not None and ambient is not None and gain.max() > 0:
                hot = gain[ambient >= ambient.quantile(0.75)].mean()
                cool = gain[ambient <= ambient.quantile(0.25)].mean()
                if cool and cool > 0:
                    out.add("gain, hot quartile / cool quartile", hot / cool, "x", ".2f",
                            key="gain_hot_cool_ratio")
                    out.notes.append(
                        "Tank gain is not a flat daily fraction: it peaks with ambient, "
                        "which is the hour the stored cooling is worth most."
                    )
        else:
            out.notes.append(
                "No central store was dispatched in this run. A store at a satellite "
                "plant, if the scenario has one, is reported under that plant."
            )

    if src.report is not None:
        flex = src.report
        out.add("peak power reduction", getattr(flex, "peak_power_reduction_pct", None),
                "%", ".2f", key="peak_reduction_pct")
        out.add("energy penalty", getattr(flex, "energy_penalty_pct", None), "%", ".2f",
                key="energy_penalty_pct")
        out.add("electric round trip", getattr(flex, "electric_round_trip", None), "-",
                ".3f", key="electric_round_trip")
    return out


def report_satellites(source: Any, results: Any = None) -> ComponentReport:
    """Distributed plants: what each carried, and what the fleet paid for it."""
    src = resolve_source(source, results)
    out = ComponentReport("Satellite plants")

    system = src.system
    plants = list(getattr(system, "satellite_plants", []) or []) if system is not None else []
    if system is not None:
        out.data["n_plants"] = len(plants)
        if plants:
            out.add("distributed plants", len(plants), fmt="d", key="n_plants")
        for plant in plants:
            design = getattr(plant, "design_Q_evap_W", None)
            if design:
                extra = " + store" if getattr(plant, "storage", None) is not None else ""
                out.add(f"  {plant.label} design duty",
                        f"{float(design) / W_PER_KW:.1f} kW{extra}",
                        key=f"{plant.label}_design_kW")

    if src.has_results:
        names = _satellite_names_from_frame(src.results)
        duty = src.col("satellite_Q_evap_W")
        if names or (duty is not None and duty.abs().max() > 0):
            out.add("satellite cooling produced", src.energy_kWh("satellite_Q_evap_W"),
                    "kWh", ".0f", key="satellite_cooling_kWh")
            out.add("satellite compressor energy",
                    src.energy_kWh("satellite_compressor_power_W"), "kWh", ".0f",
                    key="satellite_power_kWh")
            for name in names:
                out.add(f"  {name.replace('_', ' ')}",
                        src.energy_kWh(f"satellite_{name}_Q_evap_W"), "kWh", ".0f",
                        key=f"{name}_kWh")
            central = _series_stats(src.col("compressor_power_W"))
            fleet = _series_stats(src.col("total_compressor_power_W"))
            if central and fleet:
                out.add("peak central / fleet power",
                        f"{central['max'] / W_PER_KW:.1f} / {fleet['max'] / W_PER_KW:.1f} kW",
                        key="peak_central_fleet_kW")
                out.notes.append(
                    "A satellite plant moves duty; it does not create it. The gap between "
                    "the central peak and the fleet peak is the whole of what it bought."
                )
    if not out.rows:
        out.notes.append("This network has no distributed plants.")
    return out


def report_balance(source: Any, results: Any = None) -> ComponentReport:
    """Where the plant's cooling went, and whether the loop closes.

    The headline of a DisCoolPy run: production runs above metered demand by
    exactly the parasitic terms, and the residual says whether to believe it.
    """
    src = resolve_source(source, results)
    out = ComponentReport("Plant energy balance")

    if src.has_results:
        demand = src.energy_kWh("actual_building_total_Q_W")
        out.add("building demand served", demand, "kWh", ".0f", key="demand_kWh")
        contributions = [
            ("distribution pipe gain", "pipe_heat_gain_W", "pipe_gain_kWh"),
            ("pump heat", "pump_power_W", "pump_kWh"),
            ("tank ambient gain", "storage_ambient_heat_gain_W", "tank_gain_kWh"),
        ]
        for label, column, key in contributions:
            value = src.energy_kWh(column)
            if value:
                out.add(label, value, "kWh", ".0f", key=key)
                out.add("  share of demand", _share_of(value, demand), "%", ".2f",
                        key=f"{key}_share_pct")
        produced = src.energy_kWh("total_cooling_produced_W")
        if produced is None:
            produced = src.energy_kWh("chiller_Q_evap_W")
        out.add("plant cooling produced", produced, "kWh", ".0f", key="produced_kWh")
        out.add("  as a share of demand", _share_of(produced, demand), "%", ".2f",
                key="produced_share_pct")
        power = src.energy_kWh("total_compressor_power_W")
        if power is None:
            power = src.energy_kWh("compressor_power_W")
        out.add("compressor electricity", power, "kWh", ".0f", key="electricity_kWh")
        pump = src.energy_kWh("pump_power_W")
        if power and pump:
            out.add("total electricity", power + pump, "kWh", ".0f", key="total_electricity_kWh")
        residual = src.col("chw_energy_residual_W")
        if residual is not None:
            worst = float(residual.abs().max())
            out.add("worst closure residual", worst, "W", ".4f", key="residual_W")
            if worst > 1.0:
                out.notes.append(
                    "The chilled-water loop does not close to within 1 W. Something is "
                    "adding or removing heat that is not being reported; the shares above "
                    "do not add up and should not be quoted."
                )
        share = _share_of(produced, demand)
        if share is not None and share > 100.5:
            out.notes.append(
                f"The plant produces {share - 100:.1f}% more cooling than the buildings "
                "consume. That excess is the network, and a model that treats pipes as "
                "adiabatic and the store as lossless reports none of it."
            )
    return out


def report_system(source: Any, results: Any = None) -> SystemReport:
    """Every component report for one run, in reading order."""
    src = resolve_source(source, results)
    sections = [
        report_balance(src),
        report_chiller(src),
        report_pipes(src),
        report_buildings(src),
        report_cooling_tower(src),
        report_storage(src),
        report_satellites(src),
    ]
    return SystemReport(name=src.name, sections=[s for s in sections if s.rows or s.notes])


def print_report(source: Any, results: Any = None) -> SystemReport:
    """Print :func:`report_system` and return it."""
    report = report_system(source, results)
    print(report.to_text())
    return report


# ---------------------------------------------------------------------------
# Cross-check: closed-form conductance against TESPy's own pipe groups
# ---------------------------------------------------------------------------

def compare_pipe_heat_models(
    inner_diameter_m: float,
    length_m: float,
    insulation_thickness_m: float = 0.05,
    insulation_conductivity: Any = "pur",
    placement: str = "buried",
    burial_depth_m: float = 1.2,
    ground: str = "moist soil",
    pipe_wall_thickness_m: float = 0.006,
    twin_spacing_m: Optional[float] = None,
    wind_velocity_m_s: float = 1.0,
    ambient_temperature_degC: float = 29.4,
    inlet_temperature_degC: float = 7.0,
    mass_flow_kg_s: float = 30.0,
) -> Dict[str, Any]:
    """Solve one pipe both ways and return the two conductances side by side.

    DisCoolPy derives a pipe's ``UA`` in closed form and hands it to TESPy's
    ``UA_group``; TESPy can instead derive the conductance itself from the same
    geometry. Both then solve the same ``Q = UA * dT_log`` equation, so the
    only thing that can differ is the conductance, and this puts a number on
    that rather than leaving it to be argued.

    The comparison is run by building a three-component TESPy network and
    solving it twice, so the native figure is TESPy's own, not a
    reimplementation of it. Returns the two per-metre conductances, their
    ratio, and the duty each produces. ``twin_spacing_m`` is reported
    separately because TESPy's buried group is a single-pipe model and has no
    equivalent term.
    """
    from tespy.components import Pipe, Sink, Source
    from tespy.connections import Connection
    from tespy.networks import Network

    from .thermal import (
        TESPY_MIN_INSULATION_THICKNESS_M,
        TESPY_NATIVE_GROUND_MEDIA,
        _resolve_conductivity,
        INSULATION_CONDUCTIVITY_W_mK,
        buried_pipe_UA_per_m,
        surface_pipe_UA_per_m,
    )

    buried = str(placement).lower() == "buried"
    k_ins = _resolve_conductivity(
        insulation_conductivity, INSULATION_CONDUCTIVITY_W_mK, "insulation"
    )

    def solve(**attrs: Any) -> Tuple[float, float, float]:
        nw = Network(iterinfo=False)
        nw.units.set_defaults(
            temperature="degC", power="W", pressure="bar",
            pressure_difference="bar", enthalpy="kJ/kg",
        )
        src_, pipe, snk = Source("src"), Pipe("pipe"), Sink("snk")
        c_in = Connection(src_, "out1", pipe, "in1", label="in")
        c_out = Connection(pipe, "out1", snk, "in1", label="out")
        nw.add_conns(c_in, c_out)
        c_in.set_attr(fluid={"water": 1}, m=mass_flow_kg_s,
                      T=inlet_temperature_degC, p=6.0)
        pipe.set_attr(pr=1.0, Tamb=ambient_temperature_degC, **attrs)
        nw.solve("design")
        if not nw.converged:
            raise RuntimeError("the cross-check network did not converge")
        return float(pipe.Q.val), float(pipe.UA.val), float(c_out.T.val)

    closed_form_per_m = (
        buried_pipe_UA_per_m(
            inner_diameter_m, insulation_thickness_m, insulation_conductivity,
            burial_depth_m, ground, pipe_wall_thickness_m=pipe_wall_thickness_m,
            twin_spacing_m=twin_spacing_m,
        )
        if buried
        else surface_pipe_UA_per_m(
            inner_diameter_m, insulation_thickness_m, insulation_conductivity,
            pipe_wall_thickness_m=pipe_wall_thickness_m,
            wind_velocity_m_s=wind_velocity_m_s,
            ambient_temperature_degC=ambient_temperature_degC,
        )
    )
    q_closed, _, t_closed = solve(UA=closed_form_per_m * length_m)

    out: Dict[str, Any] = {
        "placement": "buried" if buried else "surface",
        "closed_form_UA_per_m_W_mK": closed_form_per_m,
        "closed_form_Q_W": q_closed,
        "closed_form_outlet_degC": t_closed,
        "twin_spacing_m": twin_spacing_m,
    }

    reason = None
    if insulation_thickness_m < TESPY_MIN_INSULATION_THICKNESS_M:
        reason = (
            f"TESPy's native groups need at least {TESPY_MIN_INSULATION_THICKNESS_M} m "
            "of insulation; this pipe has less, so only the closed form can model it."
        )
    elif buried and str(ground).lower() not in TESPY_NATIVE_GROUND_MEDIA:
        reason = (
            f"TESPy's buried group knows only {list(TESPY_NATIVE_GROUND_MEDIA)}, and this "
            f"pipe sits in {ground!r}."
        )
    elif not buried and wind_velocity_m_s <= 0:
        reason = (
            "TESPy's surface group uses a forced-convection correlation and cannot take "
            "still air."
        )

    if reason is not None:
        out["native_available"] = False
        out["reason"] = reason
        return out

    native_attrs = {
        "D": inner_diameter_m,
        "L": length_m,
        "insulation_thickness": insulation_thickness_m,
        "insulation_tc": k_ins,
        "pipe_thickness": pipe_wall_thickness_m,
        "material": "Steel",
    }
    if buried:
        native_attrs["pipe_depth"] = burial_depth_m
        native_attrs["environment_media"] = str(ground).lower()
    else:
        native_attrs["wind_velocity"] = wind_velocity_m_s
        native_attrs["environment_media"] = "air"

    q_native, ua_native, t_native = solve(**native_attrs)
    out.update({
        "native_available": True,
        "native_UA_per_m_W_mK": ua_native / length_m,
        "native_Q_W": q_native,
        "native_outlet_degC": t_native,
        "ratio": closed_form_per_m / (ua_native / length_m),
        "Q_difference_W": q_closed - q_native,
    })
    return out


# ---------------------------------------------------------------------------
# Plotting: shared scaffolding
# ---------------------------------------------------------------------------

def _figure(nrows: int, ncols: int, title: str, axes: Any = None,
            height: float = 2.7, width: float = 5.4) -> Tuple[Any, Any]:
    """Create (or adopt) a grid of axes with the module's house style."""
    import matplotlib.pyplot as plt

    if axes is not None:
        flat = list(axes.flat) if hasattr(axes, "flat") else list(axes)
        return flat[0].figure, flat
    fig, grid = plt.subplots(nrows, ncols, figsize=(width * ncols, height * nrows))
    flat = list(grid.flat) if hasattr(grid, "flat") else [grid]
    fig.suptitle(title, fontsize=11.5, fontweight="bold", color=C["ink"], y=0.995)
    return fig, flat


def _style(ax: Any, title: str = "", xlabel: str = "", ylabel: str = "",
           legend: bool = False) -> Any:
    """Recessive axes, a light grid, and the labels every panel needs."""
    ax.set_facecolor(C["surface"])
    ax.grid(True, linestyle=":", linewidth=0.6, color=C["grid"], alpha=0.9)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(C["grid"])
    ax.tick_params(colors=C["ink2"], labelsize=8, length=3)
    if title:
        ax.set_title(title, fontsize=9.5, color=C["ink"], pad=6)
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=8.5, color=C["ink2"])
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=8.5, color=C["ink2"])
    # One series needs no legend box: the panel title already names it. Two or
    # more always get one, so identity is never carried by colour alone.
    if legend and len(ax.get_legend_handles_labels()[0]) > 1:
        ax.legend(fontsize=7.5, framealpha=0.92, edgecolor=C["grid"], loc="best")
    return ax


def _headroom(ax: Any, fraction: float = 0.3) -> None:
    """Leave space above a filled panel so its legend does not sit on the data."""
    low, high = ax.get_ylim()
    ax.set_ylim(low, high + fraction * (high - low))


def _blank(ax: Any, message: str) -> Any:
    """Say why a panel is empty instead of drawing an empty panel."""
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=8.5,
            color=C["muted"], transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.set_facecolor(C["surface"])
    return ax


def _time_axis(ax: Any, src: _Source) -> None:
    """Label the x axis for whatever the frame gave us to plot against."""
    if src.has_results and "timestamp" in src.results.columns:
        for label in ax.get_xticklabels():
            label.set_rotation(20)
            label.set_horizontalalignment("right")
    else:
        ax.set_xlabel("snapshot", fontsize=8.5, color=C["ink2"])


def _line(ax: Any, x: Any, y: Any, color: str, label: str, width: float = 1.6,
          style: str = "-", alpha: float = 1.0) -> None:
    ax.plot(x, y, color=color, linewidth=width, linestyle=style, label=label,
            alpha=alpha, solid_capstyle="round")


def _finish(fig: Any, save_path: Optional[str], dpi: int) -> Any:
    fig.tight_layout(rect=(0, 0, 1, 0.975))
    if save_path:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(save_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    return fig


def _duration_curve(series: pd.Series) -> pd.Series:
    """Values sorted high to low, the form that answers 'for how many hours'."""
    return series.dropna().sort_values(ascending=False).reset_index(drop=True)


def _comfort_band(src: _Source) -> Optional[Tuple[float, float]]:
    """The comfort band a scenario declared, if any, as (low, high) in degC."""
    system = src.system
    if system is not None:
        for building in getattr(system, "buildings", []) or []:
            mass = getattr(building, "thermal_mass", None)
            if mass is None:
                continue
            low = getattr(mass, "min_temperature_degC", None)
            high = getattr(mass, "max_temperature_degC", None)
            if low is not None and high is not None:
                return float(low), float(high)
    return None


# ---------------------------------------------------------------------------
# Plotting: per component
# ---------------------------------------------------------------------------

def plot_pipes(source: Any, results: Any = None, title: Optional[str] = None,
               axes: Any = None, save_path: Optional[str] = None,
               dpi: int = 160) -> Any:
    """Distribution heat gain, four ways: per pipe, over time, and against soil.

    The claim DisCoolPy makes about pipes is that the gain is solved rather
    than assumed, and that it therefore moves with the water temperature the
    solver found. These panels are where that is either visible or not.
    """
    import matplotlib.patches as mpatches

    src = resolve_source(source, results)
    fig, ax = _figure(2, 2, title or f"{src.name}: distribution pipes", axes)

    # (a) Per-pipe conductance and duty at the design point.
    detail: Dict[str, Dict[str, Any]] = {}
    if src.system is not None and getattr(src.system, "branch", None) is not None:
        detail = src.system.branch.heat_gain_report().get("per_pipe", {})
    active = {k: v for k, v in detail.items() if v["model"] != "adiabatic"}
    if active:
        keys = sorted(active, key=lambda k: (0 if active[k]["side"] == "supply" else 1, k))
        values = [active[k]["Q_W"] / W_PER_KW for k in keys]
        colors = [C["supply"] if active[k]["side"] == "supply" else C["return"]
                  for k in keys]
        bars = ax[0].barh(range(len(keys)), values, color=colors, height=0.68)
        ax[0].set_yticks(range(len(keys)))
        ax[0].set_yticklabels([k.replace("_", " ") for k in keys], fontsize=7.5)
        ax[0].invert_yaxis()
        span = max(values) if values else 1.0
        for bar, key in zip(bars, keys):
            ua = active[key]["UA_W_K"]
            if ua is None:
                continue
            ax[0].annotate(f"UA {ua:.0f} W/K",
                           (bar.get_width() + 0.02 * span,
                            bar.get_y() + bar.get_height() / 2),
                           va="center", fontsize=7, color=C["ink2"])
        ax[0].set_xlim(0, span * 1.45)
        ax[0].legend(handles=[mpatches.Patch(color=C["supply"], label="supply"),
                              mpatches.Patch(color=C["return"], label="return")],
                     fontsize=7.5, framealpha=0.92, edgecolor=C["grid"],
                     loc="lower right")
        _style(ax[0], "Design-point gain per pipe", "heat gain (kW)")
    elif detail:
        _blank(ax[0], "Every pipe in this network is adiabatic.\n"
                      "Set branch.heat_model to represent distribution gain.")
    else:
        _blank(ax[0], "No solved system supplied. Pass the\n"
                      "DistrictCoolingSystem to see conductances.")

    # (b) Gain over the run, split by side. An adiabatic network's solved pipe
    #     duties are floating-point dust around zero; plotted on their own axis
    #     they look like a signal, so say what they are instead.
    total = src.col("pipe_heat_gain_W")
    t = src.time
    if total is not None and float(total.abs().max()) < ADIABATIC_TOLERANCE_W:
        _blank(ax[1], "Pipe duties are zero to within a watt over\n"
                      "the whole run: this network is adiabatic.")
        _blank(ax[2], "No energy gained, so there is nothing to\n"
                      "break down per pipe.")
        _blank(ax[3], "Water arrives at the temperature it left:\n"
                      "an adiabatic network has no profile to draw.")
        return _finish(fig, save_path, dpi)
    if total is not None:
        supply = src.col("pipe_supply_heat_gain_W")
        ret = src.col("pipe_return_heat_gain_W")
        if supply is not None and ret is not None:
            ax[1].stackplot(t, supply / W_PER_KW, ret / W_PER_KW,
                            colors=[C["supply"], C["return"]],
                            labels=["supply mains", "return mains"],
                            edgecolor="white", linewidth=0.6)
        else:
            _line(ax[1], t, total / W_PER_KW, C["pipe_gain"], "total gain")
        _headroom(ax[1])
        _style(ax[1], "Gain over the run", ylabel="heat gain (kW)", legend=True)
        _time_axis(ax[1], src)
    else:
        _blank(ax[1], "No time series supplied.")

    # (c) Energy per pipe over the whole run.
    keys = _pipe_keys_from_frame(src.results) if src.has_results else []
    energies = {k: src.energy_kWh(f"pipe_{k}_Q_W") for k in keys}
    energies = {k: v for k, v in energies.items() if v is not None}
    if energies:
        names = list(energies)
        ax[2].bar(range(len(names)), [energies[k] for k in names],
                  color=[C["supply"] if "supply" in k else C["return"] for k in names],
                  width=0.7)
        ax[2].set_xticks(range(len(names)))
        ax[2].set_xticklabels([k.replace("_", " ") for k in names], rotation=35,
                              ha="right", fontsize=7)
        _style(ax[2], "Energy gained per pipe", ylabel="energy (kWh)")
    else:
        _blank(ax[2], "No per-pipe duties in the results frame.")

    # (d) The temperature profile along the index run. This is the panel the
    #     whole heat-gain model exists to produce: water leaves the plant at
    #     setpoint and arrives warmer, and the further out a building sits the
    #     less of the design delta-T is left for it.
    profile = _route_temperature_profile(active)
    if profile is not None:
        distance_s, supply_T, distance_r, return_T = profile
        _line(ax[3], distance_s, supply_T, C["supply"], "supply", width=1.8)
        ax[3].scatter(distance_s, supply_T, s=18, color=C["supply"], zorder=3,
                      edgecolor="white", linewidth=0.8)
        _line(ax[3], distance_r, return_T, C["return"], "return", width=1.8)
        ax[3].scatter(distance_r, return_T, s=18, color=C["return"], zorder=3,
                      edgecolor="white", linewidth=0.8)
        rise = supply_T[-1] - supply_T[0]
        ax[3].annotate(
            f"supply warms {rise:+.2f} K over {distance_s[-1]:.0f} m",
            (0.03, 0.52), xycoords="axes fraction", ha="left", va="center",
            fontsize=7.5, color=C["ink2"])
        _style(ax[3], "Temperature along the index run",
               "distance from the plant (m)", "water temperature (degC)",
               legend=True)
    else:
        # No routed geometry to lay it out along: fall back to the gain against
        # whatever drives it.
        ground = src.col("ground_temperature_degC")
        ambient = src.col("ambient_temperature_degC")
        driver = ground if ground is not None else ambient
        if total is not None and driver is not None:
            name = "soil temperature" if ground is not None else "air temperature"
            ax[3].scatter(driver, total / W_PER_KW, s=14,
                          color=C["ground"] if ground is not None else C["ambient"],
                          alpha=0.65, edgecolor="none")
            _style(ax[3], f"Gain against {name}", f"{name} (degC)", "heat gain (kW)")
            spread = float(driver.max() - driver.min())
            if spread < 0.5:
                ax[3].annotate(
                    f"the driving temperature moves only {spread:.2f} K over the run",
                    (0.03, 0.94), xycoords="axes fraction", ha="left", va="top",
                    fontsize=7.5, color=C["ink2"])
        else:
            _blank(ax[3], "No routed pipe geometry and no driving\n"
                          "temperature: nothing to lay a profile along.")

    return _finish(fig, save_path, dpi)


def _route_temperature_profile(active: Mapping[str, Mapping[str, Any]]):
    """Lay the root branch's solved pipe temperatures out along its route.

    Returns ``(supply_distance, supply_T, return_distance, return_T)`` in metres
    and degC, or ``None`` when the root branch has no lengths to lay them along.
    Only the root branch is used: once a network forks there is no single index
    run to plot against, and drawing one anyway would invent a route.
    """
    root = {k: v for k, v in active.items() if "/" not in k}
    supply = sorted((k for k in root if k.startswith("supply")),
                    key=lambda k: int(k.rpartition("_")[2]))
    returns = sorted((k for k in root if k.startswith("return")),
                     key=lambda k: int(k.rpartition("_")[2]))
    if not supply:
        return None
    if any(not root[k].get("length_m") for k in supply):
        return None
    if any(root[k].get("T_in_degC") is None or root[k].get("T_out_degC") is None
           for k in supply):
        return None

    distance_s: List[float] = []
    supply_T: List[float] = []
    cumulative = 0.0
    for key in supply:
        distance_s.append(cumulative)
        supply_T.append(float(root[key]["T_in_degC"]))
        cumulative += float(root[key]["length_m"])
        distance_s.append(cumulative)
        supply_T.append(float(root[key]["T_out_degC"]))

    # return_i shares supply_i's trench, so it sits at the same distance from
    # the plant, traversed the other way.
    distance_r: List[float] = []
    return_T: List[float] = []
    edges = [0.0]
    for key in supply:
        edges.append(edges[-1] + float(root[key]["length_m"]))
    for key in reversed(returns):
        index = int(key.rpartition("_")[2]) - 1
        if index + 1 >= len(edges):
            continue
        detail = root[key]
        if detail.get("T_in_degC") is None or detail.get("T_out_degC") is None:
            continue
        distance_r.append(edges[index + 1])
        return_T.append(float(detail["T_in_degC"]))
        distance_r.append(edges[index])
        return_T.append(float(detail["T_out_degC"]))
    if not distance_r:
        return None
    return distance_s, supply_T, distance_r, return_T


def plot_buildings(source: Any, results: Any = None, title: Optional[str] = None,
                   axes: Any = None, save_path: Optional[str] = None,
                   dpi: int = 160, max_series: int = 8) -> Any:
    """Delivered cooling per building, the envelope split, and indoor conditions."""
    src = resolve_source(source, results)
    fig, ax = _figure(2, 2, title or f"{src.name}: buildings", axes)
    labels = _building_labels_from_frame(src.results) if src.has_results else []
    t = src.time

    # (a) Delivered duty per building. Past the palette, fold the rest into one
    #     "other" series rather than repeating a hue.
    if labels:
        energies = {lab: (src.energy_kWh(f"{lab}_effective_Q_W") or 0.0) for lab in labels}
        ordered = sorted(labels, key=lambda lab: -energies[lab])
        shown, folded = ordered[:max_series], ordered[max_series:]
        for i, lab in enumerate(shown):
            series = src.col(f"{lab}_effective_Q_W")
            if series is not None:
                _line(ax[0], t, series / W_PER_KW, PALETTE[i % len(PALETTE)],
                      lab.replace("_", " "), width=1.4)
        if folded:
            rest = None
            for lab in folded:
                series = src.col(f"{lab}_effective_Q_W")
                if series is not None:
                    rest = series.fillna(0.0) if rest is None else rest + series.fillna(0.0)
            if rest is not None:
                _line(ax[0], t, rest / W_PER_KW, C["muted"],
                      f"other ({len(folded)})", width=1.4)
        _headroom(ax[0], 0.22)
        _style(ax[0], "Cooling delivered per building", ylabel="duty (kW)", legend=True)
        _time_axis(ax[0], src)
    else:
        _blank(ax[0], "No per-building duties in the results frame.")

    # (b) District envelope split. The point of the split is that a single
    #     scalar duty leaves the ambient driving force nowhere to act.
    parts = [("conduction_W", "conduction", PALETTE[0]),
             ("solar_W", "solar", PALETTE[3]),
             ("infiltration_W", "infiltration", PALETTE[2]),
             ("internal_W", "internal", PALETTE[4])]
    stacked, names, colors = [], [], []
    for column, pretty, color in parts:
        total = None
        for lab in labels:
            series = src.col(f"{lab}_{column}")
            if series is not None:
                total = series.fillna(0.0) if total is None else total + series.fillna(0.0)
        if total is not None:
            stacked.append(total.clip(lower=0.0) / W_PER_KW)
            names.append(pretty)
            colors.append(color)
    # Without an envelope model every joule lands in "internal", and a stack
    # with one band in it says only that the buildings followed their profile.
    weather_driven = any(
        name in ("conduction", "solar", "infiltration") and float(series.abs().max()) > 0
        for name, series in zip(names, stacked)
    )
    if stacked and weather_driven:
        ax[1].stackplot(t, *stacked, colors=colors, labels=names,
                        edgecolor="white", linewidth=0.6)
        _headroom(ax[1])
        _style(ax[1], "District envelope gain, by path", ylabel="gain (kW)", legend=True)
        _time_axis(ax[1], src)
    else:
        _blank(ax[1], "No envelope model in this scenario. These buildings\n"
                      "follow a demand profile, so the load carries no\n"
                      "weather sensitivity.")

    # (c) Indoor temperature against the comfort band, where there is one.
    indoor = [lab for lab in labels if src.col(f"{lab}_T_indoor_degC") is not None]
    if indoor:
        for i, lab in enumerate(indoor[:max_series]):
            _line(ax[2], t, src.col(f"{lab}_T_indoor_degC"),
                  PALETTE[i % len(PALETTE)], lab.replace("_", " "), width=1.4)
        band = _comfort_band(src)
        if band is not None:
            low, high = band
            ax[2].axhspan(low, high, color=C["storage"], alpha=0.12, zorder=0)
            ax[2].annotate("comfort band", (0.015, (low + high) / 2),
                           xycoords=("axes fraction", "data"), fontsize=7.5,
                           color=C["ink2"], va="center")
        _style(ax[2], "Indoor temperature", ylabel="temperature (degC)", legend=True)
        _time_axis(ax[2], src)
    else:
        _blank(ax[2], "No thermal-mass model, so there is no indoor\n"
                      "temperature to track. Pre-cooling needs one.")

    # (d) Energy per building.
    if labels:
        energies = {lab: src.energy_kWh(f"{lab}_effective_Q_W") for lab in labels}
        energies = {k: v for k, v in energies.items() if v is not None}
        ordered = sorted(energies, key=lambda k: -energies[k])
        ax[3].bar(range(len(ordered)), [energies[k] for k in ordered],
                  color=PALETTE[0], width=0.7)
        ax[3].set_xticks(range(len(ordered)))
        ax[3].set_xticklabels([k.replace("_", " ") for k in ordered], rotation=35,
                              ha="right", fontsize=7)
        _style(ax[3], "Cooling energy per building", ylabel="energy (kWh)")
    else:
        _blank(ax[3], "No per-building duties in the results frame.")

    return _finish(fig, save_path, dpi)


def plot_chiller(source: Any, results: Any = None, title: Optional[str] = None,
                 axes: Any = None, save_path: Optional[str] = None,
                 dpi: int = 160) -> Any:
    """Plant duty and power, efficiency against ambient, and the duration curve."""
    src = resolve_source(source, results)
    fig, ax = _figure(2, 2, title or f"{src.name}: central chiller", axes)
    t = src.time

    # (a) Cooling produced. Power belongs on its own panel: two quantities of
    #     different magnitude sharing one pair of axes is the chart that lies.
    duty = src.col("chiller_Q_evap_W")
    if duty is not None:
        _line(ax[0], t, duty / W_PER_KW, C["plant"], "central evaporator duty")
        satellite = src.col("satellite_Q_evap_W")
        if satellite is not None and satellite.abs().max() > 0:
            _line(ax[0], t, satellite / W_PER_KW, C["satellite"], "satellite plants")
        demand = src.col("actual_building_total_Q_W")
        if demand is not None:
            _line(ax[0], t, demand / W_PER_KW, C["demand"], "building demand",
                  width=1.3, style="--")
        _style(ax[0], "Cooling produced and consumed", ylabel="duty (kW)", legend=True)
        _time_axis(ax[0], src)
    else:
        _blank(ax[0], "No chiller duty in the results frame.")

    # (b) Electrical power.
    power = src.col("compressor_power_W")
    if power is not None:
        _line(ax[1], t, power / W_PER_KW, C["power"], "central compressor")
        fleet = src.col("total_compressor_power_W")
        if fleet is not None and (fleet - power).abs().max() > 1.0:
            _line(ax[1], t, fleet / W_PER_KW, C["satellite"], "fleet compressors")
        pump = src.col("pump_power_W")
        if pump is not None and pump.max() > 0:
            _line(ax[1], t, pump / W_PER_KW, C["pump"], "distribution pump", width=1.3)
        _style(ax[1], "Electrical power", ylabel="power (kW)", legend=True)
        _time_axis(ax[1], src)
    else:
        _blank(ax[1], "No compressor power in the results frame.")

    # (c) COP against ambient: the dependence a fixed-efficiency model erases.
    cop = src.col("cop")
    ambient = src.col("ambient_temperature_degC")
    if cop is not None and ambient is not None:
        ax[2].scatter(ambient, cop, s=14, color=C["cop"], alpha=0.6, edgecolor="none")
        _style(ax[2], "COP against ambient temperature",
               "ambient temperature (degC)", "COP (-)")
        # A constant COP has no standard deviation, and correlating against it
        # is a divide by zero rather than a result.
        if len(cop.dropna()) > 3 and cop.std() > 0 and ambient.std() > 0:
            correlation = float(cop.corr(ambient))
            if correlation == correlation:
                ax[2].annotate(f"correlation {correlation:+.2f}", (0.03, 0.94),
                               xycoords="axes fraction", ha="left", va="top",
                               fontsize=7.5, color=C["ink2"])
    elif cop is not None:
        _line(ax[2], t, cop, C["cop"], "COP")
        _style(ax[2], "Coefficient of performance", ylabel="COP (-)")
        _time_axis(ax[2], src)
    else:
        _blank(ax[2], "No COP in the results frame.")

    # (d) Duration curve: how long the plant sits near its peak is the sizing
    #     question, and a time series does not answer it.
    if power is not None:
        curve = _duration_curve(power) / W_PER_KW
        hours = [i * src.dt_h for i in range(len(curve))]
        ax[3].fill_between(hours, curve, color=C["power"], alpha=0.18)
        _line(ax[3], hours, curve, C["power"], "central compressor")
        fleet = src.col("total_compressor_power_W")
        if fleet is not None and (fleet - power).abs().max() > 1.0:
            fleet_curve = _duration_curve(fleet) / W_PER_KW
            _line(ax[3], [i * src.dt_h for i in range(len(fleet_curve))],
                  fleet_curve, C["satellite"], "fleet compressors")
        _style(ax[3], "Compressor power duration curve", "hours at or above",
               "power (kW)", legend=True)
    else:
        _blank(ax[3], "No compressor power in the results frame.")

    return _finish(fig, save_path, dpi)


def plot_cooling_tower(source: Any, results: Any = None, title: Optional[str] = None,
                       axes: Any = None, save_path: Optional[str] = None,
                       dpi: int = 160) -> Any:
    """Heat rejection, condenser water temperatures, and loop closure."""
    src = resolve_source(source, results)
    fig, ax = _figure(2, 2, title or f"{src.name}: cooling tower", axes)
    t = src.time

    rejected = src.col("heat_rejection_W")
    if rejected is not None:
        _line(ax[0], t, rejected / W_PER_KW, C["rejection"], "heat rejected")
        duty = src.col("chiller_Q_evap_W")
        if duty is not None:
            _line(ax[0], t, duty / W_PER_KW, C["plant"], "evaporator duty",
                  width=1.3, style="--")
        _style(ax[0], "Heat rejection", ylabel="duty (kW)", legend=True)
        _time_axis(ax[0], src)
    else:
        _blank(ax[0], "No heat rejection in the results frame.")

    cw_in, cw_out = src.col("cw_in_T_degC"), src.col("cw_out_T_degC")
    ambient = src.col("ambient_temperature_degC")
    if cw_in is not None and cw_out is not None:
        _line(ax[1], t, cw_in, C["supply"], "condenser water in")
        _line(ax[1], t, cw_out, C["return"], "condenser water out")
        if ambient is not None:
            # A reference line, not a third member of the condenser loop, so it
            # wears muted ink rather than a categorical hue. It also keeps the
            # warm water off a red-against-orange contrast.
            _line(ax[1], t, ambient, C["ink2"], "ambient air", width=1.2, style="--")
        _style(ax[1], "Condenser loop temperatures", ylabel="temperature (degC)",
               legend=True)
        _time_axis(ax[1], src)
    else:
        _blank(ax[1], "No condenser water temperatures in the results frame.")

    # (c) Rejection against ambient. A tower that cannot reject on the hottest
    #     afternoon is the constraint the whole plant runs into.
    if rejected is not None and ambient is not None:
        ax[2].scatter(ambient, rejected / W_PER_KW, s=14, color=C["rejection"],
                      alpha=0.6, edgecolor="none")
        _style(ax[2], "Rejection against ambient", "ambient temperature (degC)",
               "heat rejected (kW)")
    else:
        _blank(ax[2], "No ambient temperature in the results frame.")

    # (d) Closure: rejected heat must equal duty plus work, snapshot by snapshot.
    duty, power = src.col("chiller_Q_evap_W"), src.col("compressor_power_W")
    if rejected is not None and duty is not None and power is not None:
        residual = rejected - duty - power
        ax[3].axhline(0.0, color=C["muted"], linewidth=1.0)
        _line(ax[3], t, residual, C["plant"], "rejected - duty - work")
        worst = float(residual.abs().max())
        ax[3].annotate(f"worst |residual| = {worst:.3g} W", (0.5, 0.92),
                       xycoords="axes fraction", ha="center", fontsize=8,
                       color=C["ink2"])
        _style(ax[3], "Condenser loop closure", ylabel="residual (W)")
        _time_axis(ax[3], src)
    else:
        _blank(ax[3], "Not enough columns to check the condenser closure.")

    return _finish(fig, save_path, dpi)


def plot_storage(source: Any, results: Any = None, title: Optional[str] = None,
                 axes: Any = None, save_path: Optional[str] = None,
                 dpi: int = 160) -> Any:
    """Charge and discharge, state of charge, ambient gain, and the thermocline."""
    from matplotlib.colors import LinearSegmentedColormap

    src = resolve_source(source, results)
    fig, ax = _figure(2, 2, title or f"{src.name}: cold storage", axes)
    t = src.time

    charge = src.col("storage_charge_power_W")
    discharge = src.col("storage_discharge_power_W")
    dispatched = (charge is not None and discharge is not None
                  and (charge.abs().max() + discharge.abs().max()) > 0)
    if dispatched:
        # Charging and discharging are opposite states of one thing, so they get
        # the diverging pair about a zero line rather than two unrelated hues.
        ax[0].axhline(0.0, color=C["muted"], linewidth=1.0)
        ax[0].fill_between(t, charge / W_PER_KW, 0.0, color=C["storage_charge"],
                           alpha=0.8, linewidth=0, label="charging")
        ax[0].fill_between(t, -discharge / W_PER_KW, 0.0, color=C["storage_discharge"],
                           alpha=0.8, linewidth=0, label="discharging")
        _style(ax[0], "Store dispatch", ylabel="power (kW)", legend=True)
        _time_axis(ax[0], src)
    else:
        _blank(ax[0], "No store was dispatched in this run.")

    soc = src.col("storage_soc_after")
    if soc is not None and soc.notna().any():
        _line(ax[1], t, soc, C["storage"], "state of charge")
        ax[1].set_ylim(0, 1.02)
        _style(ax[1], "State of charge", ylabel="state of charge (-)")
        _time_axis(ax[1], src)
    else:
        _blank(ax[1], "No state of charge in the results frame.")

    # (c) Ambient gain against ambient temperature. This is the panel that
    #     retires the flat daily-loss-fraction assumption.
    gain = src.col("storage_ambient_heat_gain_W")
    ambient = src.col("ambient_temperature_degC")
    if gain is not None and gain.max() > 0 and ambient is not None:
        ax[2].scatter(ambient, gain / W_PER_KW, s=14, color=C["storage"],
                      alpha=0.65, edgecolor="none")
        _style(ax[2], "Tank gain against ambient", "ambient temperature (degC)",
               "gain into the tank (kW)")
        low, high = float(gain.min()), float(gain.max())
        if low > 0:
            ax[2].annotate(
                f"{high / low:.1f}x between the coolest and hottest snapshot",
                (0.03, 0.94), xycoords="axes fraction", ha="left", va="top",
                fontsize=7.5, color=C["ink2"])
    else:
        _blank(ax[2], "No ambient-driven tank gain in this run.\n"
                      "Set storage.loss_model: ua to model it.")

    # (d) Stratified tank profile, where there is one.
    layers = sorted(
        (c for c in (src.results.columns if src.has_results else [])
         if c.startswith("storage_tank_T") and c.endswith("_degC")),
        key=lambda c: int(c[len("storage_tank_T"): -len("_degC")]),
    )
    if layers:
        profile = src.results[layers].apply(pd.to_numeric, errors="coerce").to_numpy().T
        cmap = LinearSegmentedColormap.from_list("chw", list(reversed(_SEQUENTIAL_BLUE)))
        image = ax[3].imshow(profile, aspect="auto", origin="lower", cmap=cmap,
                             interpolation="nearest")
        bar = fig.colorbar(image, ax=ax[3], pad=0.02)
        bar.set_label("layer temperature (degC)", fontsize=8, color=C["ink2"])
        bar.ax.tick_params(labelsize=7, colors=C["ink2"])
        _style(ax[3], "Stratified tank, coldest layer at the foot", "snapshot", "layer")
        ax[3].grid(False)  # a grid over a heat map reads as data that is not there
    else:
        thermocline = src.col("storage_thermocline_m")
        if thermocline is not None:
            _line(ax[3], t, thermocline, C["storage"], "thermocline thickness")
            _style(ax[3], "Thermocline thickness", ylabel="thickness (m)")
            _time_axis(ax[3], src)
        else:
            _blank(ax[3], "This store is a scalar model: it has an amount but\n"
                          "no temperature. A stratified tank has one.")

    return _finish(fig, save_path, dpi)


def plot_balance(source: Any, results: Any = None, title: Optional[str] = None,
                 axes: Any = None, save_path: Optional[str] = None,
                 dpi: int = 160) -> Any:
    """Where the plant's cooling goes, what it costs, and whether it adds up."""
    src = resolve_source(source, results)
    fig, ax = _figure(2, 2, title or f"{src.name}: plant energy balance", axes)
    t = src.time

    # (a) What the plant is producing against, stacked: demand first, then the
    #     parasitic terms a simpler model would leave out.
    layers = [
        ("actual_building_total_Q_W", "building demand", C["demand"]),
        ("pipe_heat_gain_W", "distribution pipe gain", C["pipe_gain"]),
        ("pump_power_W", "pump heat", C["pump"]),
        ("storage_ambient_heat_gain_W", "tank ambient gain", C["storage"]),
    ]
    stacked, names, colors = [], [], []
    for column, pretty, color in layers:
        series = src.col(column)
        if series is not None and series.abs().max() > 0:
            stacked.append(series.fillna(0.0).clip(lower=0.0) / W_PER_KW)
            names.append(pretty)
            colors.append(color)
    if stacked:
        ax[0].stackplot(t, *stacked, colors=colors, labels=names,
                        edgecolor="white", linewidth=0.6)
        produced = src.col("total_cooling_produced_W")
        if produced is None:
            produced = src.col("chiller_Q_evap_W")
        if produced is not None:
            _line(ax[0], t, produced / W_PER_KW, C["ink"], "plant cooling produced",
                  width=1.3, style="--")
        _headroom(ax[0], 0.42)
        _style(ax[0], "What the plant has to remove", ylabel="duty (kW)", legend=True)
        _time_axis(ax[0], src)
    else:
        _blank(ax[0], "No load breakdown in the results frame.")

    # (b) The same thing as energy shares, which is how it gets quoted.
    demand = src.energy_kWh("actual_building_total_Q_W")
    shares: List[Tuple[str, float, str]] = []
    if demand:
        for column, pretty, color in layers[1:]:
            value = src.energy_kWh(column)
            if value:
                shares.append((pretty, 100.0 * value / demand, color))
    if shares:
        names = [s[0] for s in shares]
        values = [s[1] for s in shares]
        bars = ax[1].barh(range(len(names)), values,
                          color=[s[2] for s in shares], height=0.62)
        ax[1].set_yticks(range(len(names)))
        ax[1].set_yticklabels(names, fontsize=8)
        ax[1].invert_yaxis()
        for bar, value in zip(bars, values):
            ax[1].annotate(f"{value:.2f} %",
                           (bar.get_width() + 0.03 * max(values),
                            bar.get_y() + bar.get_height() / 2),
                           va="center", fontsize=7.5, color=C["ink2"])
        ax[1].set_xlim(0, max(values) * 1.35)
        _style(ax[1], f"Parasitic load, {sum(values):.1f} % of metered demand",
               "share of building demand (%)")
    else:
        _blank(ax[1], "No parasitic terms to report. Pipes adiabatic\n"
                      "and the store lossless?")

    # (c) Electricity, which is what the district actually pays for.
    power = src.col("total_compressor_power_W")
    if power is None:
        power = src.col("compressor_power_W")
    if power is not None:
        pump = src.col("pump_power_W")
        parts = [power.fillna(0.0) / W_PER_KW]
        names, colors = ["compressors"], [C["power"]]
        if pump is not None and pump.max() > 0:
            parts.append(pump.fillna(0.0) / W_PER_KW)
            names.append("distribution pump")
            colors.append(C["pump"])
        ax[2].stackplot(t, *parts, colors=colors, labels=names,
                        edgecolor="white", linewidth=0.6)
        _headroom(ax[2], 0.35)
        _style(ax[2], "Electrical demand", ylabel="power (kW)", legend=True)
        _time_axis(ax[2], src)
    else:
        _blank(ax[2], "No electrical power in the results frame.")

    # (d) Closure. Without this the panels above are decoration.
    residual = src.col("chw_energy_residual_W")
    if residual is not None:
        ax[3].axhline(0.0, color=C["muted"], linewidth=1.0)
        _line(ax[3], t, residual, C["plant"], "chilled-water loop residual")
        worst = float(residual.abs().max())
        closes = worst <= 1.0
        ax[3].annotate(
            f"worst |residual| = {worst:.3g} W"
            + ("  (loop closes)" if closes else "  (loop does NOT close)"),
            (0.5, 0.92), xycoords="axes fraction", ha="center", fontsize=8,
            color=C["ink2"] if closes else PALETTE[7],
        )
        _style(ax[3], "Chilled-water loop closure", ylabel="residual (W)")
        _time_axis(ax[3], src)
    else:
        _blank(ax[3], "No closure residual in the results frame.")

    return _finish(fig, save_path, dpi)


def plot_system(source: Any, results: Any = None, title: Optional[str] = None,
                axes: Any = None, save_path: Optional[str] = None,
                dpi: int = 160) -> Any:
    """One page for the whole district: the six signals worth seeing together.

    Each panel here has a fuller treatment in its own component figure. This is
    the one to look at first, and the one to put in front of somebody who has
    not read the scenario file.
    """
    src = resolve_source(source, results)
    fig, ax = _figure(3, 2, title or f"{src.name}: district cooling system", axes,
                      height=2.5)
    t = src.time

    # 1. Weather, which drives everything else on the page.
    ambient = src.col("ambient_temperature_degC")
    ground = src.col("ground_temperature_degC")
    if ambient is not None:
        _line(ax[0], t, ambient, C["ambient"], "ambient air")
        if ground is not None:
            _line(ax[0], t, ground, C["ground"], "soil at pipe depth")
        _style(ax[0], "Weather", ylabel="temperature (degC)", legend=True)
        _time_axis(ax[0], src)
    else:
        _blank(ax[0], "No weather in the results frame.")

    # 2. Cooling: demand against what the plant had to make.
    demand = src.col("actual_building_total_Q_W")
    produced = src.col("total_cooling_produced_W")
    if produced is None:
        produced = src.col("chiller_Q_evap_W")
    if demand is not None:
        _line(ax[1], t, demand / W_PER_KW, C["demand"], "building demand")
        if produced is not None:
            _line(ax[1], t, produced / W_PER_KW, C["plant"], "plant production")
        _style(ax[1], "Cooling", ylabel="duty (kW)", legend=True)
        _time_axis(ax[1], src)
    else:
        _blank(ax[1], "No cooling duties in the results frame.")

    # 3. The gap between those two lines, broken out.
    parasitics = [("pipe_heat_gain_W", "pipe gain", C["pipe_gain"]),
                  ("pump_power_W", "pump heat", C["pump"]),
                  ("storage_ambient_heat_gain_W", "tank gain", C["storage"])]
    drawn = False
    for column, pretty, color in parasitics:
        series = src.col(column)
        if series is not None and series.abs().max() > 0:
            _line(ax[2], t, series / W_PER_KW, color, pretty, width=1.4)
            drawn = True
    if drawn:
        _style(ax[2], "Parasitic load", ylabel="duty (kW)", legend=True)
        _time_axis(ax[2], src)
    else:
        _blank(ax[2], "No parasitic load: pipes adiabatic, no pump\n"
                      "heat, no tank gain.")

    # 4. Electricity.
    power = src.col("total_compressor_power_W")
    if power is None:
        power = src.col("compressor_power_W")
    if power is not None:
        _line(ax[3], t, power / W_PER_KW, C["power"], "compressors")
        pump = src.col("pump_power_W")
        if pump is not None and pump.max() > 0:
            _line(ax[3], t, pump / W_PER_KW, C["pump"], "pump", width=1.3)
        _style(ax[3], "Electrical demand", ylabel="power (kW)", legend=True)
        _time_axis(ax[3], src)
    else:
        _blank(ax[3], "No electrical power in the results frame.")

    # 5. Distribution temperatures: the signal that says whether the network is
    #    delivering cooling or only moving water.
    supply = src.col("chw_supply_T_degC")
    ret = src.col("chw_return_T_degC")
    if supply is not None and ret is not None:
        _line(ax[4], t, supply, C["supply"], "district supply")
        _line(ax[4], t, ret, C["return"], "district return")
        plant = src.col("chw_plant_supply_T_degC")
        if plant is not None and (plant - supply).abs().max() > 0.05:
            _line(ax[4], t, plant, C["plant"], "plant leaving water",
                  width=1.3, style="--")
        _style(ax[4], "Chilled water", ylabel="temperature (degC)", legend=True)
        _time_axis(ax[4], src)
    else:
        _blank(ax[4], "No distribution temperatures in the results frame.")

    # 6. Storage, or the plant's efficiency when there is no store.
    soc = src.col("storage_soc_after")
    charge = src.col("storage_charge_power_W")
    if soc is not None and soc.notna().any():
        _line(ax[5], t, soc, C["storage"], "state of charge")
        ax[5].set_ylim(0, 1.02)
        _style(ax[5], "Cold store", ylabel="state of charge (-)")
        _time_axis(ax[5], src)
    elif charge is not None and charge.abs().max() > 0:
        _line(ax[5], t, charge / W_PER_KW, C["storage"], "charging")
        _style(ax[5], "Cold store", ylabel="power (kW)")
        _time_axis(ax[5], src)
    else:
        cop = src.col("fleet_cop")
        if cop is None:
            cop = src.col("cop")
        if cop is not None:
            _line(ax[5], t, cop, C["cop"], "COP")
            _style(ax[5], "Plant efficiency", ylabel="COP (-)")
            _time_axis(ax[5], src)
        else:
            _blank(ax[5], "No store in this scenario.")

    return _finish(fig, save_path, dpi)


# ---------------------------------------------------------------------------
# Everything at once
# ---------------------------------------------------------------------------

#: Figure name to plotting function, in the order :func:`plot_all` writes them.
FIGURES = {
    "system": plot_system,
    "balance": plot_balance,
    "pipes": plot_pipes,
    "buildings": plot_buildings,
    "chiller": plot_chiller,
    "cooling_tower": plot_cooling_tower,
    "storage": plot_storage,
}


def plot_all(source: Any, output_dir: Any, results: Any = None,
             prefix: str = "", dpi: int = 160, layout: bool = True,
             report: bool = True, close: bool = True) -> Dict[str, Path]:
    """Write every figure for one run to a directory and return the paths.

    Also writes the plan view (:func:`~discoolpy.layout.plot_network`) when a
    system was supplied, and the printed report as a text file. The point is
    that one call documents a whole run, whatever that run happened to contain:
    a scenario without a store still gets a storage figure, and the figure says
    there is no store rather than failing.
    """
    import matplotlib.pyplot as plt

    src = resolve_source(source, results)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = prefix or "".join(
        ch if ch.isalnum() or ch in "-_" else "_" for ch in src.name
    ).strip("_") or "system"
    paths: Dict[str, Path] = {}

    for name, function in FIGURES.items():
        path = directory / f"{stem}_{name}.png"
        figure = function(src, save_path=str(path), dpi=dpi)
        paths[name] = path
        if close:
            plt.close(figure)

    if layout and src.system is not None:
        from .layout import plot_network

        path = directory / f"{stem}_layout.png"
        ax = plot_network(src.system, save_path=str(path), annotate_pipes=True)
        paths["layout"] = path
        if close:
            plt.close(ax.figure)

    if report:
        path = directory / f"{stem}_report.txt"
        path.write_text(report_system(src).to_text(), encoding="utf-8")
        paths["report"] = path

    return paths
