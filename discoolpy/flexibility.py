"""Quantify the flexibility a district-cooling scenario can actually deliver.

Flexibility in a cooling system means moving electrical demand in time without
breaking a service constraint. Three things in this tool can do that: the cold
store, which makes cooling early and delivers it later; building thermal mass,
which lets you pre-cool a zone inside its comfort band; and the distribution
network, a marginal buffer that is mostly a loss.

Heat gains had to be modelled before any of this could be assessed honestly,
because gains erode all three levers, and they erode them hardest at the hour
the flexibility is worth most. A tank that holds its charge losslessly and a
chiller that makes ice at nominal COP always looks like a good investment. Give
the tank a gain of ``UA*(T_air - 0 degC)`` through a 45 degC afternoon, make
the plant depress its evaporating temperature to charge, and the number comes
down a long way. Sometimes it goes negative on energy while staying positive on
peak.

Everything here reads the result frames
:func:`discoolpy.utils.run_configured_case` produces, so you can assess a
scenario without re-running it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd

__all__ = [
    "LoadShapeMetrics",
    "HeatGainSummary",
    "FlexibilityReport",
    "load_shape_metrics",
    "heat_gain_summary",
    "assess_flexibility",
    "flexibility_envelope",
]


def _hours(results: pd.DataFrame, default: float = 1.0) -> float:
    """Snapshot duration in hours, inferred from the timestamp column."""
    if "resolution_hours" in results.columns and len(results):
        return float(results["resolution_hours"].iloc[0])
    if "timestamp" in results.columns and len(results) > 1:
        stamps = pd.to_datetime(results["timestamp"])
        delta = (stamps.iloc[1] - stamps.iloc[0]).total_seconds() / 3600.0
        if delta > 0:
            return float(delta)
    return float(default)


def _sum_kWh(series: pd.Series, dt_h: float) -> float:
    return float(pd.to_numeric(series, errors="coerce").fillna(0.0).sum()) * dt_h / 1000.0


@dataclass
class LoadShapeMetrics:
    """Shape of one electrical demand profile."""

    peak_kW: float
    mean_kW: float
    min_kW: float
    energy_kWh: float
    load_factor: float
    peak_to_mean: float
    max_ramp_up_kW_per_h: float
    max_ramp_down_kW_per_h: float

    def to_dict(self) -> Dict[str, float]:
        return dict(self.__dict__)


def load_shape_metrics(power_W: Sequence[float], dt_h: float) -> LoadShapeMetrics:
    """Peak, load factor and ramp statistics of an electrical demand series."""
    series = pd.to_numeric(pd.Series(list(power_W)), errors="coerce").dropna() / 1000.0
    if series.empty:
        raise ValueError("Cannot compute load-shape metrics from an empty series.")
    peak = float(series.max())
    mean = float(series.mean())
    ramps = series.diff().dropna() / dt_h
    return LoadShapeMetrics(
        peak_kW=peak,
        mean_kW=mean,
        min_kW=float(series.min()),
        energy_kWh=float(series.sum()) * dt_h,
        load_factor=(mean / peak) if peak else float("nan"),
        peak_to_mean=(peak / mean) if mean else float("nan"),
        max_ramp_up_kW_per_h=float(ramps.max()) if len(ramps) else 0.0,
        max_ramp_down_kW_per_h=float(ramps.min()) if len(ramps) else 0.0,
    )


@dataclass
class HeatGainSummary:
    """Where the parasitic cooling load came from, over the whole horizon."""

    building_demand_kWh: float
    pipe_gain_kWh: float
    storage_gain_kWh: float
    pump_heat_kWh: float
    plant_cooling_kWh: float
    pipe_gain_percent_of_demand: float
    storage_gain_percent_of_demand: float
    total_parasitic_percent_of_plant: float
    peak_pipe_gain_kW: float
    peak_storage_gain_kW: float

    def to_dict(self) -> Dict[str, float]:
        return dict(self.__dict__)


def heat_gain_summary(results: pd.DataFrame, dt_h: Optional[float] = None) -> HeatGainSummary:
    """Aggregate the heat-gain columns of a result frame.

    Missing columns are treated as zero so the function also works on results
    produced with heat gains disabled, which makes it usable as the "before"
    side of a comparison.
    """
    dt = _hours(results) if dt_h is None else float(dt_h)

    def col(name: str) -> pd.Series:
        if name in results.columns:
            return pd.to_numeric(results[name], errors="coerce").fillna(0.0)
        return pd.Series([0.0] * len(results), index=results.index)

    demand = _sum_kWh(col("actual_building_total_Q_W"), dt)
    pipe = _sum_kWh(col("pipe_heat_gain_W"), dt)
    storage = _sum_kWh(col("storage_ambient_heat_gain_W"), dt)
    pump = _sum_kWh(col("pump_power_W"), dt)
    plant = _sum_kWh(col("chiller_Q_evap_W"), dt)
    return HeatGainSummary(
        building_demand_kWh=demand,
        pipe_gain_kWh=pipe,
        storage_gain_kWh=storage,
        pump_heat_kWh=pump,
        plant_cooling_kWh=plant,
        pipe_gain_percent_of_demand=100.0 * pipe / demand if demand else float("nan"),
        storage_gain_percent_of_demand=100.0 * storage / demand if demand else float("nan"),
        total_parasitic_percent_of_plant=(
            100.0 * (pipe + storage + pump) / plant if plant else float("nan")
        ),
        peak_pipe_gain_kW=float(col("pipe_heat_gain_W").max()) / 1000.0,
        peak_storage_gain_kW=float(col("storage_ambient_heat_gain_W").max()) / 1000.0,
    )


@dataclass
class FlexibilityReport:
    """Complete flexibility assessment of a flexible case against a reference."""

    reference_name: str
    flexible_name: str
    resolution_hours: float
    reference: LoadShapeMetrics
    flexible: LoadShapeMetrics
    peak_reduction_kW: float
    peak_reduction_percent: float
    energy_penalty_kWh: float
    energy_penalty_percent: float
    shifted_energy_kWh: float
    storage_charged_kWh: float
    storage_discharged_kWh: float
    storage_losses_kWh: float
    storage_net_soc_change_kWh: float
    storage_round_trip_thermal: float
    electric_round_trip: float
    mean_cop_reference: float
    mean_cop_flexible: float
    charging_cop_penalty: float
    cost_reference: Optional[float] = None
    cost_flexible: Optional[float] = None
    cost_saving: Optional[float] = None
    cost_saving_percent: Optional[float] = None
    carbon_reference_kg: Optional[float] = None
    carbon_flexible_kg: Optional[float] = None
    carbon_saving_kg: Optional[float] = None
    heat_gains_reference: Optional[HeatGainSummary] = None
    heat_gains_flexible: Optional[HeatGainSummary] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if isinstance(value, (LoadShapeMetrics, HeatGainSummary)):
                out.update({f"{key}_{k}": v for k, v in value.to_dict().items()})
            elif key != "notes":
                out[key] = value
        return out

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([self.to_dict()])

    def to_markdown(self) -> str:
        """A compact, honest summary table."""
        def pct(x: Optional[float]) -> str:
            return "n/a" if x is None or x != x else f"{x:+.1f}%"

        def num(x: Optional[float], fmt: str = ".1f") -> str:
            return "n/a" if x is None or x != x else format(x, fmt)

        lines = [
            f"# Flexibility assessment: `{self.flexible_name}` vs `{self.reference_name}`",
            "",
            f"Horizon resolution: {self.resolution_hours:g} h.",
            "",
            "## Headline",
            "",
            "| Metric | Reference | Flexible | Change |",
            "|---|---:|---:|---:|",
            f"| Peak compressor power [kW] | {num(self.reference.peak_kW)} | "
            f"{num(self.flexible.peak_kW)} | {num(-self.peak_reduction_kW, '+.1f')} "
            f"({pct(-self.peak_reduction_percent)}) |",
            f"| Compressor energy [kWh] | {num(self.reference.energy_kWh)} | "
            f"{num(self.flexible.energy_kWh)} | {num(self.energy_penalty_kWh, '+.1f')} "
            f"({pct(self.energy_penalty_percent)}) |",
            f"| Load factor [-] | {num(self.reference.load_factor, '.3f')} | "
            f"{num(self.flexible.load_factor, '.3f')} | "
            f"{num(self.flexible.load_factor - self.reference.load_factor, '+.3f')} |",
            f"| Mean COP [-] | {num(self.mean_cop_reference, '.3f')} | "
            f"{num(self.mean_cop_flexible, '.3f')} | "
            f"{num(self.mean_cop_flexible - self.mean_cop_reference, '+.3f')} |",
            f"| Max ramp up [kW/h] | {num(self.reference.max_ramp_up_kW_per_h)} | "
            f"{num(self.flexible.max_ramp_up_kW_per_h)} | "
            f"{num(self.flexible.max_ramp_up_kW_per_h - self.reference.max_ramp_up_kW_per_h, '+.1f')} |",
            "",
            "## Where the flexibility went",
            "",
            "| Quantity | Value |",
            "|---|---:|",
            f"| Cooling charged into store [kWh] | {num(self.storage_charged_kWh)} |",
            f"| Cooling discharged from store [kWh] | {num(self.storage_discharged_kWh)} |",
            f"| Store losses (ambient gain + standby) [kWh] | {num(self.storage_losses_kWh)} |",
            f"| Net change in stored cooling over the horizon [kWh] | "
            f"{num(self.storage_net_soc_change_kWh, '+.1f')} |",
            f"| Thermal round-trip efficiency [-] | {num(self.storage_round_trip_thermal, '.3f')} |",
            f"| **Electric** round-trip efficiency [-] | {num(self.electric_round_trip, '.3f')} |",
            f"| COP while charging, vs the same hour in the reference [-] | "
            f"{num(self.charging_cop_penalty, '+.3f')} |",
            f"| Energy shifted out of the peak [kWh] | {num(self.shifted_energy_kWh)} |",
        ]
        if self.cost_saving is not None:
            lines += [
                "",
                "## Economics",
                "",
                "| Quantity | Reference | Flexible | Saving |",
                "|---|---:|---:|---:|",
                f"| Electricity cost | {num(self.cost_reference, '.2f')} | "
                f"{num(self.cost_flexible, '.2f')} | {num(self.cost_saving, '+.2f')} "
                f"({pct(self.cost_saving_percent)}) |",
            ]
        if self.carbon_saving_kg is not None:
            lines.append(
                f"| Emissions [kg CO2e] | {num(self.carbon_reference_kg, '.1f')} | "
                f"{num(self.carbon_flexible_kg, '.1f')} | {num(self.carbon_saving_kg, '+.1f')} |"
            )
        for label, summary in (
            ("reference", self.heat_gains_reference),
            ("flexible", self.heat_gains_flexible),
        ):
            if summary is None:
                continue
            lines += [
                "",
                f"## Parasitic heat gains ({label})",
                "",
                "| Source | Energy [kWh] | Share of building demand |",
                "|---|---:|---:|",
                f"| Building demand served | {summary.building_demand_kWh:.1f} | 100.0% |",
                f"| Distribution pipe gain | {summary.pipe_gain_kWh:.1f} | "
                f"{summary.pipe_gain_percent_of_demand:.2f}% |",
                f"| Storage ambient gain | {summary.storage_gain_kWh:.1f} | "
                f"{summary.storage_gain_percent_of_demand:.2f}% |",
                f"| Pump heat | {summary.pump_heat_kWh:.1f} | "
                f"{100 * summary.pump_heat_kWh / summary.building_demand_kWh:.2f}% |"
                if summary.building_demand_kWh
                else "| Pump heat | n/a | n/a |",
                f"| **Plant cooling produced** | **{summary.plant_cooling_kWh:.1f}** | "
                f"{100 * summary.plant_cooling_kWh / summary.building_demand_kWh:.2f}% |"
                if summary.building_demand_kWh
                else "| Plant cooling produced | n/a | n/a |",
            ]
        if self.notes:
            lines += ["", "## Notes", ""] + [f"- {n}" for n in self.notes]
        return "\n".join(lines)


def _tariff_series(tariff: Any, results: pd.DataFrame) -> Optional[pd.Series]:
    """Coerce a tariff specification into a per-snapshot price series."""
    if tariff is None:
        return None
    if isinstance(tariff, (int, float)):
        return pd.Series([float(tariff)] * len(results), index=results.index)
    if isinstance(tariff, str):
        if tariff not in results.columns:
            raise KeyError(f"Tariff column {tariff!r} is not present in the result frame.")
        return pd.to_numeric(results[tariff], errors="coerce").fillna(0.0)
    if isinstance(tariff, Mapping):
        # Hour-of-day mapping.
        stamps = pd.to_datetime(results["timestamp"])
        return stamps.dt.hour.map(lambda h: float(tariff.get(h, tariff.get(str(h), 0.0))))
    values = list(tariff)
    if len(values) != len(results):
        raise ValueError("Tariff sequence length must match the number of snapshots.")
    return pd.Series([float(v) for v in values], index=results.index)


def assess_flexibility(
    reference: pd.DataFrame,
    flexible: pd.DataFrame,
    reference_name: str = "reference",
    flexible_name: str = "flexible",
    tariff: Any = None,
    carbon_intensity: Any = None,
    peak_hours: Optional[Sequence[int]] = None,
    dt_h: Optional[float] = None,
) -> FlexibilityReport:
    """Compare a flexible case against an inflexible reference.

    ``tariff`` and ``carbon_intensity`` accept a scalar, a column name in the
    frames, an hour-of-day mapping, or a full-length sequence.

    ``peak_hours`` defines the window used for "energy shifted out of the peak".
    Defaults to 12:00-18:00, the usual cooling-system system peak.
    """
    if len(reference) != len(flexible):
        raise ValueError(
            f"Cases have different lengths ({len(reference)} vs {len(flexible)}); they must "
            "cover the same horizon at the same resolution to be comparable."
        )
    dt = _hours(reference) if dt_h is None else float(dt_h)
    notes: List[str] = []

    ref_metrics = load_shape_metrics(reference["compressor_power_W"], dt)
    flex_metrics = load_shape_metrics(flexible["compressor_power_W"], dt)

    peak_reduction = ref_metrics.peak_kW - flex_metrics.peak_kW
    peak_reduction_pct = 100.0 * peak_reduction / ref_metrics.peak_kW if ref_metrics.peak_kW else float("nan")
    energy_penalty = flex_metrics.energy_kWh - ref_metrics.energy_kWh
    energy_penalty_pct = (
        100.0 * energy_penalty / ref_metrics.energy_kWh if ref_metrics.energy_kWh else float("nan")
    )

    def col(frame: pd.DataFrame, name: str) -> pd.Series:
        if name in frame.columns:
            return pd.to_numeric(frame[name], errors="coerce").fillna(0.0)
        return pd.Series([0.0] * len(frame), index=frame.index)

    charged = _sum_kWh(col(flexible, "storage_charge_power_W"), dt)
    discharged = _sum_kWh(col(flexible, "storage_discharge_power_W"), dt)
    losses = (
        _sum_kWh(col(flexible, "storage_ambient_heat_gain_W"), dt)
        + float(col(flexible, "storage_fractional_loss_kWh").sum())
    )
    # A horizon that does not start and end at the same state of charge distorts
    # the round trip in both directions: ending fuller means charge that was
    # never discharged, ending emptier means discharge that was never charged
    # for. Net both sides out and report the residual so the reader can judge
    # how cyclic the run really was.
    stored = col(flexible, "storage_energy_after_kWh")
    net_soc_change = (
        float(stored.iloc[-1] - stored.iloc[0]) if len(stored) and stored.notna().any() else 0.0
    )
    cycled_in = charged - max(net_soc_change, 0.0)
    cycled_out = discharged + min(net_soc_change, 0.0)
    thermal_rt = cycled_out / cycled_in if cycled_in > 1e-9 else float("nan")

    # Electric round trip: the extra electricity spent charging, against the
    # electricity avoided while discharging. This is the number that decides
    # whether a store is worth running, and it is always worse than the thermal
    # figure because charging happens at a depressed evaporating temperature.
    electric_rt = float("nan")
    charging = col(flexible, "storage_charge_power_W") > 0
    discharging = col(flexible, "storage_discharge_power_W") > 0
    extra_electric = _sum_kWh(
        (col(flexible, "compressor_power_W") - col(reference, "compressor_power_W")).where(charging, 0.0),
        dt,
    )
    avoided_electric = _sum_kWh(
        (col(reference, "compressor_power_W") - col(flexible, "compressor_power_W")).where(discharging, 0.0),
        dt,
    )
    cyclic = charged <= 1e-9 or abs(net_soc_change) <= 0.10 * charged
    if extra_electric > 1e-9 and cyclic:
        electric_rt = avoided_electric / extra_electric
    elif extra_electric > 1e-9 and not cyclic:
        # Over a horizon that starts and ends at very different states of charge
        # the ratio compares charging and discharging of *different* energy, and
        # can even exceed 1. Reporting it would be worse than reporting nothing.
        notes.append(
            f"The store's state of charge moved {net_soc_change:+.0f} kWh over the horizon "
            f"({abs(net_soc_change)/max(charged, 1.0):.0%} of what was charged), so the "
            "electric round-trip figure is not meaningful and has been withheld. Start the "
            "run closer to its cyclic steady state, or extend the horizon."
        )
    elif charged > 0:
        notes.append(
            "Charging consumed no measurable extra compressor energy relative to the reference; "
            "the electric round-trip figure is unreliable for this run."
        )

    mean_cop_ref = float(pd.to_numeric(reference["cop"], errors="coerce").mean())
    mean_cop_flex = float(pd.to_numeric(flexible["cop"], errors="coerce").mean())
    # Compare like with like: the COP the plant achieved while charging against
    # the COP it achieved at the *same timestamps* in the reference run.
    # Comparing charging hours against non-charging hours instead would mostly
    # measure the day/night swing in condenser temperature, not the charging.
    cop_delta = (
        pd.to_numeric(flexible["cop"], errors="coerce")
        - pd.to_numeric(reference["cop"], errors="coerce")
    ).where(charging)
    charging_penalty = float(cop_delta.mean()) if cop_delta.notna().any() else float("nan")

    window = set(peak_hours) if peak_hours is not None else set(range(12, 18))
    stamps = pd.to_datetime(reference["timestamp"])
    in_peak = stamps.dt.hour.isin(window)
    shifted = _sum_kWh(
        (col(reference, "compressor_power_W") - col(flexible, "compressor_power_W")).where(in_peak, 0.0),
        dt,
    )

    report = FlexibilityReport(
        reference_name=reference_name,
        flexible_name=flexible_name,
        resolution_hours=dt,
        reference=ref_metrics,
        flexible=flex_metrics,
        peak_reduction_kW=peak_reduction,
        peak_reduction_percent=peak_reduction_pct,
        energy_penalty_kWh=energy_penalty,
        energy_penalty_percent=energy_penalty_pct,
        shifted_energy_kWh=shifted,
        storage_charged_kWh=charged,
        storage_discharged_kWh=discharged,
        storage_losses_kWh=losses,
        storage_net_soc_change_kWh=net_soc_change,
        storage_round_trip_thermal=thermal_rt,
        electric_round_trip=electric_rt,
        mean_cop_reference=mean_cop_ref,
        mean_cop_flexible=mean_cop_flex,
        charging_cop_penalty=charging_penalty,
        heat_gains_reference=heat_gain_summary(reference, dt),
        heat_gains_flexible=heat_gain_summary(flexible, dt),
        notes=notes,
    )

    price = _tariff_series(tariff, flexible)
    if price is not None:
        report.cost_reference = float((col(reference, "compressor_power_W") * price).sum()) * dt / 1000.0
        report.cost_flexible = float((col(flexible, "compressor_power_W") * price).sum()) * dt / 1000.0
        report.cost_saving = report.cost_reference - report.cost_flexible
        report.cost_saving_percent = (
            100.0 * report.cost_saving / report.cost_reference if report.cost_reference else float("nan")
        )

    intensity = _tariff_series(carbon_intensity, flexible)
    if intensity is not None:
        report.carbon_reference_kg = (
            float((col(reference, "compressor_power_W") * intensity).sum()) * dt / 1000.0
        )
        report.carbon_flexible_kg = (
            float((col(flexible, "compressor_power_W") * intensity).sum()) * dt / 1000.0
        )
        report.carbon_saving_kg = report.carbon_reference_kg - report.carbon_flexible_kg

    if energy_penalty > 0 and peak_reduction > 0:
        notes.append(
            f"This store buys {peak_reduction:.1f} kW of peak reduction for "
            f"{energy_penalty:.1f} kWh of extra electricity. Whether that trade is worth "
            "making depends entirely on the tariff's demand charge."
        )
    if thermal_rt == thermal_rt and electric_rt == electric_rt and electric_rt < thermal_rt:
        notes.append(
            f"Electric round-trip ({electric_rt:.3f}) is below the thermal round-trip "
            f"({thermal_rt:.3f}) because charging depresses the evaporating temperature. A model "
            "that shifts cooling at constant COP would miss this entirely."
        )
    if abs(net_soc_change) > 0.02 * max(charged, 1.0):
        notes.append(
            f"The store ended the horizon {net_soc_change:+.1f} kWh away from where it started. "
            "That energy is netted out of the round-trip figure, but a longer horizon or a "
            "cyclic-boundary run would give a cleaner comparison."
        )
    if charging_penalty == charging_penalty and charging_penalty < 0:
        notes.append(
            f"While charging, the plant ran {abs(charging_penalty):.3f} COP points below the "
            "reference case at the same hours, because a series-coupled store forces it to "
            "produce water below the distribution setpoint. This is the cost of making ice that "
            "an energy-only storage model never charges you for."
        )
    return report


def flexibility_envelope(
    buildings: Sequence[Any],
    storage: Optional[Any],
    dt_h: float,
    ambient_temperature_degC: float,
    solar_irradiance_W_m2: float = 0.0,
) -> Dict[str, float]:
    """Instantaneous up/down cooling-power flexibility of the whole system.

    ``increase_W`` is how much extra cooling the system could absorb right now
    (pre-cooling buildings plus charging the store); ``decrease_W`` is how much
    it could shed (coasting buildings plus discharging the store). Both respect
    comfort bands, storage SOC limits and power ratings, so the numbers are
    deliverable rather than nameplate.
    """
    increase = 0.0
    decrease = 0.0
    detail: Dict[str, float] = {}
    for building in buildings:
        if not hasattr(building, "flexibility_W"):
            continue
        flex = building.flexibility_W(
            dt_h=dt_h,
            ambient_temperature_degC=ambient_temperature_degC,
            solar_irradiance_W_m2=solar_irradiance_W_m2,
        )
        increase += flex["increase_W"]
        decrease += flex["decrease_W"]
        detail[f"{building.label}_increase_W"] = flex["increase_W"]
        detail[f"{building.label}_decrease_W"] = flex["decrease_W"]

    storage_increase = storage_decrease = 0.0
    if storage is not None:
        headroom_kWh = max(storage.max_energy_kWh - storage.energy_kWh, 0.0)
        available_kWh = max(storage.energy_kWh - storage.min_energy_kWh, 0.0)
        storage_increase = min(
            storage.max_charge_kW * 1000.0,
            headroom_kWh / (storage.charge_efficiency * dt_h) * 1000.0,
        )
        storage_decrease = min(
            storage.max_discharge_kW * 1000.0,
            available_kWh * storage.discharge_efficiency / dt_h * 1000.0,
        )
        increase += storage_increase
        decrease += storage_decrease

    return {
        "increase_W": increase,
        "decrease_W": decrease,
        "building_increase_W": increase - storage_increase,
        "building_decrease_W": decrease - storage_decrease,
        "storage_increase_W": storage_increase,
        "storage_decrease_W": storage_decrease,
        **detail,
    }
