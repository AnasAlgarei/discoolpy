"""Orchestration: load a scenario, assemble the network, run it.

The component classes stay thin wrappers around TESPy objects. Assembly lives
here instead: reading the configuration, translating pipe attributes, building
the branch tree with its heat gains and any hydraulically coupled storage,
generating synthetic profiles for however many buildings there are, building
the time schedule, collecting results, and post-processing comparisons.
"""

from __future__ import annotations

import math
import shutil
import warnings
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import matplotlib.pyplot as plt
import pandas as pd
import yaml
from tespy.components import CycleCloser
from tespy.connections import Connection
from tespy.networks import Network

from .branch import Branch, EndBypass, SatellitePlant, SubBranch, Terminal
from .building import Building
from .chiller import Chiller
from .cold_storage import ColdStorage
from .config_schema import (
    BranchSpec,
    TerminalSpec,
    branch_pipe_thermal,
    building_design_load_W,
    heat_gains_enabled,
    make_branch_specs,
    make_buildings,
    make_pipe_thermal,
    make_storage,
    make_storage_from_section,
    plant_control_mode,
)
from .cooling_tower import CoolingTower
from .hydraulics import (
    BranchTopology,
    TerminalTopology,
    analyse_network_topology,
    resolve_network_pressure_specification,
    validate_network_topology,
    validate_thermal_degrees_of_freedom,
)
from .thermal import soil_temperature_degC
from .time_snapshot import SnapshotSchedule

CP_WATER_DEFAULT = 4180.0

# Fallbacks for the `design` section, so a scenario only has to state the
# values it actually cares about. These are ordinary chilled-water district
# numbers: 7/12 degC distribution, a condenser loop 5 K wide sitting above a
# 35 degC design ambient. Any scenario that sets a key overrides its fallback,
# so nothing that already spells these out changes behaviour.
DESIGN_DEFAULTS: Dict[str, float] = {
    "supply_temperature_degC": 7.0,
    "building_return_temperature_degC": 12.0,
    "ambient_temperature_degC": 35.0,
    "condenser_inlet_temperature_degC": 30.0,
    "condenser_outlet_temperature_degC": 35.0,
    "chilled_water_pressure_bar": 3.0,
    "condenser_pressure_bar": 3.0,
    "cp_water_J_kgK": CP_WATER_DEFAULT,
    "pump_efficiency": 0.70,
    "bypass_mass_flow_kg_s": 0.05,
}

# Nominal distribution pump head used only when a scenario states no
# `design.pump_power_W`. Real district heads run from well under 1 bar on a
# short campus loop to 3 bar on a several-kilometre network, so this is a
# placeholder to get a first solve, not a sizing. `build_system` says so in
# its notes whenever it falls back to it.
NOMINAL_PUMP_HEAD_BAR = 1.0


# Configuration and paths

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
    """Create and return the configured output directory.

    A relative ``outputs.output_dir`` resolves against the scenario file's own
    directory rather than the current working directory, so a scenario writes
    to the same place whether you launch it from the repo root, from
    ``examples/`` or from a notebook.
    """
    outputs = config.get("outputs", {}) or {}
    raw = outputs.get("output_dir", "outputs")
    output_dir = Path(raw).expanduser()
    if not output_dir.is_absolute():
        base = Path(config.get("_config_dir", Path.cwd()))
        output_dir = (base / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def output_path(config: Mapping[str, Any], key: str, default_name: str) -> Path:
    """Return one configured output file path inside the configured output dir."""
    outputs = config.get("outputs", {}) or {}
    return ensure_output_dir(config) / outputs.get(key, default_name)


def design_value(config: Mapping[str, Any], key: str, default: Optional[float] = None) -> float:
    """Read a numeric value from the `design` section.

    Falls back to the caller's ``default``, then to :data:`DESIGN_DEFAULTS`.
    Only a key in neither place raises.
    """
    design = config.get("design", {}) or {}
    if key not in design or design[key] is None:
        if default is None:
            default = DESIGN_DEFAULTS.get(key)
        if default is None:
            raise KeyError(f"Missing required design value: {key}")
        return float(default)
    return float(design[key])


def building_labels(config: Mapping[str, Any]) -> List[str]:
    return [str(b["label"]) for b in config.get("buildings", [])]


def building_design_loads(config: Mapping[str, Any]) -> List[float]:
    loads = [building_design_load_W(b) for b in config.get("buildings", [])]
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
    """Design flow entering the root branch: every building plus every terminal."""
    return network_design_mass_flows(config)[""]["inlet"]


def design_evaporator_load(config: Mapping[str, Any]) -> float:
    """Design plant duty used to size the main chiller.

    Under ``plant_control="supply_temperature"`` this is only a starting
    figure. The solved duty comes out of the loop balance: above this by the
    pipe and storage gains, below it by whatever the satellite plants carry.
    """
    return sum(building_design_loads(config)) + default_pump_power_W(config)


# Design mass flows across the branch tree

def network_design_mass_flows(
    config: Mapping[str, Any],
    root: Optional["BranchSpec"] = None,
    building_flows: Optional[Sequence[float]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Design mass flow of every pipe, building leg and terminal in the network.

    Returns a mapping keyed by branch label, plus the key ``""`` aliasing the
    root branch so callers that do not know the label can still ask for the
    network total. Each entry holds ``pipe`` (by local pipe key), ``buildings``
    (in branch order), ``terminals`` (by terminal label) and ``inlet``.

    Flow is determined bottom-up: a terminal that is a sub-branch carries
    whatever its own subtree consumes, so only bypasses and satellite plants
    need a number in the scenario file. That is also exactly the set of mass
    flows TESPy needs specified, which is why the two lists always agree.
    """
    if root is None:
        root = make_branch_specs(config)
    if building_flows is None:
        building_flows = compute_building_mass_flows(config)

    out: Dict[str, Dict[str, Any]] = {}

    def visit(branch: "BranchSpec") -> float:
        flows = [float(building_flows[i]) for i in branch.building_indices]
        terminals: Dict[str, float] = {}
        for terminal in branch.terminals:
            if terminal.child is not None:
                terminals[terminal.label] = visit(terminal.child)
            else:
                terminals[terminal.label] = float(terminal.mass_flow_kg_s or 0.0)
        end_flow = sum(terminals.values())
        # Supply pipe i and return pipe i both carry the buildings from tap i
        # onwards plus whatever leaves through the end of the line.
        pipe: Dict[str, float] = {}
        for i in range(1, branch.n_pipes_per_side + 1):
            carried = sum(flows[i - 1:]) + end_flow
            pipe[f"supply_{i}"] = carried
            pipe[f"return_{i}"] = carried
        inlet = sum(flows) + end_flow
        out[branch.label] = {
            "pipe": pipe,
            "buildings": flows,
            "terminals": terminals,
            "end_of_line": end_flow,
            "inlet": inlet,
        }
        return inlet

    visit(root)
    out[""] = out[root.label]
    return out


# Pipe hydraulics

def _darcy_pressure_ratio_from_length(
    config: Mapping[str, Any],
    pipe_key: str,
    raw: Mapping[str, Any],
    mass_flow: float,
    hydraulic: Optional[Mapping[str, Any]] = None,
) -> float:
    """Calculate an equivalent pressure ratio from L, D and ks using Darcy-Weisbach.

    .. note::
       This freezes the pressure drop at the **design** mass flow. Real pressure
       drop scales roughly with the square of flow, so at part load this model
       overstates the drop. It exists because a fixed ``pr`` is far easier for
       the solver than the full Darcy group; use ``pipe_model: darcy`` when the
       part-load hydraulics matter.
    """
    hydraulic = hydraulic or {}
    pressure_bar = float(
        hydraulic.get("design_pressure_bar", design_value(config, "chilled_water_pressure_bar"))
    )
    rho = float(hydraulic.get("fluid_density_kg_m3", 999.0))
    mu = float(hydraulic.get("dynamic_viscosity_Pa_s", 0.0013))
    min_pr = float(hydraulic.get("min_pr", 0.90))
    max_pr = float(hydraulic.get("max_pr", 0.9999))
    length = float(raw["L"])
    diameter = float(raw["D"])
    roughness = float(raw["ks"])
    if length <= 0 or diameter <= 0 or roughness < 0:
        raise ValueError(
            f"Invalid length-derived pipe values for {pipe_key}: L and D must be positive and "
            "ks non-negative."
        )
    mass_flow = max(float(mass_flow), 1e-9)
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


def branch_pipe_attrs(
    branch: "BranchSpec",
    config: Mapping[str, Any],
    design_flows: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Dict[str, float]]:
    """Translate one branch's YAML pipe specifications into TESPy Pipe attributes.

    Supported ``pipe_model`` values are ``pressure_ratio``, ``darcy``, and
    ``length_derived_pr``. A child branch inherits its parent's ``pipe_model``
    unless it names its own.

    ``Q`` is only forwarded for pipes whose heat model is ``adiabatic`` or
    ``fixed``. A pipe using a ``UA`` or native TESPy heat model gets its energy
    equation from :meth:`Branch._apply_pipe_thermal` instead, and specifying both
    would over-determine that pipe.
    """
    pipe_model = str(branch.setting("pipe_model", "pressure_ratio")).lower().replace("-", "_")
    if pipe_model not in {"pressure_ratio", "darcy", "length_derived_pr"}:
        raise ValueError(
            f"Branch {branch.label!r}: pipe_model must be 'pressure_ratio', 'darcy', or "
            "'length_derived_pr'."
        )
    default_heat = str(branch.setting("heat_model", "adiabatic")).lower()
    hydraulic = branch.setting("hydraulic", {}) or {}
    if design_flows is None:
        design_flows = network_design_mass_flows(config)
    flows = design_flows.get(branch.label, {}).get("pipe", {})

    attrs: Dict[str, Dict[str, float]] = {}
    for key, raw in branch.pipes.items():
        if raw is None:
            attrs[str(key)] = {}
            continue
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"Pipe specification for {key!r} on branch {branch.label!r} must be a mapping."
            )
        heat_model = str(raw.get("heat_model", default_heat)).lower()
        pipe_attrs: Dict[str, float] = {}
        if heat_model in {"adiabatic", "fixed"}:
            if "Q" in raw:
                pipe_attrs["Q"] = float(raw["Q"])
            elif "Q_W" in raw:
                pipe_attrs["Q"] = float(raw["Q_W"])
        if pipe_model == "pressure_ratio":
            if "pr" in raw and raw["pr"] is not None:
                pipe_attrs["pr"] = float(raw["pr"])
        elif pipe_model == "length_derived_pr":
            if {"L", "D", "ks"}.issubset(raw):
                pipe_attrs["pr"] = _darcy_pressure_ratio_from_length(
                    config, str(key), raw, flows.get(str(key), 0.0), hydraulic
                )
            elif "pr" in raw and raw["pr"] is not None:
                pipe_attrs["pr"] = float(raw["pr"])
            elif any(attr in raw for attr in ("L", "D", "ks")):
                # L and D alone are legitimate for a UA heat model; only complain
                # when the intent was clearly hydraulic.
                if "ks" in raw:
                    raise ValueError(
                        f"Length-derived pipe {key} on branch {branch.label!r} must provide "
                        "L, D, and ks together, or none of them."
                    )
        else:
            for attr in ("L", "D", "ks"):
                if attr in raw and raw[attr] is not None:
                    pipe_attrs[attr] = float(raw[attr])
            if {"L", "D", "ks"}.issubset(pipe_attrs):
                if pipe_attrs["L"] <= 0 or pipe_attrs["D"] <= 0 or pipe_attrs["ks"] < 0:
                    raise ValueError(
                        f"Invalid Darcy pipe values for {key} on branch {branch.label!r}: L and D "
                        "must be positive and ks non-negative."
                    )
            elif any(attr in pipe_attrs for attr in ("L", "D", "ks")):
                raise ValueError(
                    f"Darcy pipe {key} on branch {branch.label!r} must provide L, D, and ks "
                    "together, or none of them."
                )
        attrs[str(key)] = pipe_attrs
    return attrs


def make_pipe_attrs(config: Mapping[str, Any]) -> Dict[str, Dict[str, float]]:
    """TESPy pipe attributes for the scenario's *root* branch.

    Use :func:`make_network_pipe_attrs` to get the same thing for every branch of
    a forking network.
    """
    return branch_pipe_attrs(make_branch_specs(config), config)


def make_network_pipe_attrs(config: Mapping[str, Any]) -> Dict[str, Dict[str, Dict[str, float]]]:
    """TESPy pipe attributes for every branch, keyed by branch label."""
    root = make_branch_specs(config)
    design_flows = network_design_mass_flows(config, root)
    return {b.label: branch_pipe_attrs(b, config, design_flows) for b in root.walk()}


def branch_pipe_lengths(branch: "BranchSpec") -> Dict[str, float]:
    """Length in metres of every pipe that names one.

    A return pipe inherits its supply counterpart's length unless it names its
    own, because supply and return follow the same street.
    """
    lengths: Dict[str, float] = {}
    for key, raw in branch.pipes.items():
        if isinstance(raw, Mapping) and raw.get("L") is not None:
            lengths[str(key)] = float(raw["L"])
    for i in range(1, branch.n_pipes_per_side + 1):
        supply, ret = f"supply_{i}", f"return_{i}"
        if ret not in lengths and supply in lengths:
            lengths[ret] = lengths[supply]
        if supply not in lengths and ret in lengths:
            lengths[supply] = lengths[ret]
    return lengths


def branch_pipe_headings(branch: "BranchSpec") -> Dict[str, float]:
    """Plan-view bearing of every pipe that names one, in degrees CCW from east.

    A return pipe inherits its supply counterpart's bearing unless it names its
    own, because supply and return follow the same street.
    """
    headings: Dict[str, float] = {}
    for key, raw in branch.pipes.items():
        if isinstance(raw, Mapping) and raw.get("heading_deg") is not None:
            headings[str(key)] = float(raw["heading_deg"])
    for i in range(1, branch.n_pipes_per_side + 1):
        supply, ret = f"supply_{i}", f"return_{i}"
        if ret not in headings and supply in headings:
            headings[ret] = headings[supply]
        if supply not in headings and ret in headings:
            headings[supply] = headings[ret]
    return headings


def set_start(conn: Connection, m: float, T: float, p: float) -> None:
    """Set starting values on a TESPy connection."""
    conn.m.set_val0(float(m))
    conn.T.set_val0(float(T))
    conn.p.set_val0(float(p))


# Network assembly

class DistrictCoolingSystem:
    """Everything a scenario needs to run, in one place.

    Returned by :func:`build_system`. It is also iterable as the six-tuple the
    pre-upgrade :func:`build_standard_branch_system` returned, so existing code
    keeps working:

    >>> nw, chiller, buildings, branch, tower, conn = build_standard_branch_system(cfg)
    """

    def __init__(
        self,
        network: Network,
        chiller: Chiller,
        buildings: List[Building],
        branch: Branch,
        cooling_tower: CoolingTower,
        chiller_to_closer: Connection,
        storage: Optional[ColdStorage] = None,
        plant_control: str = "evaporator_duty",
        notes: Optional[List[str]] = None,
        branch_spec: Optional[BranchSpec] = None,
        config: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.network = network
        self.chiller = chiller
        self.buildings = buildings
        self.branch = branch
        self.cooling_tower = cooling_tower
        self.chiller_to_closer = chiller_to_closer
        self.storage = storage
        self.plant_control = plant_control
        self.notes = notes or []
        #: Normalised scenario tree, kept so the layout plotter can read the
        #: geometry the solver has no use for.
        self.branch_spec = branch_spec
        #: The scenario this was built from, so a solved system can still say
        #: which one it is. The reporting module titles its figures from it.
        self.config: Dict[str, Any] = dict(config or {})

    @property
    def name(self) -> str:
        """The scenario's name, falling back to the root branch label."""
        meta = self.config.get("metadata") or {}
        if meta.get("name"):
            return str(meta["name"])
        path = self.config.get("_config_path")
        if path:
            return Path(path).stem
        return str(getattr(self.branch, "label", "district cooling system"))

    @property
    def building_map(self) -> Dict[str, Building]:
        return {b.label: b for b in self.buildings}

    @property
    def branches(self) -> List[Branch]:
        """Every branch in the network, root first, depth-first by terminal order."""
        return list(self.branch.walk())

    @property
    def satellite_plants(self) -> List[SatellitePlant]:
        """Every distributed plant hanging off the end of a branch."""
        return self.branch.satellite_plants()

    @property
    def chillers(self) -> List[Chiller]:
        """Main plant first, then every satellite plant's chiller."""
        return [self.chiller] + [p.chiller for p in self.satellite_plants]

    def __iter__(self):
        return iter(
            (
                self.network,
                self.chiller,
                self.buildings,
                self.branch,
                self.cooling_tower,
                self.chiller_to_closer,
            )
        )


def make_chiller(
    cfg: Mapping[str, Any],
    label: str,
    Q_evap_W: float,
    native_offdesign_default: bool = True,
) -> Chiller:
    """Build a :class:`~discoolpy.chiller.Chiller` from a ``chiller`` mapping.

    Shared by the main plant and by every satellite plant, so a distributed
    chiller is described with exactly the same keys as the central one.
    """
    cfg = dict(cfg or {})
    pr = cfg.get("pressure_ratios", {}) or {}
    chill = Chiller(
        cfg.get("label", label),
        T_evap=float(cfg.get("T_evap_degC", 2.0)),
        T_cond=float(cfg.get("T_cond_degC", 46.0)),
        eta_s=float(cfg.get("eta_s", 0.75)),
        Q_evap=float(Q_evap_W),
        refrigerant=cfg.get("refrigerant", "R134a"),
        pr_evap_1=float(pr.get("evap_1", 0.999)),
        pr_evap_2=float(pr.get("evap_2", 0.999)),
        pr_cond_1=float(pr.get("cond_1", 0.999)),
        pr_cond_2=float(pr.get("cond_2", 1.0)),
    )
    offdesign_cfg = cfg.get("native_offdesign", {}) or {}
    if offdesign_cfg.get("enabled", native_offdesign_default):
        chill.configure_native_offdesign(
            evaporator_ttd_l=offdesign_cfg.get("evaporator_ttd_l"),
            condenser_ttd_u=offdesign_cfg.get("condenser_ttd_u"),
            use_pressure_loss_characteristics=bool(
                offdesign_cfg.get("use_pressure_loss_characteristics", False)
            ),
        )
    return chill


def make_cooling_tower(cfg: Mapping[str, Any], config: Mapping[str, Any], label: str) -> CoolingTower:
    """Build a condenser-water loop, falling back to the scenario's design values."""
    cfg = dict(cfg or {})
    t_in = design_value(config, "condenser_inlet_temperature_degC")
    ambient = design_value(config, "ambient_temperature_degC")
    return CoolingTower(
        cfg.get("label", label),
        T_in_chiller=float(cfg.get("condenser_inlet_temperature_degC", t_in)),
        T_out_chiller=float(
            cfg.get(
                "condenser_outlet_temperature_degC",
                design_value(config, "condenser_outlet_temperature_degC"),
            )
        ),
        p_in_chiller=float(
            cfg.get("condenser_pressure_bar", design_value(config, "condenser_pressure_bar"))
        ),
        fluid=cfg.get("fluid", {"water": 1.0}),
        pr=cfg.get("pr"),
        ambient_temperature=ambient,
        approach_temperature=float(cfg.get("approach_temperature_K", t_in - ambient)),
    )


def _satellite_duty_W(raw: Mapping[str, Any], label: str) -> float:
    if raw.get("Q_evap_W") is not None:
        return float(raw["Q_evap_W"])
    if raw.get("Q_evap_kW") is not None:
        return float(raw["Q_evap_kW"]) * 1000.0
    raise ValueError(
        f"Satellite plant {label!r} needs a duty: give 'Q_evap_W' or 'Q_evap_kW'. The main "
        "plant's duty is the loop's one free energy variable, so a satellite's duty has to be "
        "asserted, as a dispatch decision."
    )


def _build_branch_tree(
    spec: "BranchSpec",
    config: Mapping[str, Any],
    buildings: Sequence[Building],
    pipe_attrs: Mapping[str, Dict[str, Dict[str, float]]],
    pipe_thermal: Mapping[str, Dict[str, Any]],
    design_flows: Mapping[str, Any],
    satellites: List[SatellitePlant],
) -> Branch:
    """Instantiate the :class:`Branch` tree described by ``spec``, depth first."""
    terminals: List[Terminal] = []
    flows = design_flows[spec.label]["terminals"]
    for terminal_spec in spec.terminals:
        m = flows.get(terminal_spec.label)
        if terminal_spec.kind == "branch":
            child = _build_branch_tree(
                terminal_spec.child, config, buildings, pipe_attrs, pipe_thermal,
                design_flows, satellites,
            )
            terminals.append(
                SubBranch(label=child.label, m=m, heading_deg=terminal_spec.heading_deg,
                          branch=child)
            )
        elif terminal_spec.kind == "plant":
            raw = terminal_spec.raw
            duty = _satellite_duty_W(raw, terminal_spec.label)
            plant = SatellitePlant(
                label=terminal_spec.label,
                m=m,
                heading_deg=terminal_spec.heading_deg,
                chiller=make_chiller(
                    raw.get("chiller", {}) or {},
                    label=f"{terminal_spec.label} chiller",
                    Q_evap_W=duty,
                ),
                cooling_tower=make_cooling_tower(
                    raw.get("cooling_tower", {}) or {},
                    config,
                    label=f"{terminal_spec.label} cooling tower",
                ),
                storage=make_storage_from_section(raw.get("storage")),
                balancing_dp=terminal_spec.dp,
                dispatch=str(raw.get("dispatch", "fixed")).lower(),
                min_load_fraction=float(raw.get("min_load_fraction", 0.2)),
                design_Q_evap_W=duty,
            )
            satellites.append(plant)
            terminals.append(plant)
        else:
            terminals.append(
                EndBypass(
                    label=terminal_spec.label,
                    m=m,
                    heading_deg=terminal_spec.heading_deg,
                    dp=terminal_spec.dp,
                )
            )

    branch = Branch(
        spec.label,
        buildings=[buildings[i] for i in spec.building_indices],
        terminals=terminals,
        pump_placement=spec.raw.get("pump_placement", "supply_inlet" if spec.is_root else None),
        pump_label=spec.raw.get("pump_label", "main pump" if spec.is_root else None),
        uniform_pipes=False,
        pipe_attrs=dict(pipe_attrs[spec.label]),
        pipe_thermal=dict(pipe_thermal[spec.label]),
        pipe_headings=branch_pipe_headings(spec),
        pipe_lengths=branch_pipe_lengths(spec),
        origin=spec.origin_m,
        heading_deg=spec.heading_deg,
    )
    branch.design_building_mass_flows = list(design_flows[spec.label]["buildings"])
    return branch


def _topology_from_specs(
    spec: "BranchSpec",
    buildings: Sequence[Building],
    pipe_attrs: Mapping[str, Dict[str, Dict[str, float]]],
) -> BranchTopology:
    """Project the normalised scenario onto the solver-free pressure graph."""
    node = BranchTopology(
        label=spec.label,
        building_pr=[buildings[i].pr for i in spec.building_indices],
        pipe_attrs=dict(pipe_attrs[spec.label]),
    )
    for terminal in spec.terminals:
        pressure_spec = terminal.pressure_spec()
        node.terminals.append(
            TerminalTopology(
                key=terminal.label,
                kind=terminal.kind,
                specified=pressure_spec is not None,
                spec=pressure_spec or "free",
                releasable=terminal.releasable,
                child=(
                    None
                    if terminal.child is None
                    else _topology_from_specs(terminal.child, buildings, pipe_attrs)
                ),
            )
        )
    return node


def network_topology(config: Mapping[str, Any]) -> BranchTopology:
    """The scenario's pressure graph, without building a single TESPy component.

    Hand the result to :func:`~discoolpy.hydraulics.analyse_network_topology` to
    find out whether the specification is well posed before committing to a
    solve. This is the check :func:`build_system` runs for you; it is exposed
    separately because reading its report is much faster than reading a TESPy
    cycle dump.
    """
    root = make_branch_specs(config)
    pipe_attrs = {b.label: branch_pipe_attrs(b, config) for b in root.walk()}
    return _topology_from_specs(root, make_buildings(config), pipe_attrs)


def _apply_relaxations(
    actions: Sequence[Mapping[str, Any]],
    spec: "BranchSpec",
    buildings: Sequence[Building],
) -> None:
    """Push the resolver's decisions back onto the objects the builder will use."""
    by_label = {b.label: b for b in spec.walk()}
    for action in actions:
        branch = by_label[action["branch"]]
        if action["target"] == "building":
            buildings[branch.building_indices[action["index"]]].pr = None
        else:
            for terminal in branch.terminals:
                if terminal.label == action["key"]:
                    terminal.dp = None


def check_stratified_temperatures(
    config: Mapping[str, Any], storage: Optional[ColdStorage]
) -> List[str]:
    """Warn when a stratified tank's temperatures cannot be reached in practice.

    A stratified tank charges to whatever the plant supplies and discharges to
    whatever the district returns. If its ``charged_temperature_degC`` sits below
    the supply setpoint the tank can never fill, and if its
    ``discharged_temperature_degC`` sits above the return it can never empty.
    Either way it drifts isothermal and quietly stops storing anything.

    A scalar store tolerated this mismatch because its temperatures only fed the
    UA loss term. Here they are structural, so it is worth saying out loud
    rather than leaving as a puzzling flat state of charge.
    """
    if storage is None or not storage.is_stratified:
        return []
    tank = storage.stratified
    supply = design_value(config, "supply_temperature_degC")
    building_return = design_value(config, "building_return_temperature_degC")

    notes: List[str] = []
    if tank.charged_temperature_degC < supply - 0.5:
        notes.append(
            f"Stratified tank {storage.label!r} is configured to charge to "
            f"{tank.charged_temperature_degC:.1f} degC but the plant supplies "
            f"{supply:.1f} degC, so it can never reach that state and its usable capacity "
            "will be smaller than configured. Set charged_temperature_degC to the supply "
            "setpoint."
        )
    if tank.discharged_temperature_degC > building_return + 0.5:
        notes.append(
            f"Stratified tank {storage.label!r} is configured to discharge to "
            f"{tank.discharged_temperature_degC:.1f} degC but the district returns "
            f"{building_return:.1f} degC, so the last part of its capacity is unreachable. "
            "Set discharged_temperature_degC to the design return temperature."
        )
    for note in notes:
        warnings.warn(note, stacklevel=3)
    return notes


def build_system(config: Mapping[str, Any], strict_hydraulics: bool = True) -> DistrictCoolingSystem:
    """Build the district-cooling network described by a scenario.

    The network is a tree of branches. The main plant feeds the root, and each
    branch ends in one or more terminals: a bypass valve, a satellite plant, or
    another branch. Name no ``terminals`` at all and you get a single street
    ending in one bypass, which is what most scenarios want.
    """
    notes: List[str] = []
    plant_control = plant_control_mode(config)

    buildings = make_buildings(config)
    root_spec = make_branch_specs(config)
    building_flows = compute_building_mass_flows(config)
    design_flows = network_design_mass_flows(config, root_spec, building_flows)

    pipe_attrs = {b.label: branch_pipe_attrs(b, config, design_flows) for b in root_spec.walk()}
    pipe_thermal = {b.label: branch_pipe_thermal(b, config) for b in root_spec.walk()}

    # --- structural checks before TESPy sees anything -----------------------
    topology = _topology_from_specs(root_spec, buildings, pipe_attrs)
    if bool((config.get("branch", {}) or {}).get("auto_relax_pressure", False)):
        actions, relax_notes = resolve_network_pressure_specification(topology)
        _apply_relaxations(actions, root_spec, buildings)
        notes.extend(relax_notes)
        topology = _topology_from_specs(root_spec, buildings, pipe_attrs)
    validate_network_topology(topology, strict=strict_hydraulics)

    # A pipe's energy equation comes either from an explicit Q (adiabatic pipes
    # get Q = 0 that way) or from a non-adiabatic heat model applied by the
    # branch. Anything else leaves the pipe's duty free.
    pipe_keys: List[str] = []
    energy_specified: List[str] = []
    for spec in root_spec.walk():
        for key in spec.pipe_keys:
            qualified = spec.qualify(key)
            pipe_keys.append(qualified)
            thermal = pipe_thermal[spec.label].get(key)
            if "Q" in pipe_attrs[spec.label].get(key, {}) or (
                thermal is not None and thermal.model != "adiabatic"
            ):
                energy_specified.append(qualified)
    validate_thermal_degrees_of_freedom(pipe_keys, energy_specified, plant_control)

    chiller_cfg = config.get("chiller", {}) or {}
    chill = make_chiller(
        chiller_cfg, label="yaml_config_chiller", Q_evap_W=design_evaporator_load(config)
    )

    satellites: List[SatellitePlant] = []
    branch = _build_branch_tree(
        root_spec, config, buildings, pipe_attrs, pipe_thermal, design_flows, satellites
    )

    cooling_tower = make_cooling_tower(
        config.get("cooling_tower", {}) or {}, config, label="yaml cooling tower"
    )

    nw = Network(iterinfo=bool((config.get("solver", {}) or {}).get("iterinfo", False)))
    nw.units.set_defaults(
        temperature="degC",
        power="W",
        pressure="bar",
        pressure_difference="bar",
        enthalpy="kJ/kg",
    )

    cc_chw = CycleCloser(
        (config.get("network", {}) or {}).get(
            "chilled_water_cycle_closer", "chilled water cycle closer"
        )
    )
    c_chiller_to_cc = Connection(chill, "out1", cc_chw, "in1", label="chiller_to_cyclecloser")

    storage = make_storage(config)
    notes.extend(check_stratified_temperatures(config, storage))
    storage_conns: Tuple[Connection, ...] = ()
    if storage is not None and storage.coupling == "hydraulic":
        # The store sits in the plant section, between the cycle closer and the
        # branch supply, so it carries a real heat flow rather than a rescaling
        # of the building duties. Charging then forces the chiller to produce
        # water below the distribution setpoint, which is the efficiency
        # penalty a supervisory model cannot see.
        storage.create_hydraulic_element()
        storage_conns = (
            Connection(cc_chw, "out1", storage.heat_exchanger, "in1", label="cyclecloser_to_storage"),
        )
        branch.connect_between(
            storage.heat_exchanger,
            "out1",
            chill,
            "in1",
            inlet_label="storage_to_pump",
            outlet_label="return_pipe_1_to_chiller",
        )
        storage.set_design(idle_Q_W=0.0)
    else:
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
    if storage_conns:
        nw.add_conns(*storage_conns)
    branch.add_to_network(nw)
    nw.add_conns(*cooling_tower.connections)
    nw.add_subsystems(chill)

    # Fluid, supply-temperature setpoint and pressure anchor all sit on
    # `branch_in`, downstream of the store, because that is what the district
    # actually receives. The store's own inlet floats, which is the point:
    # charging pulls the plant's leaving water below the distribution setpoint.
    apply_design_specifications(config, branch, cooling_tower)
    apply_default_starting_values(config, c_chiller_to_cc, buildings, branch, cooling_tower)
    if storage_conns:
        set_start(
            storage_conns[0],
            total_design_mass_flow(config),
            design_value(config, "supply_temperature_degC"),
            design_value(config, "chilled_water_pressure_bar"),
        )

    if plant_control == "supply_temperature":
        chill.release_Q_evap()
        notes.append(
            "Chiller duty is a solved output (plant_control='supply_temperature'); it equals "
            "building demand + pipe gains + pump heat + storage exchange - satellite plant duty."
        )
    if satellites:
        notes.append(
            f"{len(satellites)} satellite plant(s) carry "
            f"{sum(p.chiller._Q_evap for p in satellites) / 1e3:.1f} kW of the district duty; "
            "their evaporator duties stay asserted because the loop has only one free energy "
            "variable and that is the main plant."
        )

    return DistrictCoolingSystem(
        network=nw,
        chiller=chill,
        buildings=buildings,
        branch=branch,
        cooling_tower=cooling_tower,
        chiller_to_closer=c_chiller_to_cc,
        storage=storage,
        plant_control=plant_control,
        notes=notes,
        branch_spec=root_spec,
        config=config,
    )


def build_standard_branch_system(
    config: Mapping[str, Any],
) -> Tuple[Network, Chiller, List[Building], Branch, CoolingTower, Connection]:
    """Backward-compatible wrapper returning the original six-tuple."""
    return tuple(build_system(config))


def default_pump_power_W(config: Mapping[str, Any]) -> float:
    """Design pump shaft power, from the scenario or from a nominal head.

    Zero is not a usable fallback here: a pump that does no work has no
    isentropic efficiency, and TESPy divides by the enthalpy rise to get one.
    So when a scenario says nothing, size the pump from the flow it has to move
    against :data:`NOMINAL_PUMP_HEAD_BAR`, which at least converges and lands
    in the right order of magnitude.
    """
    design = config.get("design", {}) or {}
    if design.get("pump_power_W") is not None:
        return float(design["pump_power_W"])
    flow = total_design_mass_flow(config)
    eta = design_value(config, "pump_efficiency", 0.70)
    return flow * NOMINAL_PUMP_HEAD_BAR * 1e5 / (1000.0 * max(eta, 1e-6))


def apply_design_specifications(
    config: Mapping[str, Any], branch: Branch, cooling_tower: CoolingTower
) -> None:
    """Apply design specifications to the branch tree and the cooling tower."""
    branch.connections["branch_in"].set_attr(
        fluid={"water": 1.0},
        T=design_value(config, "supply_temperature_degC"),
        p=design_value(config, "chilled_water_pressure_bar"),
    )
    branch_cfg = config.get("branch", {}) or {}
    pump_attrs = {"eta_s": design_value(config, "pump_efficiency", 0.7)}
    pipe_model = str(branch_cfg.get("pipe_model", "pressure_ratio")).lower().replace("-", "_")
    fix_pump_power_default = pipe_model != "darcy"
    if bool(branch_cfg.get("fix_pump_power", fix_pump_power_default)):
        pump_attrs["P"] = default_pump_power_W(config)
    elif "pump_pressure_ratio" in branch_cfg:
        pump_attrs["pr"] = float(branch_cfg["pump_pressure_ratio"])
    branch.set_design(
        pump_attrs=pump_attrs,
        native_offdesign=bool(branch_cfg.get("native_offdesign", False)),
    )
    cooling_tower.set_design(
        native_offdesign=bool((config.get("cooling_tower", {}) or {}).get("native_offdesign", False))
    )


def apply_default_starting_values(
    config: Mapping[str, Any],
    c_chiller_to_cc: Connection,
    buildings: Sequence[Building],
    branch: Branch,
    cooling_tower: CoolingTower,
) -> None:
    """Seed robust starting values across the whole branch tree.

    Pressure is walked down the tree from the branch inlet: every tap loses one
    ``initial_pressure_step_bar``, every building leg a nominal 0.15 bar, and a
    sub-branch continues from wherever its parent's end of line got to. Getting
    these roughly right matters much more once the network forks, because a
    Newton solver started with every pressure equal has no gradient to tell the
    parallel paths apart.
    """
    t_supply = design_value(config, "supply_temperature_degC")
    t_return = design_value(config, "building_return_temperature_degC")
    p_supply = design_value(config, "chilled_water_pressure_bar")
    p_step = float((config.get("solver", {}) or {}).get("initial_pressure_step_bar", 0.05))
    leg_drop = 0.15

    inlet_flow = _subtree_design_flow(branch)

    set_start(c_chiller_to_cc, inlet_flow, t_supply, p_supply)

    def seed(node: Branch, p_in: float) -> float:
        """Seed one branch and its children; returns its end-of-line pressure."""
        flows = node.design_building_mass_flows or [0.0] * node.n_buildings
        total = _subtree_design_flow(node)
        node.set_connection_start("branch_in", total, t_supply, p_in)
        p_header = p_in
        if "pump_to_supply_1" in node.connections:
            p_header = p_in + 0.2
            node.set_connection_start("pump_to_supply_1", total, t_supply, p_header)
        if "supply_1_to_splitter_1" in node.connections:
            node.set_connection_start(
                "supply_1_to_splitter_1", total, t_supply, p_header - p_step
            )

        remaining = total
        p_tap = p_header - p_step
        for i, building in enumerate(node.buildings, start=1):
            m_b = flows[i - 1]
            p_out = p_tap * building.pr if building.pr is not None else p_tap - leg_drop
            building.set_start(m_b, t_supply, t_return, p_tap, p_out)
            remaining -= m_b
            if i < node.n_buildings:
                node.set_connection_start(
                    f"splitter_{i}_to_supply_{i + 1}", remaining, t_supply, p_tap
                )
                node.set_connection_start(
                    f"supply_{i + 1}_to_splitter_{i + 1}", remaining, t_supply, p_tap - p_step
                )
                p_tap -= p_step

        p_end = p_tap if node.buildings else p_header - p_step
        if "end_split_in" in node.connections:
            node.set_connection_start(
                "end_split_in", remaining, t_supply, p_end
            )
        p_end_return = p_end - leg_drop
        for terminal in node.terminals:
            if isinstance(terminal, SubBranch):
                seed(terminal.branch, p_end)
            else:
                terminal.start_values(terminal.m or 0.0, t_supply, p_end, leg_drop)
        if "end_merge_out" in node.connections:
            node.set_connection_start(
                "end_merge_out", remaining, t_return, p_end_return
            )

        cumulative = total - sum(flows)
        p_ret = p_end_return
        for i in range(node.n_pipes_per_side, 0, -1):
            if node.buildings:
                cumulative += flows[i - 1]
                node.set_connection_start(
                    f"merge_{i}_to_return_{i}", cumulative, t_return, p_ret
                )
            if i > 1:
                node.set_connection_start(
                    f"return_{i}_to_merge_{i - 1}", cumulative, t_return, p_ret - p_step
                )
                p_ret -= p_step
        node.set_connection_start("branch_out", total, t_return, max(0.5, p_ret - p_step))
        return p_end

    seed(branch, p_supply)
    cooling_tower.set_start(
        m=float((config.get("cooling_tower", {}) or {}).get("start_mass_flow_kg_s", 45.0)),
        pr_start=0.999,
    )


def _subtree_design_flow(node: Branch) -> float:
    """Design mass flow entering ``node``: its own buildings plus its terminals."""
    total = sum(node.design_building_mass_flows or [])
    for terminal in node.terminals:
        if isinstance(terminal, SubBranch):
            total += _subtree_design_flow(terminal.branch)
        else:
            total += float(terminal.m or 0.0)
    return total


def make_storage_from_config(config: Mapping[str, Any]) -> Optional[ColdStorage]:
    """Create a ColdStorage instance when the YAML storage section is enabled."""
    return make_storage(config)


# Synthetic profiles

# Occupancy archetypes available to profiles.archetypes.
OCCUPANCY_ARCHETYPES: Dict[str, Dict[str, float]] = {
    "office": {"base": 0.32, "weather": 0.43, "solar": 0.17, "occupancy": 0.18,
               "floor": 0.35, "weekend": 0.92},
    "hotel": {"base": 0.48, "weather": 0.35, "solar": 0.08, "occupancy": 0.16,
              "floor": 0.42, "weekend": 1.00},
    "retail": {"base": 0.25, "weather": 0.42, "solar": 0.18, "occupancy": 0.23,
               "floor": 0.32, "weekend": 1.08},
    "residential": {"base": 0.35, "weather": 0.40, "solar": 0.10, "occupancy": 0.20,
                    "floor": 0.30, "weekend": 1.02},
    "hospital": {"base": 0.60, "weather": 0.25, "solar": 0.06, "occupancy": 0.10,
                 "floor": 0.55, "weekend": 1.00},
    "datacentre": {"base": 0.85, "weather": 0.10, "solar": 0.01, "occupancy": 0.04,
                   "floor": 0.80, "weekend": 1.00},
}


def _occupancy_fraction(kind: str, hour: float) -> float:
    """Normalised occupancy-driven load shape for one archetype."""
    if kind == "office":
        return 0.25 + 0.75 / (1 + math.exp(-(hour - 8.0))) * (1 - 1 / (1 + math.exp(-(hour - 18.0))))
    if kind == "hotel":
        return 0.55 + 0.20 * math.sin(2 * math.pi * (hour - 18.0) / 24.0) + 0.10 * math.sin(4 * math.pi * hour / 24.0)
    if kind == "retail":
        return 0.18 + 0.82 / (1 + math.exp(-(hour - 10.0))) * (1 - 1 / (1 + math.exp(-(hour - 23.0))))
    if kind == "residential":
        return 0.45 + 0.35 * math.sin(2 * math.pi * (hour - 19.0) / 24.0)
    if kind == "hospital":
        return 0.80 + 0.15 * math.sin(2 * math.pi * (hour - 13.0) / 24.0)
    if kind == "datacentre":
        return 1.0
    raise ValueError(f"Unknown occupancy archetype {kind!r}. Choose from {sorted(OCCUPANCY_ARCHETYPES)}.")


def make_weather_and_load_profiles(config: Mapping[str, Any]) -> pd.DataFrame:
    """Generate a synthetic weather and demand profile for **any** number of buildings.

    Each building picks an archetype (``office``, ``hotel``, ``retail``,
    ``residential``, ``hospital``, ``datacentre``) either from
    ``profiles.archetypes[<label>]`` or from its own ``archetype`` key; the
    default cycles through office/hotel/retail so a three-building scenario
    reproduces the original generator's intent.

    Alongside dry-bulb air, the frame carries **ground temperature** (from the
    Kusuda harmonic at the configured burial depth) and **solar irradiance**,
    because buried pipes and building envelopes are driven by those rather than
    by air temperature alone.
    """
    profile_cfg = config.get("profiles", {}) or {}
    start = profile_cfg.get("start", "2026-07-01 00:00:00")
    periods = int(profile_cfg.get("periods", 7 * 48))
    freq = profile_cfg.get("freq", "30min")
    timestamps = pd.date_range(start, periods=periods, freq=freq)
    daily_high = list(profile_cfg.get("daily_high_degC", [43.0, 44.0, 42.5, 45.0, 44.0, 43.5, 42.8]))
    daily_low = list(profile_cfg.get("daily_low_degC", [29.0, 30.0, 29.2, 31.0, 30.0, 29.5, 29.0]))
    peak_solar = float(profile_cfg.get("peak_solar_W_m2", 950.0))

    loads = building_design_loads(config)
    labels = building_labels(config)
    archetype_cfg = profile_cfg.get("archetypes", {}) or {}
    default_cycle = ["office", "hotel", "retail"]
    archetypes: List[str] = []
    for index, (label, item) in enumerate(zip(labels, config.get("buildings", []))):
        kind = archetype_cfg.get(label) or item.get("archetype") or default_cycle[index % len(default_cycle)]
        kind = str(kind).lower()
        if kind not in OCCUPANCY_ARCHETYPES:
            raise ValueError(
                f"Building {label!r} uses unknown archetype {kind!r}. "
                f"Choose from {sorted(OCCUPANCY_ARCHETYPES)}."
            )
        archetypes.append(kind)

    # Ground temperature: either given directly, or derived from the annual
    # harmonic at the configured burial depth.
    ground_fixed = profile_cfg.get("ground_temperature_degC")
    ground_cfg = profile_cfg.get("ground", {}) or {}
    depth = float(
        ground_cfg.get(
            "depth_m",
            ((config.get("branch", {}) or {}).get("thermal_defaults", {}) or {}).get("burial_depth_m", 1.0),
        )
    )
    mean_annual = float(ground_cfg.get("mean_annual_degC", 0.5 * (max(daily_high) + min(daily_low))))
    annual_amplitude = float(ground_cfg.get("annual_amplitude_K", 12.0))

    records: List[Dict[str, Any]] = []
    for ts in timestamps:
        day = (ts.date() - timestamps[0].date()).days % len(daily_high)
        hour = ts.hour + ts.minute / 60.0
        mean = 0.5 * (float(daily_high[day]) + float(daily_low[day]))
        amp = 0.5 * (float(daily_high[day]) - float(daily_low[day]))
        ambient = mean - amp * math.cos(2 * math.pi * (hour - 5.0) / 24.0)
        ambient += 0.35 * math.sin(2 * math.pi * day / max(1, len(daily_high)))
        solar = max(0.0, math.sin(math.pi * (hour - 6.0) / 13.0))
        weekend = ts.dayofweek in (4, 5)
        weather_index = max(0.0, min(1.25, (ambient - 27.0) / (45.0 - 27.0)))
        solar_index = max(0.0, min(1.0, solar))

        if ground_fixed is not None:
            ground = float(ground_fixed)
        else:
            ground = soil_temperature_degC(
                day_of_year=float(ts.dayofyear),
                mean_annual_degC=mean_annual,
                amplitude_K=annual_amplitude,
                depth_m=depth,
            )

        building_values: List[float] = []
        for label, kind, peak in zip(labels, archetypes, loads):
            weights = OCCUPANCY_ARCHETYPES[kind]
            occupancy = _occupancy_fraction(kind, hour)
            factor = (
                weights["base"]
                + weights["weather"] * weather_index
                + weights["solar"] * solar_index
                + weights["occupancy"] * occupancy
            )
            if weekend:
                factor *= weights["weekend"]
            building_values.append(max(weights["floor"] * peak, min(peak, factor * peak)))

        total = sum(building_values)
        load_fraction = total / sum(loads)
        condenser_in = max(30.0, min(39.0, ambient - 5.8 + 1.8 * load_fraction))
        record = {
            "timestamp": ts,
            "ambient_temperature_degC": ambient,
            "ground_temperature_degC": ground,
            "solar_irradiance_W_m2": peak_solar * solar_index,
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


def make_riyadh_weather_and_load_profiles(config: Mapping[str, Any]) -> pd.DataFrame:
    """Backward-compatible alias for :func:`make_weather_and_load_profiles`.

    The original function raised unless the scenario had exactly three
    buildings. It now works for any number.
    """
    return make_weather_and_load_profiles(config)


def make_schedule_from_profile(config: Mapping[str, Any], profile_df: pd.DataFrame, name: str) -> SnapshotSchedule:
    """Create a SnapshotSchedule from a profile dataframe and configured building labels."""
    labels = building_labels(config)
    optional = {}
    if "ground_temperature_degC" in profile_df.columns:
        optional["ground_temperatures"] = profile_df["ground_temperature_degC"].tolist()
    if "solar_irradiance_W_m2" in profile_df.columns:
        optional["solar_irradiances"] = profile_df["solar_irradiance_W_m2"].tolist()
    metadata_profile = {
        key: profile_df[key].tolist()
        for key in ("weather_index", "solar_index", "load_fraction")
        if key in profile_df.columns
    }
    for column in profile_df.columns:
        if column.endswith("_mass_mode") or column == "mass_mode":
            metadata_profile[column] = profile_df[column].tolist()
    return SnapshotSchedule.from_profile_arrays(
        start=profile_df["timestamp"].iloc[0].isoformat(),
        resolution=(config.get("profiles", {}) or {}).get("freq", "30min"),
        building_profiles={label: profile_df[f"{label}_Q_W"].tolist() for label in labels},
        name=name,
        ambient_temperatures=profile_df["ambient_temperature_degC"].tolist(),
        condenser_inlet_temperatures=profile_df["condenser_inlet_temperature_degC"].tolist(),
        metadata_profile=metadata_profile or None,
        **optional,
    )


def reset_case_directory(output_dir: Path, case: str) -> Path:
    case_dir = output_dir / case
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir


# Running

def collect_result(
    snapshot_index,
    case,
    actual_snapshot,
    solved_snapshot,
    chiller,
    buildings,
    branch,
    cooling_tower,
    storage_record=None,
    storage=None,
) -> Dict[str, float]:
    """Collect a standard result row from a solved snapshot."""
    q_actual = actual_snapshot.total_building_load
    q_effective = solved_snapshot.total_building_load
    q_evap = chiller.solved_Q_evap_W
    compressor_power = chiller.compressor.P.val
    heat_rejection = cooling_tower.heat_rejection
    cop = q_evap / compressor_power if compressor_power else float("nan")
    conns = chiller.internal_connections
    gains = branch.heat_gain_report()
    satellites = branch.satellite_report()
    applied_building_Q = sum(b.component.Q.val for b in buildings)

    record = {
        "case": case,
        "snapshot_index": snapshot_index,
        "timestamp": actual_snapshot.timestamp.isoformat(),
        "resolution_hours": actual_snapshot.resolution_hours,
        "ambient_temperature_degC": actual_snapshot.ambient_temperature,
        "ground_temperature_degC": actual_snapshot.ground_temperature,
        "solar_irradiance_W_m2": actual_snapshot.solar_irradiance_W_m2,
        "condenser_inlet_temperature_degC": actual_snapshot.condenser_inlet_temperature,
        "actual_building_total_Q_W": applied_building_Q,
        "profile_building_total_Q_W": q_actual,
        "effective_building_total_Q_W": q_effective,
        "chiller_Q_evap_W": q_evap,
        "compressor_power_W": compressor_power,
        "cop": cop,
        "heat_rejection_W": heat_rejection,
        "pipe_heat_gain_W": gains["total_heat_gain_W"],
        "pipe_supply_heat_gain_W": gains["supply_heat_gain_W"],
        "pipe_return_heat_gain_W": gains["return_heat_gain_W"],
        # Read back off the solved components, so it is the conductance the
        # network balanced on at this snapshot. For the `ua` model that is the
        # value DisCoolPy handed in; for a native group it is what TESPy's own
        # geometry equation worked out, which is the only way to see it.
        "pipe_network_UA_W_K": gains["network_UA_W_K"],
        "cw_in_T_degC": conns["cw_in"].T.val,
        "cw_out_T_degC": conns["cw_out"].T.val,
        "chw_plant_supply_T_degC": conns["chw_out"].T.val,
        "chw_supply_T_degC": branch.connections["branch_in"].T.val,
        "chw_return_T_degC": branch.connections["branch_out"].T.val,
        "chw_delta_T_K": branch.connections["branch_out"].T.val - branch.connections["branch_in"].T.val,
        "cw_m_kg_s": conns["cw_in"].m.val_SI,
        "chw_total_m_kg_s": branch.connections["branch_in"].m.val_SI,
        "pump_power_W": branch.pump.P.val if branch.pump is not None else 0.0,
        "satellite_Q_evap_W": satellites["total_Q_evap_W"],
        "satellite_compressor_power_W": satellites["total_compressor_power_W"],
        "satellite_storage_Q_W": satellites["total_storage_Q_W"],
        "satellite_delivered_Q_W": satellites["total_delivered_Q_W"],
    }
    # Electricity is what the district actually pays for, so report the whole
    # plant fleet as well as the central machine.
    record["total_compressor_power_W"] = (
        compressor_power + satellites["total_compressor_power_W"]
    )
    record["total_cooling_produced_W"] = q_evap + satellites["total_Q_evap_W"]
    record["fleet_cop"] = (
        record["total_cooling_produced_W"] / record["total_compressor_power_W"]
        if record["total_compressor_power_W"]
        else float("nan")
    )
    for name, values in satellites["per_plant"].items():
        safe = name.replace(" ", "_")
        record[f"satellite_{safe}_Q_evap_W"] = values["Q_evap_W"]
        record[f"satellite_{safe}_compressor_power_W"] = values["compressor_power_W"]
        record[f"satellite_{safe}_cop"] = values["cop"]
        record[f"satellite_{safe}_delivered_Q_W"] = values["delivered_Q_W"]
        if values["storage_soc"] == values["storage_soc"]:
            record[f"satellite_{safe}_storage_Q_W"] = values["storage_Q_W"]
            record[f"satellite_{safe}_storage_soc"] = values["storage_soc"]
    for key, value in gains["per_pipe_W"].items():
        record[f"pipe_{key.replace('/', '_')}_Q_W"] = value
    for building in buildings:
        safe = building.label.replace(" ", "_")
        record[f"{safe}_effective_Q_W"] = building.component.Q.val
        breakdown = getattr(building, "last_breakdown", {}) or {}
        for field_name in ("conduction_W", "solar_W", "infiltration_W", "internal_W", "envelope_gain_W"):
            if field_name in breakdown:
                record[f"{safe}_{field_name}"] = breakdown[field_name]
        if breakdown.get("T_indoor_degC") is not None:
            record[f"{safe}_T_indoor_degC"] = breakdown["T_indoor_degC"]
        mass_state = getattr(building, "last_mass_state", None)
        if mass_state is not None:
            record[f"{safe}_mass_mode"] = mass_state.mode
            record[f"{safe}_setpoint_deviation_K"] = mass_state.setpoint_deviation_K
            record[f"{safe}_stored_energy_change_kWh"] = mass_state.stored_energy_change_kWh
            record[f"{safe}_band_violation_K"] = mass_state.band_violation_K
            record[f"{safe}_capacity_limited"] = mass_state.capacity_limited

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
            "storage_ambient_heat_gain_W": 0.0,
            "storage_ambient_heat_gain_kWh": 0.0,
            "storage_fractional_loss_kWh": 0.0,
            "storage_medium_temperature_degC": float("nan"),
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
            "storage_ambient_heat_gain_W": storage_record.ambient_heat_gain_W,
            "storage_ambient_heat_gain_kWh": storage_record.ambient_heat_gain_kWh,
            "storage_fractional_loss_kWh": storage_record.fractional_loss_kWh,
            "storage_medium_temperature_degC": storage_record.medium_temperature_degC,
        })
        # A stratified tank reports its profile as well. These columns exist
        # only when one is in use, so a with/without comparison simply will not
        # have them on the reference side.
        if storage_record.tank_profile_degC is not None:
            record.update({
                "storage_tank_mass_flow_kg_s": storage_record.tank_mass_flow_kg_s,
                "storage_tank_outlet_temperature_degC": storage_record.tank_outlet_temperature_degC,
                "storage_tank_inlet_temperature_degC": storage_record.tank_inlet_temperature_degC,
                "storage_cold_port_degC": storage_record.cold_port_temperature_degC,
                "storage_warm_port_degC": storage_record.warm_port_temperature_degC,
                "storage_thermocline_m": storage_record.thermocline_thickness_m,
            })
            for i, temperature in enumerate(storage_record.tank_profile_degC):
                record[f"storage_tank_T{i}_degC"] = temperature

    # Closure check: everything that enters the chilled water must leave through
    # an evaporator. A satellite plant takes duty out of the loop before it
    # reaches the main machine, so it enters with the opposite sign. Reporting
    # the residual keeps a silent modelling error visible.
    record["chw_energy_residual_W"] = (
        q_evap
        + satellites["total_Q_evap_W"]
        - satellites["total_storage_Q_W"]
        - applied_building_Q
        - gains["total_heat_gain_W"]
        - record["pump_power_W"]
        - record["storage_chiller_load_offset_W"]
    )
    return record


def dispatch_satellite_plants(
    branch: Branch,
    snapshot,
    design_building_load_W: Optional[float] = None,
) -> Dict[str, Any]:
    """Set every satellite plant's duty, and its store's, for one snapshot.

    The control is the one a distributed plant is actually given: what it
    delivers either follows the district (``dispatch: proportional``) or stays
    flat (``dispatch: fixed``). Its store sits in series downstream of the
    evaporator and absorbs the difference between that and the flat duty it is
    aiming the chiller at. The chiller then runs level while its output follows
    demand, which is the reason to put a store at a satellite at all.

    Returns the per-plant :class:`~discoolpy.cold_storage.StorageDispatchResult`
    for the plants that have a store.
    """
    results: Dict[str, Any] = {}
    fraction = 1.0
    if design_building_load_W:
        fraction = float(snapshot.total_building_load) / float(design_building_load_W)
    for plant in branch.satellite_plants():
        delivered = plant.delivered_duty_W(fraction)
        store = plant.storage
        storage_Q = 0.0
        if store is not None:
            target = store.target_chiller_load_kW
            result = store.dispatch(
                snapshot,
                base_chiller_load_W=delivered,
                target_chiller_load_W=None if target is None else float(target) * 1000.0,
            )
            store.apply_dispatch_to_network(result)
            storage_Q = float(result.chiller_load_offset_W)
            results[plant.label] = result
        plant.apply_dispatch(delivered, storage_Q)
    return results


#: Kept under its old name for callers written against the first branching
#: release, which only dispatched the store.
dispatch_satellite_storage = dispatch_satellite_plants


def run_configured_case(
    config: Mapping[str, Any],
    profile_df: pd.DataFrame,
    case: str,
    use_storage: bool,
    progress: bool = True,
    return_system: bool = False,
):
    """Run one configured design/offdesign case and return the result dataframe.

    ``return_system`` also hands back the assembled
    :class:`DistrictCoolingSystem`, as ``(frame, system)``. The reporting layer
    wants both, and rebuilding the network afterwards would mean solving the
    design point a second time to learn what this solve already knows.
    """
    output_dir = ensure_output_dir(config)
    case_dir = reset_case_directory(output_dir, case)
    design_path = case_dir / "design_state"

    scenario = dict(config)
    if not use_storage:
        storage_cfg = dict(scenario.get("storage", {}) or {})
        storage_cfg["enabled"] = False
        scenario["storage"] = storage_cfg

    system = build_system(scenario)
    nw, chill, buildings, branch, cooling_tower = (
        system.network, system.chiller, system.buildings, system.branch, system.cooling_tower
    )
    storage = system.storage
    schedule = make_schedule_from_profile(config, profile_df, f"{case}_schedule")

    solver_cfg = config.get("solver", {}) or {}
    max_iter = int(solver_cfg.get("max_iter", 250))
    pump_offset = default_pump_power_W(config)
    storage_cfg = config.get("storage", {}) or {}
    target = storage_cfg.get("target_chiller_load_kW")
    target_chiller_load_W = None if target is None else float(target) * 1000.0
    min_load_fraction = float(storage_cfg.get("minimum_load_fraction", 0.08))
    assert_duty = system.plant_control == "evaporator_duty"
    design_building_load_W = sum(building_design_loads(config))

    if progress:
        satellite_note = (
            f", {len(system.satellite_plants)} satellite plant(s)"
            if system.satellite_plants
            else ""
        )
        print(f"[{case}] plant control: {system.plant_control}"
              + (f", storage coupling: {storage.coupling}" if storage else ", no storage")
              + satellite_note)
        print(f"[{case}] Solving design case...")
    nw.solve(mode="design", max_iter=max_iter)
    if not bool(getattr(nw, "converged", False)):
        raise RuntimeError(
            f"Design solve for case {case!r} did not converge; review pipe lengths, diameters, "
            "pressure anchors, heat-gain conductances, and starting values."
        )
    feasibility = branch.pressure_feasibility()
    if not feasibility["feasible"]:
        warnings.warn(f"[{case}] {feasibility['message']}", stacklevel=2)
    for machine in system.chillers:
        machine.enable_compressor_characteristic_extrapolation()
    nw.save(str(design_path))

    for building in buildings:
        if building.thermal_mass is not None:
            building.thermal_mass.reset()
    if storage is not None:
        storage.reset()
    for plant in system.satellite_plants:
        if plant.storage is not None:
            plant.storage.reset()

    hydraulic_storage = storage is not None and storage.coupling == "hydraulic"

    # A load-levelling controller has to aim at the plant duty rather than the
    # sum of the building loads. With heat gains in play those differ by
    # several per cent, and aiming at the wrong one makes the controller charge
    # straight through the peak. Pipe gain barely varies with load, so the
    # previous snapshot's solved value predicts it well enough for one step,
    # and the design solve seeds the first one.
    parasitic_estimate_W = (
        branch.heat_gain_report()["total_heat_gain_W"]
        + (branch.pump.P.val if branch.pump is not None else 0.0)
        - branch.satellite_report()["total_delivered_Q_W"]
    )

    # A stratified tank's ports see the network's water, not a design
    # constant, so it needs the solved supply and return temperatures. Those
    # only exist after the solve, so the previous snapshot's values stand in
    # for one step. Same trick as the parasitic estimate above, seeded the same
    # way from the design solve.
    tank_supply_T = branch.connections["branch_in"].T.val
    tank_return_T = branch.connections["branch_out"].T.val

    records: List[Dict[str, Any]] = []
    for index, snapshot in enumerate(schedule):
        storage_record = None
        solved_snapshot = snapshot
        if storage is not None:
            if hydraulic_storage:
                base_load = float(snapshot.total_building_load) + parasitic_estimate_W
                storage_record = storage.dispatch(
                    snapshot,
                    base_chiller_load_W=base_load,
                    target_chiller_load_W=target_chiller_load_W,
                    supply_temperature_degC=tank_supply_T,
                    return_temperature_degC=tank_return_T,
                )
            else:
                storage_record, solved_snapshot = storage.dispatch_and_make_effective_snapshot(
                    snapshot,
                    base_chiller_load_offset_W=pump_offset,
                    target_chiller_load_W=target_chiller_load_W,
                    minimum_load_fraction=min_load_fraction,
                    supply_temperature_degC=tank_supply_T,
                    return_temperature_degC=tank_return_T,
                )

        dispatch_satellite_plants(branch, snapshot, design_building_load_W)
        solved_snapshot.apply(
            buildings=buildings,
            chiller=chill,
            cooling_tower=cooling_tower,
            update_chiller=assert_duty,
            chiller_load_offset_W=pump_offset,
            branch=branch,
            storage=storage if hydraulic_storage else None,
            storage_result=storage_record if hydraulic_storage else None,
        )
        # Reloading the design state can rebuild the characteristic lines, so
        # re-enable extrapolation each time rather than only once after design.
        for machine in system.chillers:
            machine.enable_compressor_characteristic_extrapolation()
        nw.solve(mode="offdesign", design_path=str(design_path), max_iter=max_iter)
        if not bool(getattr(nw, "converged", False)):
            raise RuntimeError(
                f"Offdesign solve for case {case!r}, snapshot {index} "
                f"({snapshot.timestamp:%Y-%m-%d %H:%M}), did not converge; review pipe and "
                "load-profile settings."
            )
        row = collect_result(
            index, case, snapshot, solved_snapshot, chill, buildings, branch,
            cooling_tower, storage_record, storage,
        )
        records.append(row)
        parasitic_estimate_W = (
            row["pipe_heat_gain_W"] + row["pump_power_W"] - row["satellite_delivered_Q_W"]
        )
        tank_supply_T = row["chw_supply_T_degC"]
        tank_return_T = row["chw_return_T_degC"]
        if progress and index % int(solver_cfg.get("progress_interval", 48)) == 0:
            print(
                f"[{case}] snapshot {index}: {snapshot.timestamp:%Y-%m-%d %H:%M}  "
                f"Q={row['chiller_Q_evap_W']/1e3:7.1f} kW  "
                f"pipes={row['pipe_heat_gain_W']/1e3:5.2f} kW  COP={row['cop']:.3f}"
            )

    frame = pd.DataFrame(records)
    residual = frame["chw_energy_residual_W"].abs().max()
    if residual > 1.0:
        warnings.warn(
            f"[{case}] chilled-water energy balance residual reached {residual:.2f} W. "
            "This should be numerically zero; check the pipe energy specifications.",
            stacklevel=2,
        )
    # Flexibility taken out of occupant comfort is not flexibility. Say so.
    for column in [c for c in frame.columns if c.endswith("_band_violation_K")]:
        worst = float(frame[column].max())
        if worst > 1e-6:
            label = column[: -len("_band_violation_K")]
            hours = float(frame[column].gt(1e-6).sum()) * frame["resolution_hours"].iloc[0]
            warnings.warn(
                f"[{case}] {label} left its comfort band by up to {worst:.2f} K over {hours:.1f} h. "
                "Its terminal units are capacity-limited for this weather, so the reported load "
                "shedding is partly a comfort failure, not flexibility.",
                stacklevel=2,
            )
    return (frame, system) if return_system else frame


# Reporting

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
        "without_storage_pipe_gain_W": no_storage["pipe_heat_gain_W"],
        "with_storage_pipe_gain_W": with_storage["pipe_heat_gain_W"],
        "storage_power_W": with_storage["storage_power_W"],
        "storage_soc_after": with_storage["storage_soc_after"],
        "storage_ambient_heat_gain_W": with_storage["storage_ambient_heat_gain_W"],
    })
    comparison["compressor_power_reduction_W"] = comparison["without_storage_compressor_power_W"] - comparison["with_storage_compressor_power_W"]
    comparison.to_csv(output_path(config, "comparison_csv", "storage_comparison_summary_timeseries.csv"), index=False)

    from .flexibility import assess_flexibility

    report = assess_flexibility(
        no_storage,
        with_storage,
        reference_name="without_storage",
        flexible_name="with_storage",
        tariff=(config.get("economics", {}) or {}).get("tariff"),
        carbon_intensity=(config.get("economics", {}) or {}).get("carbon_intensity_kg_kWh"),
    )
    branch_cfg = config.get("branch", {}) or {}
    header = (
        f"Scenario file: `{Path(config.get('_config_path', 'scenario.yaml')).name}`. "
        f"Pipe model `{branch_cfg.get('pipe_model', 'pressure_ratio')}`, "
        f"pipe heat model `{branch_cfg.get('heat_model', 'adiabatic')}`, "
        f"storage coupling `{(config.get('storage', {}) or {}).get('coupling', 'supervisory')}`.\n\n"
    )
    output_path(config, "summary_md", "storage_comparison_summary.md").write_text(
        header + report.to_markdown() + "\n", encoding="utf-8"
    )
    return comparison


def make_storage_comparison_plot(config: Mapping[str, Any], no_storage: pd.DataFrame, with_storage: pd.DataFrame) -> None:
    """Create the standard storage comparison plot, including heat gains."""
    no = no_storage.copy()
    ws = with_storage.copy()
    no["timestamp"] = pd.to_datetime(no["timestamp"])
    ws["timestamp"] = pd.to_datetime(ws["timestamp"])
    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(6, 1, figsize=(14, 21), sharex=True, facecolor="white")
    for ax in axes:
        ax.set_facecolor("white")
    axes[0].plot(no["timestamp"], no["actual_building_total_Q_W"] / 1000, color="black", label="Building cooling demand")
    axes[0].plot(no["timestamp"], no["chiller_Q_evap_W"] / 1000, color="tab:grey", ls="--", label="Plant duty, no storage")
    axes[0].plot(ws["timestamp"], ws["chiller_Q_evap_W"] / 1000, color="tab:blue", label="Plant duty, with storage")
    axes[0].set_ylabel("Cooling load [kW]")
    axes[0].set_title("Demand versus plant duty (the gap is pipe gain, pump heat and storage exchange)")
    axes[0].legend(loc="upper left")
    axes[1].plot(no["timestamp"], no["pipe_heat_gain_W"] / 1000, color="tab:brown", label="Pipe heat gain")
    axes[1].plot(ws["timestamp"], ws["storage_ambient_heat_gain_W"] / 1000, color="tab:olive", label="Storage ambient gain")
    axes[1].set_ylabel("Parasitic gain [kW]")
    axes[1].set_title("Heat gains into the chilled medium")
    axes[1].legend(loc="upper left")
    axes[2].plot(ws["timestamp"], ws["storage_power_W"] / 1000, color="tab:cyan", label="Storage power (+ discharge, - charge)")
    axes[2].axhline(0, color="black", linewidth=0.8)
    axes[2].set_ylabel("Storage power [kW]")
    axes[2].set_title("Storage charge/discharge dispatch")
    axes[2].legend(loc="upper left")
    axes[3].plot(ws["timestamp"], ws["storage_soc_after"], color="tab:green", label="Storage SOC")
    axes[3].set_ylabel("SOC [-]")
    axes[3].set_ylim(0, 1.02)
    axes[3].set_title("Storage state of charge")
    axes[3].legend(loc="upper left")
    axes[4].plot(no["timestamp"], no["compressor_power_W"] / 1000, label="Without storage", color="tab:red")
    axes[4].plot(ws["timestamp"], ws["compressor_power_W"] / 1000, label="With storage", color="tab:purple")
    axes[4].set_ylabel("Compressor power [kW]")
    axes[4].set_title("Compressor power comparison")
    axes[4].legend(loc="upper left")
    axes[5].plot(no["timestamp"], no["cop"], label="Without storage", color="tab:orange")
    axes[5].plot(ws["timestamp"], ws["cop"], label="With storage", color="tab:blue")
    axes[5].set_ylabel("COP [-]")
    axes[5].set_title("Chiller COP comparison")
    axes[5].set_xlabel("Timestamp")
    axes[5].legend(loc="upper left")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path(config, "plot_png", "storage_comparison_results.png"), dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)


__all__ = [
    "load_yaml_config",
    "ensure_output_dir",
    "output_path",
    "design_value",
    "building_labels",
    "building_design_loads",
    "make_pipe_attrs",
    "compute_building_mass_flows",
    "total_design_mass_flow",
    "design_evaporator_load",
    "DistrictCoolingSystem",
    "build_system",
    "build_standard_branch_system",
    "apply_design_specifications",
    "default_pump_power_W",
    "DESIGN_DEFAULTS",
    "apply_default_starting_values",
    "make_storage_from_config",
    "OCCUPANCY_ARCHETYPES",
    "make_weather_and_load_profiles",
    "make_riyadh_weather_and_load_profiles",
    "make_schedule_from_profile",
    "collect_result",
    "run_configured_case",
    "write_storage_comparison_summary",
    "make_storage_comparison_plot",
]
