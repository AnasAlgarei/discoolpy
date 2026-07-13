"""Utility functions for discoolpy examples.

The component classes remain small wrappers around TESPy objects. This module
contains orchestration helpers for planned-network examples: configuration
loading, pipe-attribute translation, standard three-building system assembly,
synthetic profile generation, time-schedule construction, result collection, and
comparison post-processing.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import matplotlib.pyplot as plt
import pandas as pd
import yaml
from tespy.components import CycleCloser
from tespy.connections import Connection
from tespy.networks import Network

from .branch import Branch
from .building import Building
from .chiller import Chiller
from .cold_storage import ColdStorage
from .cooling_tower import CoolingTower
from .time_snapshot import SnapshotSchedule

CP_WATER_DEFAULT = 4180.0


def load_yaml_config(path: str | Path) -> Dict[str, Any]:
    """Load a YAML scenario file and return a plain dictionary."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"YAML configuration file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML configuration must contain a mapping at the top level: {config_path}")
    data.setdefault("_config_path", str(config_path))
    data.setdefault("_config_dir", str(config_path.parent))
    return data


def ensure_output_dir(config: Mapping[str, Any]) -> Path:
    """Create and return the configured output directory."""
    outputs = config.get("outputs", {})
    output_dir = Path(outputs.get("output_dir", "/home/ubuntu/yaml_example_outputs")).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def output_path(config: Mapping[str, Any], key: str, default_name: str) -> Path:
    """Return one configured output file path inside the configured output dir."""
    outputs = config.get("outputs", {})
    return ensure_output_dir(config) / outputs.get(key, default_name)


def design_value(config: Mapping[str, Any], key: str, default: Optional[float] = None) -> float:
    """Read a numeric value from the `design` section."""
    design = config.get("design", {})
    if key not in design:
        if default is None:
            raise KeyError(f"Missing required design value: {key}")
        return float(default)
    return float(design[key])


def building_labels(config: Mapping[str, Any]) -> List[str]:
    return [str(b["label"]) for b in config.get("buildings", [])]


def building_design_loads(config: Mapping[str, Any]) -> List[float]:
    loads = [float(b["Q_design_W"]) for b in config.get("buildings", [])]
    if not loads:
        raise ValueError("At least one building must be defined in the YAML configuration.")
    return loads


def compute_building_mass_flows(config: Mapping[str, Any]) -> List[float]:
    """Compute design building mass flows from loads and supply/return temperatures."""
    cp = design_value(config, "cp_water_J_kgK", CP_WATER_DEFAULT)
    t_supply = design_value(config, "supply_temperature_degC")
    t_return = design_value(config, "building_return_temperature_degC")
    delta_t = t_return - t_supply
    if delta_t <= 0:
        raise ValueError("building_return_temperature_degC must be above supply_temperature_degC.")
    return [q / (cp * delta_t) for q in building_design_loads(config)]


def total_design_mass_flow(config: Mapping[str, Any]) -> float:
    return sum(compute_building_mass_flows(config)) + design_value(config, "bypass_mass_flow_kg_s", 0.0)


def design_evaporator_load(config: Mapping[str, Any]) -> float:
    return sum(building_design_loads(config)) + design_value(config, "pump_power_W", 0.0)


def _pipe_design_mass_flow(config: Mapping[str, Any], pipe_key: str) -> float:
    """Infer the design mass flow through a sequential branch pipe."""
    building_flows = compute_building_mass_flows(config)
    bypass = design_value(config, "bypass_mass_flow_kg_s", 0.0)
    total = sum(building_flows) + bypass
    name, _, number = pipe_key.partition("_")
    try:
        index = int(number)
    except ValueError as exc:
        raise ValueError(f"Pipe key {pipe_key!r} must use the form supply_1 or return_2.") from exc
    if name == "supply":
        return sum(building_flows[index - 1:]) + bypass
    if name == "return":
        return total if index == 1 else sum(building_flows[index - 1:]) + bypass
    raise ValueError(f"Unsupported pipe key {pipe_key!r}; expected supply_i or return_i.")


def _darcy_pressure_ratio_from_length(config: Mapping[str, Any], pipe_key: str, raw: Mapping[str, Any]) -> float:
    """Calculate an equivalent pressure ratio from L, D and ks using Darcy-Weisbach."""
    hydraulic = config.get("branch", {}).get("hydraulic", {})
    pressure_bar = float(hydraulic.get("design_pressure_bar", design_value(config, "chilled_water_pressure_bar")))
    rho = float(hydraulic.get("fluid_density_kg_m3", 999.0))
    mu = float(hydraulic.get("dynamic_viscosity_Pa_s", 0.0013))
    min_pr = float(hydraulic.get("min_pr", 0.90))
    max_pr = float(hydraulic.get("max_pr", 0.9999))
    length = float(raw["L"])
    diameter = float(raw["D"])
    roughness = float(raw["ks"])
    if length <= 0 or diameter <= 0 or roughness < 0:
        raise ValueError(f"Invalid length-derived pipe values for {pipe_key}: L and D must be positive and ks non-negative.")
    mass_flow = max(_pipe_design_mass_flow(config, pipe_key), 1e-9)
    area = math.pi * diameter**2 / 4.0
    velocity = mass_flow / (rho * area)
    reynolds = max(rho * velocity * diameter / mu, 1.0)
    if reynolds < 2300.0:
        friction = 64.0 / reynolds
    else:
        friction = 0.25 / (math.log10(roughness / (3.7 * diameter) + 5.74 / reynolds**0.9) ** 2)
    dp_pa = friction * (length / diameter) * rho * velocity**2 / 2.0
    pr = 1.0 - dp_pa / (pressure_bar * 1e5)
    return max(min_pr, min(max_pr, pr))


def make_pipe_attrs(config: Mapping[str, Any]) -> Dict[str, Dict[str, float]]:
    """Translate YAML pipe specifications into TESPy Pipe attributes.

    Supported `branch.pipe_model` values are `pressure_ratio`, `darcy`, and
    `length_derived_pr`. The latter accepts planned lengths and geometry in the
    YAML file, computes a Darcy-Weisbach design pressure drop, and passes the
    resulting pressure ratio to TESPy for robust time-series examples.
    """
    branch_cfg = config.get("branch", {})
    pipe_model = str(branch_cfg.get("pipe_model", "pressure_ratio")).lower().replace("-", "_")
    pipes = branch_cfg.get("pipes", {}) or {}
    if pipe_model not in {"pressure_ratio", "darcy", "length_derived_pr"}:
        raise ValueError("branch.pipe_model must be 'pressure_ratio', 'darcy', or 'length_derived_pr'.")

    attrs: Dict[str, Dict[str, float]] = {}
    for key, raw in pipes.items():
        if raw is None:
            attrs[str(key)] = {}
            continue
        if not isinstance(raw, Mapping):
            raise ValueError(f"Pipe specification for {key!r} must be a mapping.")
        pipe_attrs: Dict[str, float] = {}
        if "Q" in raw:
            pipe_attrs["Q"] = float(raw["Q"])
        elif "Q_W" in raw:
            pipe_attrs["Q"] = float(raw["Q_W"])
        if pipe_model == "pressure_ratio":
            if "pr" in raw and raw["pr"] is not None:
                pipe_attrs["pr"] = float(raw["pr"])
        elif pipe_model == "length_derived_pr":
            if {"L", "D", "ks"}.issubset(raw):
                pipe_attrs["pr"] = _darcy_pressure_ratio_from_length(config, str(key), raw)
            elif "pr" in raw and raw["pr"] is not None:
                pipe_attrs["pr"] = float(raw["pr"])
            elif any(attr in raw for attr in ("L", "D", "ks")):
                raise ValueError(f"Length-derived pipe {key} must provide L, D, and ks together, or none of them.")
        else:
            for attr in ("L", "D", "ks"):
                if attr in raw and raw[attr] is not None:
                    pipe_attrs[attr] = float(raw[attr])
            if {"L", "D", "ks"}.issubset(pipe_attrs):
                if pipe_attrs["L"] <= 0 or pipe_attrs["D"] <= 0 or pipe_attrs["ks"] < 0:
                    raise ValueError(f"Invalid Darcy pipe values for {key}: L and D must be positive and ks non-negative.")
            elif any(attr in pipe_attrs for attr in ("L", "D", "ks")):
                raise ValueError(f"Darcy pipe {key} must provide L, D, and ks together, or none of them.")
        attrs[str(key)] = pipe_attrs
    return attrs


def set_start(conn: Connection, m: float, T: float, p: float) -> None:
    """Set starting values on a TESPy connection."""
    conn.m.set_val0(float(m))
    conn.T.set_val0(float(T))
    conn.p.set_val0(float(p))


def build_standard_branch_system(config: Mapping[str, Any]) -> Tuple[Network, Chiller, List[Building], Branch, CoolingTower, Connection]:
    """Build the standard single-branch district-cooling network from YAML config."""
    chiller_cfg = config.get("chiller", {})
    chiller_pr = chiller_cfg.get("pressure_ratios", {})
    chill = Chiller(
        chiller_cfg.get("label", "yaml_config_chiller"),
        T_evap=float(chiller_cfg.get("T_evap_degC", 2.0)),
        T_cond=float(chiller_cfg.get("T_cond_degC", 46.0)),
        eta_s=float(chiller_cfg.get("eta_s", 0.75)),
        Q_evap=design_evaporator_load(config),
        refrigerant=chiller_cfg.get("refrigerant", "R134a"),
        pr_evap_1=float(chiller_pr.get("evap_1", 0.999)),
        pr_evap_2=float(chiller_pr.get("evap_2", 0.999)),
        pr_cond_1=float(chiller_pr.get("cond_1", 0.999)),
        pr_cond_2=float(chiller_pr.get("cond_2", 1.0)),
    )
    offdesign_cfg = chiller_cfg.get("native_offdesign", {})
    if offdesign_cfg.get("enabled", True):
        chill.configure_native_offdesign(
            evaporator_ttd_l=offdesign_cfg.get("evaporator_ttd_l"),
            condenser_ttd_u=offdesign_cfg.get("condenser_ttd_u"),
            use_pressure_loss_characteristics=bool(offdesign_cfg.get("use_pressure_loss_characteristics", False)),
        )

    buildings = [
        Building(str(item["label"]), Q_design=float(item["Q_design_W"]), pr=item.get("pr"))
        for item in config.get("buildings", [])
    ]

    branch_cfg = config.get("branch", {})
    branch = Branch(
        str(branch_cfg.get("label", "district")),
        buildings=buildings,
        bypass_m=design_value(config, "bypass_mass_flow_kg_s", 0.0),
        pump_placement=branch_cfg.get("pump_placement", "supply_inlet"),
        pump_label=branch_cfg.get("pump_label", "main pump"),
        uniform_pipes=False,
        pipe_attrs=make_pipe_attrs(config),
    )

    tower_cfg = config.get("cooling_tower", {})
    cooling_tower = CoolingTower(
        tower_cfg.get("label", "yaml cooling tower"),
        T_in_chiller=design_value(config, "condenser_inlet_temperature_degC"),
        T_out_chiller=design_value(config, "condenser_outlet_temperature_degC"),
        p_in_chiller=design_value(config, "condenser_pressure_bar"),
        fluid=tower_cfg.get("fluid", {"water": 1.0}),
        pr=tower_cfg.get("pr"),
        ambient_temperature=design_value(config, "ambient_temperature_degC"),
        approach_temperature=tower_cfg.get(
            "approach_temperature_K",
            design_value(config, "condenser_inlet_temperature_degC") - design_value(config, "ambient_temperature_degC"),
        ),
    )

    nw = Network(iterinfo=bool(config.get("solver", {}).get("iterinfo", False)))
    nw.units.set_defaults(
        temperature="degC",
        power="W",
        pressure="bar",
        pressure_difference="bar",
        enthalpy="kJ/kg",
    )

    cc_chw = CycleCloser(config.get("network", {}).get("chilled_water_cycle_closer", "chilled water cycle closer"))
    c_chiller_to_cc = Connection(chill, "out1", cc_chw, "in1", label="chiller_to_cyclecloser")

    branch.connect_between(
        cc_chw,
        "out1",
        chill,
        "in1",
        inlet_label="cyclecloser_to_pump",
        outlet_label="return_pipe_1_to_chiller",
    )
    cooling_tower.connect_to_chiller(
        chill,
        condenser_in_port="in2",
        condenser_out_port="out2",
        cond_in_label="cond_in",
        cond_out_label="cond_out",
        close_label="ct_to_secondary_closer",
    )

    nw.add_conns(c_chiller_to_cc)
    branch.add_to_network(nw)
    nw.add_conns(*cooling_tower.connections)
    nw.add_subsystems(chill)

    apply_design_specifications(config, branch, cooling_tower)
    apply_default_starting_values(config, c_chiller_to_cc, buildings, branch, cooling_tower)
    return nw, chill, buildings, branch, cooling_tower, c_chiller_to_cc


def apply_design_specifications(config: Mapping[str, Any], branch: Branch, cooling_tower: CoolingTower) -> None:
    """Apply design specifications to branch and cooling tower."""
    branch.connections["branch_in"].set_attr(
        fluid={"water": 1.0},
        T=design_value(config, "supply_temperature_degC"),
        p=design_value(config, "chilled_water_pressure_bar"),
    )
    branch_cfg = config.get("branch", {})
    pump_attrs = {"eta_s": design_value(config, "pump_efficiency", 0.7)}
    pipe_model = str(branch_cfg.get("pipe_model", "pressure_ratio")).lower().replace("-", "_")
    fix_pump_power_default = pipe_model != "darcy"
    if bool(branch_cfg.get("fix_pump_power", fix_pump_power_default)):
        pump_attrs["P"] = design_value(config, "pump_power_W", 0.0)
    elif "pump_pressure_ratio" in branch_cfg:
        pump_attrs["pr"] = float(branch_cfg["pump_pressure_ratio"])
    branch.set_design(
        pump_attrs=pump_attrs,
        building_mass_flows=compute_building_mass_flows(config),
        native_offdesign=bool(branch_cfg.get("native_offdesign", False)),
    )
    cooling_tower.set_design(native_offdesign=bool(config.get("cooling_tower", {}).get("native_offdesign", False)))


def apply_default_starting_values(
    config: Mapping[str, Any],
    c_chiller_to_cc: Connection,
    buildings: Sequence[Building],
    branch: Branch,
    cooling_tower: CoolingTower,
) -> None:
    """Apply robust default starting values for the standard sequential branch."""
    m_buildings = compute_building_mass_flows(config)
    bypass_m = design_value(config, "bypass_mass_flow_kg_s", 0.0)
    m_total = sum(m_buildings) + bypass_m
    t_supply = design_value(config, "supply_temperature_degC")
    t_return = design_value(config, "building_return_temperature_degC")
    p_supply = design_value(config, "chilled_water_pressure_bar")
    p_step = float(config.get("solver", {}).get("initial_pressure_step_bar", 0.05))

    set_start(c_chiller_to_cc, m_total, t_supply, p_supply)
    branch.set_connection_start("branch_in", m_total, t_supply, p_supply)
    if "pump_to_supply_1" in branch.connections:
        branch.set_connection_start("pump_to_supply_1", m_total, t_supply, p_supply + 0.2)
    branch.set_connection_start("supply_1_to_splitter_1", m_total, t_supply, p_supply + 0.15)

    remaining = m_total
    for i, building in enumerate(buildings, start=1):
        m_b = m_buildings[i - 1]
        p_in = p_supply + 0.15 - (i - 1) * p_step
        b_cfg = config.get("buildings", [])[i - 1] if i - 1 < len(config.get("buildings", [])) else {}
        b_pr = b_cfg.get("pr")
        p_out = p_in * float(b_pr) if b_pr is not None else p_in - 0.15
        building.set_start(m_b, t_supply, t_return, p_in, p_out)
        remaining -= m_b
        if i < len(buildings):
            branch.set_connection_start(f"splitter_{i}_to_supply_{i + 1}", remaining, t_supply, p_in)
            branch.set_connection_start(f"supply_{i + 1}_to_splitter_{i + 1}", remaining, t_supply, p_in - p_step)

    last_i = len(buildings)
    bypass_p = p_supply + 0.15 - (last_i - 1) * p_step
    branch.set_connection_start("bypass_in", bypass_m, t_supply, bypass_p)
    branch.set_connection_start("bypass_out", bypass_m, t_supply, bypass_p - 0.15)

    cumulative_return = bypass_m
    for i in reversed(range(1, len(buildings) + 1)):
        cumulative_return += m_buildings[i - 1]
        p_ret = bypass_p - 0.15 - (len(buildings) - i) * p_step
        branch.set_connection_start(f"merge_{i}_to_return_{i}", cumulative_return, t_return, p_ret)
        if i > 1:
            branch.set_connection_start(f"return_{i}_to_merge_{i - 1}", cumulative_return, t_return, p_ret - p_step)
    branch.set_connection_start("branch_out", m_total, t_return, max(0.5, p_supply - 0.4))
    cooling_tower.set_start(m=float(config.get("cooling_tower", {}).get("start_mass_flow_kg_s", 45.0)), pr_start=0.999)


def make_storage_from_config(config: Mapping[str, Any]) -> Optional[ColdStorage]:
    """Create a ColdStorage instance when the YAML storage section is enabled."""
    storage_cfg = config.get("storage", {})
    if not storage_cfg or not bool(storage_cfg.get("enabled", False)):
        return None
    return ColdStorage(
        label=storage_cfg.get("label", "ice_storage"),
        capacity_kWh=float(storage_cfg.get("capacity_kWh", 1600.0)),
        initial_soc=float(storage_cfg.get("initial_soc", 0.55)),
        max_charge_kW=float(storage_cfg.get("max_charge_kW", 180.0)),
        max_discharge_kW=float(storage_cfg.get("max_discharge_kW", 180.0)),
        charge_efficiency=float(storage_cfg.get("charge_efficiency", 0.90)),
        discharge_efficiency=float(storage_cfg.get("discharge_efficiency", 0.92)),
        standby_loss_fraction_per_day=float(storage_cfg.get("standby_loss_fraction_per_day", 0.015)),
        storage_type=storage_cfg.get("storage_type", "ice"),
        target_chiller_load_kW=float(storage_cfg.get("target_chiller_load_kW", 555.0)),
        min_soc=float(storage_cfg.get("min_soc", 0.08)),
        max_soc=float(storage_cfg.get("max_soc", 0.97)),
    )


def make_riyadh_weather_and_load_profiles(config: Mapping[str, Any]) -> pd.DataFrame:
    """Generate the synthetic Riyadh seven-day profile from YAML parameters."""
    profile_cfg = config.get("profiles", {})
    start = profile_cfg.get("start", "2026-07-01 00:00:00")
    periods = int(profile_cfg.get("periods", 7 * 48))
    freq = profile_cfg.get("freq", "30min")
    timestamps = pd.date_range(start, periods=periods, freq=freq)
    daily_high = list(profile_cfg.get("daily_high_degC", [43.0, 44.0, 42.5, 45.0, 44.0, 43.5, 42.8]))
    daily_low = list(profile_cfg.get("daily_low_degC", [29.0, 30.0, 29.2, 31.0, 30.0, 29.5, 29.0]))
    loads = building_design_loads(config)
    labels = building_labels(config)
    if len(loads) != 3:
        raise ValueError("The default Riyadh profile generator expects exactly three buildings.")

    records: List[Dict[str, Any]] = []
    for ts in timestamps:
        day = (ts.date() - timestamps[0].date()).days % len(daily_high)
        hour = ts.hour + ts.minute / 60.0
        mean = 0.5 * (float(daily_high[day]) + float(daily_low[day]))
        amp = 0.5 * (float(daily_high[day]) - float(daily_low[day]))
        ambient = mean - amp * math.cos(2 * math.pi * (hour - 5.0) / 24.0)
        ambient += 0.35 * math.sin(2 * math.pi * day / max(1, len(daily_high)))
        solar = max(0.0, math.sin(math.pi * (hour - 6.0) / 13.0))
        office_occ = 0.25 + 0.75 / (1 + math.exp(-(hour - 8.0))) * (1 - 1 / (1 + math.exp(-(hour - 18.0))))
        hotel_occ = 0.55 + 0.20 * math.sin(2 * math.pi * (hour - 18.0) / 24.0) + 0.10 * math.sin(4 * math.pi * hour / 24.0)
        retail_occ = 0.18 + 0.82 / (1 + math.exp(-(hour - 10.0))) * (1 - 1 / (1 + math.exp(-(hour - 23.0))))
        weekend_factor = 0.92 if ts.dayofweek in (4, 5) else 1.0
        weather_index = max(0.0, min(1.25, (ambient - 27.0) / (45.0 - 27.0)))
        solar_index = max(0.0, min(1.0, solar))
        b1 = loads[0] * (0.32 + 0.43 * weather_index + 0.17 * solar_index + 0.18 * office_occ) * weekend_factor
        b2 = loads[1] * (0.48 + 0.35 * weather_index + 0.08 * solar_index + 0.16 * hotel_occ)
        b3 = loads[2] * (0.25 + 0.42 * weather_index + 0.18 * solar_index + 0.23 * retail_occ) * (1.08 if ts.dayofweek in (4, 5) else 1.0)
        b1 = max(0.35 * loads[0], min(loads[0], b1))
        b2 = max(0.42 * loads[1], min(loads[1], b2))
        b3 = max(0.32 * loads[2], min(loads[2], b3))
        building_values = [b1, b2, b3]
        total = sum(building_values)
        load_fraction = total / sum(loads)
        condenser_in = max(30.0, min(39.0, ambient - 5.8 + 1.8 * load_fraction))
        record = {
            "timestamp": ts,
            "ambient_temperature_degC": ambient,
            "condenser_inlet_temperature_degC": condenser_in,
            "total_building_Q_W": total,
            "weather_index": weather_index,
            "solar_index": solar_index,
            "load_fraction": load_fraction,
        }
        for label, value in zip(labels, building_values):
            record[f"{label}_Q_W"] = value
        records.append(record)
    return pd.DataFrame(records)


def make_schedule_from_profile(config: Mapping[str, Any], profile_df: pd.DataFrame, name: str) -> SnapshotSchedule:
    """Create a SnapshotSchedule from a profile dataframe and configured building labels."""
    labels = building_labels(config)
    return SnapshotSchedule.from_profile_arrays(
        start=profile_df["timestamp"].iloc[0].isoformat(),
        resolution=config.get("profiles", {}).get("freq", "30min"),
        building_profiles={label: profile_df[f"{label}_Q_W"].tolist() for label in labels},
        name=name,
        ambient_temperatures=profile_df["ambient_temperature_degC"].tolist(),
        condenser_inlet_temperatures=profile_df["condenser_inlet_temperature_degC"].tolist(),
        metadata_profile={
            "weather_index": profile_df["weather_index"].tolist(),
            "solar_index": profile_df["solar_index"].tolist(),
            "load_fraction": profile_df["load_fraction"].tolist(),
        },
    )


def reset_case_directory(output_dir: Path, case: str) -> Path:
    case_dir = output_dir / case
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir


def collect_result(snapshot_index, case, actual_snapshot, solved_snapshot, chiller, buildings, branch, cooling_tower, storage_record=None) -> Dict[str, float]:
    """Collect a standard result row from a solved snapshot."""
    q_actual = actual_snapshot.total_building_load
    q_effective = solved_snapshot.total_building_load
    q_evap = abs(chiller.evaporator.Q.val)
    compressor_power = chiller.compressor.P.val
    heat_rejection = cooling_tower.heat_rejection
    cop = q_evap / compressor_power if compressor_power else float("nan")
    conns = chiller.internal_connections
    record = {
        "case": case,
        "snapshot_index": snapshot_index,
        "timestamp": actual_snapshot.timestamp.isoformat(),
        "ambient_temperature_degC": actual_snapshot.ambient_temperature,
        "condenser_inlet_temperature_degC": actual_snapshot.condenser_inlet_temperature,
        "actual_building_total_Q_W": q_actual,
        "effective_building_total_Q_W": q_effective,
        "chiller_Q_evap_W": q_evap,
        "compressor_power_W": compressor_power,
        "cop": cop,
        "heat_rejection_W": heat_rejection,
        "cw_in_T_degC": conns["cw_in"].T.val,
        "cw_out_T_degC": conns["cw_out"].T.val,
        "chw_supply_T_degC": branch.connections["branch_in"].T.val,
        "chw_return_T_degC": branch.connections["branch_out"].T.val,
        "cw_m_kg_s": conns["cw_in"].m.val_SI,
        "chw_total_m_kg_s": branch.connections["branch_in"].m.val_SI,
        "pump_power_W": branch.pump.P.val if branch.pump is not None else 0.0,
    }
    for building in buildings:
        safe = building.label.replace(" ", "_")
        record[f"{safe}_effective_Q_W"] = building.component.Q.val
    if storage_record is None:
        record.update({
            "storage_mode": "none",
            "storage_power_W": 0.0,
            "storage_charge_power_W": 0.0,
            "storage_discharge_power_W": 0.0,
            "storage_soc_after": float("nan"),
            "storage_energy_after_kWh": float("nan"),
            "storage_chiller_load_offset_W": 0.0,
            "storage_curtailed_request_W": 0.0,
        })
    else:
        record.update({
            "storage_mode": storage_record.mode,
            "storage_power_W": storage_record.storage_power_W,
            "storage_charge_power_W": storage_record.charge_power_W,
            "storage_discharge_power_W": storage_record.discharge_power_W,
            "storage_soc_after": storage_record.soc_after,
            "storage_energy_after_kWh": storage_record.energy_after_kWh,
            "storage_chiller_load_offset_W": storage_record.chiller_load_offset_W,
            "storage_curtailed_request_W": storage_record.curtailed_request_W,
        })
    return record


def run_configured_case(config: Mapping[str, Any], profile_df: pd.DataFrame, case: str, use_storage: bool) -> pd.DataFrame:
    """Run one configured design/offdesign case and return the result dataframe."""
    output_dir = ensure_output_dir(config)
    case_dir = reset_case_directory(output_dir, case)
    design_path = case_dir / "design_state"
    nw, chill, buildings, branch, cooling_tower, _ = build_standard_branch_system(config)
    schedule = make_schedule_from_profile(config, profile_df, f"{case}_schedule")
    storage = make_storage_from_config(config) if use_storage else None
    solver_cfg = config.get("solver", {})
    max_iter = int(solver_cfg.get("max_iter", 250))
    pump_offset = design_value(config, "pump_power_W", 0.0)
    storage_cfg = config.get("storage", {})
    target_chiller_load_W = float(storage_cfg.get("target_chiller_load_kW", 555.0)) * 1000.0
    min_load_fraction = float(storage_cfg.get("minimum_load_fraction", 0.08))

    print(f"[{case}] Solving design case...")
    nw.solve(mode="design", max_iter=max_iter)
    if not bool(getattr(nw, "converged", False)):
        raise RuntimeError(f"Design solve for case {case!r} did not converge; review pipe lengths, diameters, pressure anchors, and starting values.")
    chill.enable_compressor_characteristic_extrapolation()
    nw.save(str(design_path))

    records = []
    for index, snapshot in enumerate(schedule):
        if storage is None:
            solved_snapshot = snapshot
            storage_record = None
        else:
            storage_record, solved_snapshot = storage.dispatch_and_make_effective_snapshot(
                snapshot,
                base_chiller_load_offset_W=pump_offset,
                target_chiller_load_W=target_chiller_load_W,
                minimum_load_fraction=min_load_fraction,
            )
        solved_snapshot.apply(
            buildings=buildings,
            chiller=chill,
            cooling_tower=cooling_tower,
            chiller_load_offset_W=pump_offset,
        )
        nw.solve(mode="offdesign", design_path=str(design_path), max_iter=max_iter)
        if not bool(getattr(nw, "converged", False)):
            raise RuntimeError(f"Offdesign solve for case {case!r}, snapshot {index}, did not converge; review pipe and load-profile settings.")
        records.append(collect_result(index, case, snapshot, solved_snapshot, chill, buildings, branch, cooling_tower, storage_record))
        if index % int(solver_cfg.get("progress_interval", 48)) == 0:
            print(f"[{case}] Solved snapshot {index}: {snapshot.timestamp:%Y-%m-%d %H:%M}, COP={records[-1]['cop']:.3f}")
    return pd.DataFrame(records)


def write_storage_comparison_summary(config: Mapping[str, Any], no_storage: pd.DataFrame, with_storage: pd.DataFrame) -> pd.DataFrame:
    """Write comparison CSV and Markdown summary for paired storage cases."""
    comparison = pd.DataFrame({
        "snapshot_index": no_storage["snapshot_index"],
        "timestamp": no_storage["timestamp"],
        "actual_building_total_Q_W": no_storage["actual_building_total_Q_W"],
        "without_storage_compressor_power_W": no_storage["compressor_power_W"],
        "with_storage_compressor_power_W": with_storage["compressor_power_W"],
        "without_storage_chiller_Q_evap_W": no_storage["chiller_Q_evap_W"],
        "with_storage_chiller_Q_evap_W": with_storage["chiller_Q_evap_W"],
        "without_storage_cop": no_storage["cop"],
        "with_storage_cop": with_storage["cop"],
        "storage_power_W": with_storage["storage_power_W"],
        "storage_soc_after": with_storage["storage_soc_after"],
    })
    comparison["compressor_power_reduction_W"] = comparison["without_storage_compressor_power_W"] - comparison["with_storage_compressor_power_W"]
    comparison.to_csv(output_path(config, "comparison_csv", "storage_comparison_summary_timeseries.csv"), index=False)

    resolution_hours = pd.to_timedelta(config.get("profiles", {}).get("freq", "30min")).total_seconds() / 3600.0
    no_energy_kWh = no_storage["compressor_power_W"].sum() * resolution_hours / 1000.0
    with_energy_kWh = with_storage["compressor_power_W"].sum() * resolution_hours / 1000.0
    charge_kWh = with_storage["storage_charge_power_W"].sum() * resolution_hours / 1000.0
    discharge_kWh = with_storage["storage_discharge_power_W"].sum() * resolution_hours / 1000.0
    storage_cfg = config.get("storage", {})
    branch_cfg = config.get("branch", {})
    summary = f"""# YAML-Driven Cold/Ice Storage Comparison Summary

The paired examples use the same three-building Riyadh cooling-demand profile and the same TESPy design point. Inputs are loaded from `{Path(config.get('_config_path', 'scenario.yaml')).name}`. The branch pipe model is `{branch_cfg.get('pipe_model', 'pressure_ratio')}`, which allows the same example to use either simple pressure ratios or planned street-pipe lengths and Darcy-Weisbach parameters.

| Metric | Without storage | With storage | Difference |
|---|---:|---:|---:|
| Solved snapshots | {len(no_storage)} | {len(with_storage)} | {len(with_storage) - len(no_storage)} |
| Compressor energy [kWh] | {no_energy_kWh:.1f} | {with_energy_kWh:.1f} | {with_energy_kWh - no_energy_kWh:.1f} |
| Peak compressor power [kW] | {no_storage['compressor_power_W'].max()/1000:.1f} | {with_storage['compressor_power_W'].max()/1000:.1f} | {with_storage['compressor_power_W'].max()/1000 - no_storage['compressor_power_W'].max()/1000:.1f} |
| Maximum chiller evaporator load [kW] | {no_storage['chiller_Q_evap_W'].max()/1000:.1f} | {with_storage['chiller_Q_evap_W'].max()/1000:.1f} | {with_storage['chiller_Q_evap_W'].max()/1000 - no_storage['chiller_Q_evap_W'].max()/1000:.1f} |
| Mean COP [-] | {no_storage['cop'].mean():.3f} | {with_storage['cop'].mean():.3f} | {with_storage['cop'].mean() - no_storage['cop'].mean():.3f} |
| Storage target load [kW] | n/a | {float(storage_cfg.get('target_chiller_load_kW', 0.0)):.1f} | n/a |
| Storage charged [kWh cooling] | 0.0 | {charge_kWh:.1f} | {charge_kWh:.1f} |
| Storage discharged [kWh cooling] | 0.0 | {discharge_kWh:.1f} | {discharge_kWh:.1f} |
| Final storage SOC [-] | n/a | {with_storage['storage_soc_after'].iloc[-1]:.3f} | n/a |

The storage case intentionally shifts cooling production from high-load periods to lower-load periods. The TESPy network solves the effective plant load, while the output tables preserve actual building demand, storage charge/discharge, state of charge, compressor power, COP, and heat rejection separately.
"""
    output_path(config, "summary_md", "storage_comparison_summary.md").write_text(summary, encoding="utf-8")
    return comparison


def make_storage_comparison_plot(config: Mapping[str, Any], no_storage: pd.DataFrame, with_storage: pd.DataFrame) -> None:
    """Create the standard five-panel storage comparison plot."""
    no = no_storage.copy()
    ws = with_storage.copy()
    no["timestamp"] = pd.to_datetime(no["timestamp"])
    ws["timestamp"] = pd.to_datetime(ws["timestamp"])
    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(5, 1, figsize=(14, 18), sharex=True, facecolor="white")
    for ax in axes:
        ax.set_facecolor("white")
    axes[0].plot(no["timestamp"], no["actual_building_total_Q_W"] / 1000, color="black", label="Actual building cooling demand")
    axes[0].plot(ws["timestamp"], ws["effective_building_total_Q_W"] / 1000, color="tab:blue", label="Effective TESPy plant load with storage")
    axes[0].set_ylabel("Cooling load [kW]")
    axes[0].set_title("Actual demand versus storage-shifted plant load")
    axes[0].legend(loc="upper left")
    axes[1].plot(ws["timestamp"], ws["storage_power_W"] / 1000, color="tab:cyan", label="Storage power (+ discharge, - charge)")
    axes[1].axhline(0, color="black", linewidth=0.8)
    axes[1].set_ylabel("Storage power [kW]")
    axes[1].set_title("Ice-storage charge/discharge dispatch")
    axes[1].legend(loc="upper left")
    axes[2].plot(ws["timestamp"], ws["storage_soc_after"], color="tab:green", label="Storage SOC")
    axes[2].set_ylabel("SOC [-]")
    axes[2].set_ylim(0, 1.02)
    axes[2].set_title("Storage state of charge")
    axes[2].legend(loc="upper left")
    axes[3].plot(no["timestamp"], no["compressor_power_W"] / 1000, label="Without storage", color="tab:red")
    axes[3].plot(ws["timestamp"], ws["compressor_power_W"] / 1000, label="With storage", color="tab:purple")
    axes[3].set_ylabel("Compressor power [kW]")
    axes[3].set_title("Compressor power comparison")
    axes[3].legend(loc="upper left")
    axes[4].plot(no["timestamp"], no["cop"], label="Without storage", color="tab:orange")
    axes[4].plot(ws["timestamp"], ws["cop"], label="With storage", color="tab:blue")
    axes[4].set_ylabel("COP [-]")
    axes[4].set_title("Chiller COP comparison")
    axes[4].set_xlabel("Timestamp")
    axes[4].legend(loc="upper left")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path(config, "plot_png", "storage_comparison_results.png"), dpi=180, bbox_inches="tight", facecolor="white")


__all__ = [
    "load_yaml_config",
    "ensure_output_dir",
    "output_path",
    "make_pipe_attrs",
    "compute_building_mass_flows",
    "total_design_mass_flow",
    "design_evaporator_load",
    "build_standard_branch_system",
    "make_storage_from_config",
    "make_riyadh_weather_and_load_profiles",
    "make_schedule_from_profile",
    "run_configured_case",
    "write_storage_comparison_summary",
    "make_storage_comparison_plot",
]
