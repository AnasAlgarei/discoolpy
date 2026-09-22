"""Translate YAML scenario sections into DisCoolPy objects.

Schema handling lives in its own module for two reasons: it keeps ``utils``
from turning into a grab-bag, and it puts every default and every
compatibility shim in one place where you can read them all at once.

The rule everything here follows: a scenario that says nothing about heat gains
gets none. Every optional section defaults off, so an old scenario file keeps
solving the way it always did.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import (
    Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple,
)

from .building import Building, ThermalMass
from .errors import ScenarioError
from .hydraulics import suggest_pressure_specification
from .cold_storage import ColdStorage
from .stratified import StratifiedTank
from .thermal import (
    EnvelopeThermal,
    PipeThermal,
    StorageThermal,
    buried_pipe_UA_per_m,
    surface_pipe_UA_per_m,
    tank_UA_W_K,
)

__all__ = [
    "PIPE_HEAT_MODELS",
    "TERMINAL_KINDS",
    "BranchSpec",
    "TerminalSpec",
    "make_branch_specs",
    "autofill_branch_pressure",
    "autofill_network_energy",
    "AUTO_PIPE_PRESSURE_RATIO",
    "branch_pipe_thermal",
    "make_pipe_thermal",
    "make_network_pipe_thermal",
    "make_buildings",
    "building_design_load_W",
    "make_storage",
    "make_stratified_tank",
    "make_storage_from_section",
    "plant_control_mode",
    "heat_gains_enabled",
    "SCENARIO_KEYS",
    "validate_scenario",
]

PIPE_HEAT_MODELS = {"adiabatic", "fixed", "ua", "tespy_buried", "tespy_surface"}

#: What may close the end of a branch in a scenario file.
TERMINAL_KINDS = {"bypass", "plant", "branch"}

#: Branch settings a child inherits from its parent unless it says otherwise.
_INHERITED_BRANCH_KEYS = (
    "pipe_model",
    "heat_model",
    "thermal_defaults",
    "hydraulic",
    "native_offdesign",
    "auto_relax_pressure",
)


_THERMAL_DEFAULTS: Dict[str, Any] = {
    "insulation_thickness_m": 0.04,
    "insulation": "pur",
    "burial_depth_m": 1.0,
    "ground": "moist soil",
    "pipe_wall_thickness_m": 0.0,
    "pipe_wall_conductivity_W_mK": 45.0,
    "internal_film_W_m2K": 2000.0,
    "twin_spacing_m": None,
    "wind_velocity_m_s": 1.0,
    "emissivity": 0.9,
    "ambient_source": "ground",
}


def _merged(defaults: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(defaults)
    merged.update({k: v for k, v in (override or {}).items() if v is not None})
    return merged


def heat_gains_enabled(config: Mapping[str, Any]) -> bool:
    """True when any component in the scenario has a non-adiabatic heat model.

    Walks the whole branch tree: a forking network can carry adiabatic mains on
    its trunk and buried spurs on a child, and either one is enough to make the
    plant duty a solved output rather than an asserted one.
    """
    for section in _branch_sections(config.get("branch", {}) or {}):
        if str(section.get("heat_model", "adiabatic")).lower() != "adiabatic":
            return True
        for spec in (section.get("pipes", {}) or {}).values():
            if (
                isinstance(spec, Mapping)
                and str(spec.get("heat_model", "adiabatic")).lower() != "adiabatic"
            ):
                return True
        for terminal in section.get("terminals", []) or []:
            if not isinstance(terminal, Mapping):
                continue
            # A satellite plant is a second heat flow in the chilled-water loop,
            # so the loop balance has to close it, rather than an asserted duty.
            if str(terminal.get("type", "bypass")).lower() == "plant":
                return True
    storage = config.get("storage", {}) or {}
    if storage.get("enabled") and str(storage.get("coupling", "supervisory")).lower() == "hydraulic":
        return True
    for building in config.get("buildings", []) or []:
        if str(building.get("load_model", "profile")).lower() != "profile":
            return True
    return False


def _branch_sections(section: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Yield a raw branch mapping and every nested sub-branch mapping."""
    if not isinstance(section, Mapping):
        return
    yield section
    for terminal in section.get("terminals", []) or []:
        if isinstance(terminal, Mapping) and str(terminal.get("type", "")).lower() == "branch":
            yield from _branch_sections(terminal)


def plant_control_mode(config: Mapping[str, Any]) -> str:
    """Resolve how the chiller duty is determined for this scenario.

    Defaults to ``"supply_temperature"`` whenever heat gains are active, because
    asserting the evaporator duty *and* letting pipes or a store inject heat
    over-determines the chilled-water loop, and TESPy rejects the network outright
    rather than silently mis-solving it.
    """
    explicit = (config.get("design", {}) or {}).get("plant_control")
    if explicit:
        mode = str(explicit).lower()
        if mode not in {"supply_temperature", "evaporator_duty"}:
            raise ValueError(
                "design.plant_control must be 'supply_temperature' or 'evaporator_duty'."
            )
        if mode == "evaporator_duty" and heat_gains_enabled(config):
            raise ValueError(
                "design.plant_control='evaporator_duty' cannot be combined with heat gains: "
                "the chilled-water loop energy balance already determines the plant duty, so "
                "asserting it as well over-determines the network. Use 'supply_temperature'."
            )
        return mode
    return "supply_temperature" if heat_gains_enabled(config) else "evaporator_duty"


# Pipes

def make_pipe_thermal(config: Mapping[str, Any]) -> Dict[str, PipeThermal]:
    """Build a :class:`PipeThermal` for every pipe on the scenario's *root* branch.

    The per-pipe entry inherits ``branch.thermal_defaults`` and may override any
    of it, so a network of mostly-identical buried mains only needs the
    exceptions spelled out. Use :func:`make_network_pipe_thermal` to get the
    same thing for every branch of a forking network.
    """
    return _pipe_thermal_from_section(
        config,
        branch_label="branch",
        heat_model=str((config.get("branch", {}) or {}).get("heat_model", "adiabatic")).lower(),
        thermal_defaults=(config.get("branch", {}) or {}).get("thermal_defaults", {}) or {},
        pipes=(config.get("branch", {}) or {}).get("pipes", {}) or {},
    )


def _pipe_thermal_from_section(
    config: Mapping[str, Any],
    branch_label: str,
    heat_model: str,
    thermal_defaults: Mapping[str, Any],
    pipes: Mapping[str, Any],
) -> Dict[str, PipeThermal]:
    """Shared pipe heat-model construction for one branch's ``pipes`` mapping."""
    default_model = str(heat_model or "adiabatic").lower()
    defaults = _merged(_THERMAL_DEFAULTS, thermal_defaults or {})
    design = config.get("design", {}) or {}
    ground_T = design.get("ground_temperature_degC")
    air_T = design.get("ambient_temperature_degC")

    out: Dict[str, PipeThermal] = {}
    for key, raw in (pipes or {}).items():
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"Pipe specification for {key!r} on branch {branch_label!r} must be a mapping."
            )
        model = str(raw.get("heat_model", default_model)).lower()
        if model not in PIPE_HEAT_MODELS:
            raise ValueError(
                f"Pipe {key!r}: heat_model must be one of {sorted(PIPE_HEAT_MODELS)}, got {model!r}."
            )
        settings = _merged(defaults, raw)
        source = str(settings.get("ambient_source", "ground")).lower()
        if source not in {"ground", "air", "fixed"}:
            raise ValueError(f"Pipe {key!r}: ambient_source must be 'ground', 'air' or 'fixed'.")

        ambient = raw.get("ambient_temperature_degC")
        if ambient is None:
            ambient = ground_T if source == "ground" else air_T
            if source == "ground" and ambient is None:
                ambient = air_T

        spec = PipeThermal(
            model=model,
            ambient_source=source,
            ambient_temperature_degC=None if ambient is None else float(ambient),
            length_m=None if raw.get("L") is None else float(raw["L"]),
        )

        if model == "adiabatic":
            spec.Q_W = 0.0
        elif model == "fixed":
            q = raw.get("Q", raw.get("Q_W", 0.0))
            spec.Q_W = float(q or 0.0)
        elif model == "ua":
            if raw.get("UA_W_K") is not None:
                spec.UA_W_K = float(raw["UA_W_K"])
            elif raw.get("UA_per_m_W_mK") is not None:
                spec.UA_per_m_W_mK = float(raw["UA_per_m_W_mK"])
            else:
                if raw.get("L") is None or raw.get("D") is None:
                    raise ValueError(
                        f"Pipe {key!r} uses heat_model='ua' but gives neither UA_W_K nor the "
                        "L and D needed to derive it from geometry."
                    )
                placement = str(settings.get("placement", "buried")).lower()
                if placement == "buried":
                    spec.UA_per_m_W_mK = buried_pipe_UA_per_m(
                        inner_diameter_m=float(raw["D"]),
                        insulation_thickness_m=float(settings["insulation_thickness_m"]),
                        insulation_conductivity=settings["insulation"],
                        burial_depth_m=float(settings["burial_depth_m"]),
                        ground=settings["ground"],
                        pipe_wall_thickness_m=float(settings["pipe_wall_thickness_m"]),
                        pipe_wall_conductivity_W_mK=float(settings["pipe_wall_conductivity_W_mK"]),
                        internal_film_W_m2K=float(settings["internal_film_W_m2K"]),
                        twin_spacing_m=settings.get("twin_spacing_m"),
                    )
                elif placement in {"surface", "exposed", "tunnel"}:
                    spec.UA_per_m_W_mK = surface_pipe_UA_per_m(
                        inner_diameter_m=float(raw["D"]),
                        insulation_thickness_m=float(settings["insulation_thickness_m"]),
                        insulation_conductivity=settings["insulation"],
                        pipe_wall_thickness_m=float(settings["pipe_wall_thickness_m"]),
                        pipe_wall_conductivity_W_mK=float(settings["pipe_wall_conductivity_W_mK"]),
                        internal_film_W_m2K=float(settings["internal_film_W_m2K"]),
                        wind_velocity_m_s=float(settings["wind_velocity_m_s"]),
                        emissivity=float(settings["emissivity"]),
                        ambient_temperature_degC=float(air_T if air_T is not None else 35.0),
                    )
                else:
                    raise ValueError(
                        f"Pipe {key!r}: placement must be 'buried', 'surface' or 'tunnel'."
                    )
            spec.geometry = {
                "L": raw.get("L"),
                "D": raw.get("D"),
                "placement": settings.get("placement", "buried"),
                "insulation_thickness_m": settings.get("insulation_thickness_m"),
                "insulation": settings.get("insulation"),
            }
        else:  # tespy_buried / tespy_surface
            if raw.get("D") is None or raw.get("L") is None:
                raise ValueError(
                    f"Pipe {key!r} uses heat_model={model!r}, which is TESPy's native pipe "
                    "group and needs the real geometry: give both L and D."
                )
            native: Dict[str, Any] = {
                "D": float(raw["D"]),
                "L": float(raw["L"]),
                "insulation_thickness": _native_insulation_thickness(
                    key, settings["insulation_thickness_m"]
                ),
                "insulation_tc": _insulation_tc(settings["insulation"]),
                "pipe_thickness": float(settings["pipe_wall_thickness_m"] or 0.003),
                "material": str(settings.get("material", "Steel")),
            }
            if model == "tespy_buried":
                native["pipe_depth"] = float(settings["burial_depth_m"])
                native["environment_media"] = _native_ground(key, settings["ground"])
            else:
                native["wind_velocity"] = _native_wind_velocity(
                    key, settings["wind_velocity_m_s"]
                )
                native["environment_media"] = str(settings.get("environment_media", "air"))
            spec.native_attrs = native

        out[str(key)] = spec
    return out


def _insulation_tc(value: Any) -> float:
    from .thermal import INSULATION_CONDUCTIVITY_W_mK, _resolve_conductivity

    return _resolve_conductivity(value, INSULATION_CONDUCTIVITY_W_mK, "insulation")


# The three ways a native TESPy pipe group fails, caught while the scenario is
# still a scenario. Each of them otherwise surfaces from inside the residual
# evaluation as a non-convergence message that names neither the pipe nor the
# offending value, and the modeller is left bisecting a YAML file.

def _native_ground(key: str, value: Any) -> str:
    from .thermal import GROUND_CONDUCTIVITY_W_mK, TESPY_NATIVE_GROUND_MEDIA

    name = str(value).strip().lower()
    if name in TESPY_NATIVE_GROUND_MEDIA:
        return name
    known = name in GROUND_CONDUCTIVITY_W_mK
    raise ValueError(
        f"Pipe {key!r}: heat_model='tespy_buried' passes the ground material straight to "
        f"TESPy, whose buried-pipe group knows only {list(TESPY_NATIVE_GROUND_MEDIA)}, and "
        f"{name!r} is not one of them. "
        + (
            "DisCoolPy's own table does know it, so heat_model='ua' will model this "
            "ground directly."
            if known
            else "Use one of TESPy's four, or heat_model='ua' with a numeric "
            "conductivity in W/(m.K)."
        )
    )


def _native_insulation_thickness(key: str, value: Any) -> float:
    from .thermal import TESPY_MIN_INSULATION_THICKNESS_M

    thickness = float(value or 0.0)
    if thickness >= TESPY_MIN_INSULATION_THICKNESS_M:
        return thickness
    raise ValueError(
        f"Pipe {key!r}: TESPy's native pipe groups reject an insulation thickness below "
        f"{TESPY_MIN_INSULATION_THICKNESS_M} m, and this pipe asks for {thickness} m. A bare "
        "or near-bare pipe is a case for heat_model='ua', which carries the soil or external "
        "film resistance on its own and does not need an insulation layer to exist."
    )


def _native_wind_velocity(key: str, value: Any) -> float:
    velocity = float(value or 0.0)
    if velocity > 0.0:
        return velocity
    raise ValueError(
        f"Pipe {key!r}: heat_model='tespy_surface' builds its external film from a forced-"
        "convection correlation, which divides by the Reynolds number and so cannot take "
        f"wind_velocity_m_s={velocity}. Give a non-zero wind speed, or use heat_model='ua', "
        "whose external film stays finite in still air and adds the radiative term that "
        "dominates it there."
    )


# Buildings

def building_design_load_W(item: Mapping[str, Any]) -> float:
    """One building's design cooling load in watts.

    A scenario may write it as ``Q_design_W`` or ``Q_design_kW``. Kilowatts are
    what an engineer has on a schedule, and a 150 typed into a watts key is a
    building three orders of magnitude too small that still solves, so the
    alternative is worth having. :func:`validate_scenario` refuses both at once.
    """
    watts = item.get("Q_design_W")
    if watts is not None:
        return float(watts)
    kilowatts = item.get("Q_design_kW")
    if kilowatts is not None:
        return float(kilowatts) * 1e3
    raise ScenarioError(
        "no design load",
        where=f"buildings[{item.get('label', '?')}]",
        hint="give 'Q_design_W' in watts, or 'Q_design_kW' in kilowatts.",
    )


def make_buildings(config: Mapping[str, Any]) -> List[Building]:
    """Instantiate every building, including envelope and thermal-mass models."""
    buildings: List[Building] = []
    for item in config.get("buildings", []) or []:
        load_model = str(item.get("load_model", "profile")).lower()
        envelope = None
        env_cfg = item.get("envelope") or {}
        if env_cfg or load_model != "profile":
            envelope = EnvelopeThermal(
                UA_W_K=float(env_cfg.get("UA_W_K", 0.0)),
                solar_aperture_m2=float(env_cfg.get("solar_aperture_m2", 0.0)),
                infiltration_kg_s=float(env_cfg.get("infiltration_kg_s", 0.0)),
                indoor_setpoint_degC=float(env_cfg.get("indoor_setpoint_degC", 24.0)),
                internal_gain_W=float(env_cfg.get("internal_gain_W", 0.0)),
                cp_air_J_kgK=float(env_cfg.get("cp_air_J_kgK", 1005.0)),
            )
        mass = None
        mass_cfg = item.get("thermal_mass") or {}
        if mass_cfg:
            capacitance = mass_cfg.get("capacitance_J_K")
            if capacitance is None and mass_cfg.get("capacitance_MJ_K") is not None:
                capacitance = float(mass_cfg["capacitance_MJ_K"]) * 1e6
            if capacitance is None:
                raise ValueError(
                    f"Building {item.get('label')!r}: thermal_mass needs capacitance_J_K "
                    "or capacitance_MJ_K."
                )
            setpoint = float(
                mass_cfg.get(
                    "setpoint_degC",
                    envelope.indoor_setpoint_degC if envelope is not None else 24.0,
                )
            )
            mass = ThermalMass(
                capacitance_J_K=float(capacitance),
                setpoint_degC=setpoint,
                min_temperature_degC=float(mass_cfg.get("min_temperature_degC", setpoint - 3.0)),
                max_temperature_degC=float(mass_cfg.get("max_temperature_degC", setpoint + 2.0)),
                max_cooling_W=(
                    None if mass_cfg.get("max_cooling_W") is None else float(mass_cfg["max_cooling_W"])
                ),
                precool_max_cooling_W=(
                    None
                    if mass_cfg.get("precool_max_cooling_W") is None
                    else float(mass_cfg["precool_max_cooling_W"])
                ),
                indoor_temperature_degC=(
                    None
                    if mass_cfg.get("initial_temperature_degC") is None
                    else float(mass_cfg["initial_temperature_degC"])
                ),
            )
        buildings.append(
            Building(
                str(item["label"]),
                Q_design=building_design_load_W(item),
                pr=item.get("pr"),
                load_model=load_model,
                envelope=envelope,
                thermal_mass=mass,
            )
        )
    if not buildings:
        raise ValueError("At least one building must be defined in the scenario.")
    return buildings


# Storage

def make_storage(config: Mapping[str, Any]) -> Optional[ColdStorage]:
    """Create a ColdStorage instance when the YAML storage section is enabled."""
    cfg = config.get("storage", {}) or {}
    if not cfg or not bool(cfg.get("enabled", False)):
        return None

    loss_model = str(cfg.get("loss_model", "fraction")).lower()

    # Built first: a stratified tank carries its own shell UA layer by layer, so
    # it removes the need for a scalar StorageThermal rather than adding to it.
    stratified = make_stratified_tank(cfg)
    if stratified is not None and loss_model in {"fraction", "both"}:
        raise ValueError(
            f"Storage {cfg.get('label', 'storage')!r} is stratified, so its losses come from "
            "the tank's own shell UA layer by layer. Set loss_model: ua (or omit it) rather "
            "than 'fraction'/'both', which would count the standing loss twice."
        )

    thermal: Optional[StorageThermal] = None
    thermal_cfg = cfg.get("thermal") or {}
    if stratified is None and (loss_model in {"ua", "both"} or thermal_cfg):
        storage_T = float(thermal_cfg.get("storage_temperature_degC", 0.0))
        discharged_T = thermal_cfg.get("discharged_temperature_degC")
        if thermal_cfg.get("UA_W_K") is not None:
            ua = float(thermal_cfg["UA_W_K"])
        elif thermal_cfg.get("volume_m3") is not None:
            ua = tank_UA_W_K(
                volume_m3=float(thermal_cfg["volume_m3"]),
                insulation_thickness_m=float(thermal_cfg.get("insulation_thickness_m", 0.1)),
                insulation_conductivity=thermal_cfg.get("insulation", "pur"),
                height_to_diameter=float(thermal_cfg.get("height_to_diameter", 1.0)),
                internal_film_W_m2K=float(thermal_cfg.get("internal_film_W_m2K", 300.0)),
                external_film_W_m2K=float(thermal_cfg.get("external_film_W_m2K", 12.0)),
                buried_fraction=float(thermal_cfg.get("buried_fraction", 0.0)),
                ground=thermal_cfg.get("ground", "moist soil"),
            )
        elif loss_model in {"ua", "both"}:
            raise ValueError(
                "storage.loss_model requires a UA: give storage.thermal.UA_W_K or "
                "storage.thermal.volume_m3 (plus insulation) so it can be derived."
            )
        else:
            ua = 0.0
        thermal = StorageThermal(
            UA_W_K=ua,
            storage_temperature_degC=storage_T,
            discharged_temperature_degC=None if discharged_T is None else float(discharged_T),
            temperature_varies_with_soc=bool(thermal_cfg.get("temperature_varies_with_soc", False)),
        )

    return ColdStorage(
        label=cfg.get("label", "ice_storage"),
        capacity_kWh=float(cfg.get("capacity_kWh", 1600.0)),
        initial_soc=float(cfg.get("initial_soc", 0.55)),
        max_charge_kW=float(cfg.get("max_charge_kW", 180.0)),
        max_discharge_kW=float(cfg.get("max_discharge_kW", 180.0)),
        charge_efficiency=float(cfg.get("charge_efficiency", 0.90)),
        discharge_efficiency=float(cfg.get("discharge_efficiency", 0.92)),
        standby_loss_fraction_per_day=float(cfg.get("standby_loss_fraction_per_day", 0.015)),
        thermal=thermal,
        loss_model=loss_model,
        coupling=str(cfg.get("coupling", "supervisory")).lower(),
        hydraulic_pr=cfg.get("hydraulic_pr", 0.999),
        storage_type=cfg.get("storage_type", "ice"),
        target_chiller_load_kW=(
            None if cfg.get("target_chiller_load_kW") is None
            else float(cfg["target_chiller_load_kW"])
        ),
        min_soc=float(cfg.get("min_soc", 0.08)),
        max_soc=float(cfg.get("max_soc", 0.97)),
        charge_allowed_above_degC=cfg.get("charge_allowed_above_degC"),
        discharge_allowed_below_degC=cfg.get("discharge_allowed_below_degC"),
        stratified=stratified,
        max_tank_flow_kg_s=cfg.get("max_tank_flow_kg_s"),
    )


def make_stratified_tank(cfg: Mapping[str, Any]) -> Optional[StratifiedTank]:
    """Build the 1-D tank from a ``storage.stratified`` section, when enabled.

    The tank's temperatures default to the store's own ``thermal`` block when it
    has one, so a scenario upgrading from a scalar chilled-water store to a
    stratified one does not have to restate them.
    """
    raw = (cfg or {}).get("stratified") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("storage.stratified must be a mapping.")
    if not bool(raw.get("enabled", False)):
        return None

    thermal_cfg = (cfg.get("thermal") or {})
    charged = raw.get("charged_temperature_degC",
                      thermal_cfg.get("storage_temperature_degC"))
    discharged = raw.get("discharged_temperature_degC",
                         thermal_cfg.get("discharged_temperature_degC"))
    if charged is None or discharged is None:
        raise ValueError(
            "A stratified tank needs both charged_temperature_degC and "
            "discharged_temperature_degC (directly, or via storage.thermal's "
            "storage_temperature_degC / discharged_temperature_degC). The pair defines "
            "the tank's usable temperature span, and therefore its capacity."
        )

    volume = raw.get("volume_m3", thermal_cfg.get("volume_m3"))
    optional: Dict[str, Any] = {}
    for key in ("height_m", "diameter_m", "wall_htc_W_m2K",
                "vertical_conductivity_W_mK", "max_courant", "max_sub_steps"):
        if raw.get(key) is not None:
            optional[key] = raw[key]
    if raw.get("height_to_diameter") is not None:
        optional["height_to_diameter"] = float(raw["height_to_diameter"])
    elif thermal_cfg.get("height_to_diameter") is not None:
        optional["height_to_diameter"] = float(thermal_cfg["height_to_diameter"])

    return StratifiedTank(
        volume_m3=None if volume is None else float(volume),
        layers=int(raw.get("layers", 12)),
        charged_temperature_degC=float(charged),
        discharged_temperature_degC=float(discharged),
        initial_soc=float(cfg.get("initial_soc", 0.5)),
        **optional,
    )


# Branch tree

@dataclass
class TerminalSpec:
    """One end-of-line terminal as written in the scenario file."""

    kind: str                              # "bypass" | "plant" | "branch"
    label: str
    raw: Dict[str, Any] = field(default_factory=dict)
    child: Optional["BranchSpec"] = None
    mass_flow_kg_s: Optional[float] = None
    dp: Optional[float] = None
    heading_deg: Optional[float] = None

    @property
    def releasable(self) -> bool:
        """True when the terminal's pressure drop may be freed to repair a loop.

        A bypass valve and a satellite plant's balancing valve both have their
        drop set by a valve nobody sizes by hand, so either may be released. A
        sub-branch carries no specification of its own.
        """
        return self.kind in {"bypass", "plant"}

    def pressure_spec(self) -> Optional[str]:
        """What fixes this terminal's pressure drop, or None when it floats."""
        if self.kind == "branch" or self.dp is None:
            return None
        return f"dp={self.dp}"


@dataclass
class BranchSpec:
    """One branch of the network as written in the scenario file.

    This is a *normalised* view: inheritance from the parent branch has already
    been resolved, the building list has been turned into indices into the
    top-level ``buildings`` section, and the terminal list always has at least
    one entry. Nothing here touches TESPy, which is what makes the whole tree
    checkable before a single component is built.
    """

    label: str
    raw: Dict[str, Any] = field(default_factory=dict)
    building_indices: List[int] = field(default_factory=list)
    pipes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    terminals: List[TerminalSpec] = field(default_factory=list)
    is_root: bool = False
    heading_deg: float = 0.0
    origin_m: Optional[Tuple[float, float]] = None
    parent: Optional["BranchSpec"] = None
    #: True when the scenario named this branch's buildings explicitly.
    buildings_listed: bool = False

    @property
    def n_buildings(self) -> int:
        return len(self.building_indices)

    @property
    def n_pipes_per_side(self) -> int:
        # A branch with no buildings is a trunk link: one supply, one return pipe.
        return self.n_buildings or 1

    @property
    def pipe_keys(self) -> List[str]:
        n = self.n_pipes_per_side
        return [f"supply_{i}" for i in range(1, n + 1)] + [
            f"return_{i}" for i in range(1, n + 1)
        ]

    def qualify(self, key: str) -> str:
        """Namespace a local pipe key for whole-network reporting."""
        return key if self.is_root else f"{self.label}/{key}"

    def children(self) -> List["BranchSpec"]:
        return [t.child for t in self.terminals if t.child is not None]

    def walk(self) -> Iterator["BranchSpec"]:
        """Yield this branch and every descendant, depth-first in terminal order."""
        yield self
        for child in self.children():
            yield from child.walk()

    def setting(self, key: str, default: Any = None) -> Any:
        value = self.raw.get(key)
        return default if value is None else value


def _terminal_label(raw: Mapping[str, Any], kind: str, index: int) -> str:
    label = raw.get("label")
    if label:
        return str(label)
    if kind == "bypass":
        return "end of line bypass"
    return f"{kind}_{index}"


def _resolve_building_indices(
    raw: Mapping[str, Any],
    label_to_index: Mapping[str, int],
    branch_label: str,
) -> Optional[List[int]]:
    """Turn a branch's ``buildings`` list into indices, preserving written order."""
    listed = raw.get("buildings")
    if listed is None:
        return None
    if isinstance(listed, (str, bytes)) or not isinstance(listed, Sequence):
        raise ValueError(
            f"Branch {branch_label!r}: 'buildings' must be a list of building labels."
        )
    indices: List[int] = []
    for entry in listed:
        name = str(entry.get("label") if isinstance(entry, Mapping) else entry)
        if name not in label_to_index:
            raise ValueError(
                f"Branch {branch_label!r} lists building {name!r}, which is not defined in the "
                "top-level 'buildings' section. Known buildings: "
                + ", ".join(sorted(label_to_index))
                + "."
            )
        indices.append(label_to_index[name])
    return indices


def _make_branch_spec(
    raw: Mapping[str, Any],
    label_to_index: Mapping[str, int],
    default_bypass_m: Optional[float],
    is_root: bool,
    parent: Optional[BranchSpec],
    seen_labels: set,
) -> BranchSpec:
    raw = dict(raw or {})
    label = str(raw.get("label") or ("district" if is_root else "branch"))
    if label in seen_labels:
        raise ValueError(
            f"Duplicate branch label {label!r}. Every branch needs a unique label because it "
            "prefixes the TESPy component names."
        )
    seen_labels.add(label)

    if parent is not None:
        for key in _INHERITED_BRANCH_KEYS:
            if raw.get(key) is None and parent.raw.get(key) is not None:
                raw[key] = parent.raw[key]

    origin = raw.get("origin_m")
    spec = BranchSpec(
        label=label,
        raw=raw,
        pipes={str(k): dict(v or {}) for k, v in (raw.get("pipes") or {}).items()},
        is_root=is_root,
        heading_deg=float(
            raw.get("heading_deg", parent.heading_deg if parent is not None else 0.0)
        ),
        origin_m=None if origin is None else (float(origin[0]), float(origin[1])),
        parent=parent,
    )

    indices = _resolve_building_indices(raw, label_to_index, label)
    spec.buildings_listed = indices is not None
    spec.building_indices = list(indices or [])

    raw_terminals = raw.get("terminals")
    if raw_terminals is None:
        # No terminals section: exactly the pre-branching topology, one bypass
        # valve carrying design.bypass_mass_flow_kg_s.
        spec.terminals = [
            TerminalSpec(
                kind="bypass",
                label="end of line bypass",
                raw={},
                mass_flow_kg_s=default_bypass_m if is_root else 0.0,
                dp=(raw.get("bypass_dp", 0.0) if is_root else None),
            )
        ]
        return spec

    if isinstance(raw_terminals, (str, bytes)) or not isinstance(raw_terminals, Sequence):
        raise ValueError(f"Branch {label!r}: 'terminals' must be a list.")
    if not raw_terminals:
        raise ValueError(
            f"Branch {label!r} has an empty 'terminals' list. The end of a branch must return "
            "its flow somewhere: give it a bypass, a satellite plant, or a sub-branch."
        )

    for index, entry in enumerate(raw_terminals, start=1):
        if not isinstance(entry, Mapping):
            raise ValueError(f"Branch {label!r}: terminal {index} must be a mapping.")
        kind = str(entry.get("type", "bypass")).lower()
        if kind not in TERMINAL_KINDS:
            raise ValueError(
                f"Branch {label!r}, terminal {index}: type must be one of "
                f"{sorted(TERMINAL_KINDS)}, got {kind!r}."
            )
        terminal = TerminalSpec(
            kind=kind,
            label=_terminal_label(entry, kind, index),
            raw=dict(entry),
            mass_flow_kg_s=(None if entry.get("m_kg_s") is None else float(entry["m_kg_s"])),
            dp=(None if entry.get("dp") is None else float(entry["dp"])),
            heading_deg=(
                None if entry.get("heading_deg") is None else float(entry["heading_deg"])
            ),
        )
        if kind == "branch":
            terminal.child = _make_branch_spec(
                entry, label_to_index, None, False, spec, seen_labels
            )
            terminal.label = terminal.child.label
        elif kind == "bypass":
            if terminal.mass_flow_kg_s is None:
                terminal.mass_flow_kg_s = 0.0
        elif kind == "plant" and terminal.mass_flow_kg_s is None:
            raise ValueError(
                f"Branch {label!r}, terminal {terminal.label!r}: a satellite plant needs an "
                "explicit 'm_kg_s' slipstream. How much flow it treats is a dispatch decision, "
                "not something the network can infer."
            )
        spec.terminals.append(terminal)

    labels = [t.label for t in spec.terminals]
    if len(set(labels)) != len(labels):
        raise ValueError(f"Branch {label!r} has duplicate terminal labels: {labels}.")
    return spec


#: Pressure ratio handed to a pipe that `pressure: auto` had to specify itself.
#: A per-pipe drop of a few millibar, which is the right order for a short
#: adiabatic distribution leg and keeps the design solve well conditioned.
AUTO_PIPE_PRESSURE_RATIO = 0.998


def autofill_branch_pressure(
    branch: BranchSpec, pressure_ratio: float = AUTO_PIPE_PRESSURE_RATIO
) -> bool:
    """Fill in a consistent set of pipe pressure ratios for one branch.

    Counting pressure degrees of freedom by hand is the step that stops most
    people writing their first scenario, and the arithmetic is mechanical:
    :func:`~discoolpy.hydraulics.suggest_pressure_specification` already knows
    which elements a ladder of *n* buildings needs fixed. This applies that
    answer, giving every pipe on the list a nominal ``pr`` and leaving the rest
    to be solved.

    Returns True when it filled anything in. It declines, and returns False, on
    a branch that forks, on a trunk link with no buildings, and on any branch
    that already specifies pressure somewhere. A fork needs a modeller to
    decide which parallel path carries the free element, and a branch that
    states any pressure at all is assumed to mean it.
    """
    if len(branch.terminals) != 1 or branch.n_buildings == 0:
        return False
    for raw in branch.pipes.values():
        if not raw:
            continue
        if raw.get("pr") is not None or {"L", "D", "ks"}.issubset(raw):
            return False
    plan = suggest_pressure_specification(
        branch.n_buildings, branch.terminals[0].pressure_spec() is not None
    )
    for key in plan["fixed"]:
        if key == "bypass":
            continue
        branch.pipes.setdefault(key, {})["pr"] = float(pressure_ratio)
    return True


def autofill_network_energy(branches: Sequence[BranchSpec], plant_control: str) -> List[str]:
    """Give every adiabatic pipe the ``Q = 0`` it implies, respecting the loop rule.

    ``heat_model: adiabatic`` says a pipe gains no heat, but TESPy still needs
    that written down as an energy specification, and the closed chilled-water
    loop needs exactly one energy variable left over to absorb its balance.
    Under ``plant_control: supply_temperature`` the chiller duty is that
    variable, so every pipe is specified. Under ``evaporator_duty`` the duty is
    asserted instead, so the last return pipe of the root branch is left free
    and takes the loop residual.

    Only pipes that are adiabatic and silent about their own duty are touched.
    Returns the qualified keys that were filled in.
    """
    candidates: List[Tuple[BranchSpec, str]] = []
    for branch in branches:
        default_heat = str(branch.setting("heat_model", "adiabatic")).lower()
        for key in branch.pipe_keys:
            raw = branch.pipes.get(key) or {}
            if str(raw.get("heat_model", default_heat)).lower() != "adiabatic":
                continue
            if raw.get("Q") is not None or raw.get("Q_W") is not None:
                continue
            candidates.append((branch, key))

    if str(plant_control).lower() == "evaporator_duty" and candidates:
        # The loop needs one free energy variable, and the root branch's last
        # return pipe is the conventional place to leave it.
        root = branches[0]
        slack = (root, root.pipe_keys[-1])
        candidates = [c for c in candidates if c != slack]

    filled: List[str] = []
    for branch, key in candidates:
        branch.pipes.setdefault(key, {})["Q"] = 0.0
        filled.append(branch.qualify(key))
    return filled


def make_branch_specs(config: Mapping[str, Any]) -> BranchSpec:
    """Normalise the scenario's ``branch`` section into a tree of :class:`BranchSpec`.

    A scenario with no ``terminals`` anywhere describes exactly what it always
    did: one branch carrying every building in the order they are written, ending
    in a single bypass valve. Adding ``terminals`` turns the same section into
    the root of a tree.

    Buildings are assigned to branches by label. Within a branch they keep the
    order of that branch's own ``buildings`` list, so the first entry is the
    first tap after the branch inlet and the last is the one nearest the end of
    the line. When no branch lists any buildings, the root takes them all in the
    order of the top-level ``buildings`` section.
    """
    branch_cfg = config.get("branch", {}) or {}
    labels = [str(b["label"]) for b in (config.get("buildings") or [])]
    if not labels:
        raise ValueError("At least one building must be defined in the scenario.")
    if len(set(labels)) != len(labels):
        raise ValueError("Building labels must be unique; they name TESPy components.")
    label_to_index = {name: i for i, name in enumerate(labels)}

    default_bypass_m = (config.get("design", {}) or {}).get("bypass_mass_flow_kg_s")
    root = _make_branch_spec(
        branch_cfg,
        label_to_index,
        None if default_bypass_m is None else float(default_bypass_m),
        True,
        None,
        set(),
    )

    branches = list(root.walk())
    if not any(b.buildings_listed for b in branches):
        root.building_indices = list(range(len(labels)))
    else:
        assigned: Dict[int, str] = {}
        for branch in branches:
            for index in branch.building_indices:
                if index in assigned:
                    raise ValueError(
                        f"Building {labels[index]!r} is listed on both branch "
                        f"{assigned[index]!r} and branch {branch.label!r}. Each building belongs "
                        "to exactly one branch."
                    )
                assigned[index] = branch.label
        missing = [labels[i] for i in range(len(labels)) if i not in assigned]
        if missing:
            raise ValueError(
                "These buildings are defined but placed on no branch: "
                + ", ".join(repr(m) for m in missing)
                + ". Add them to a branch's 'buildings' list."
            )

    for branch in branches:
        unknown = set(branch.pipes) - set(branch.pipe_keys)
        if unknown:
            raise ValueError(
                f"Branch {branch.label!r} defines pipe(s) {sorted(unknown)} that do not exist. "
                f"With {branch.n_buildings} building(s) it has {branch.n_pipes_per_side} supply "
                f"and {branch.n_pipes_per_side} return pipe(s): "
                + ", ".join(branch.pipe_keys)
                + "."
            )

    if bool(branch_cfg.get("autofill", False)):
        for branch in branches:
            autofill_branch_pressure(branch)
        autofill_network_energy(branches, plant_control_mode(config))

    return root


def branch_pipe_thermal(branch: BranchSpec, config: Mapping[str, Any]) -> Dict[str, PipeThermal]:
    """Pipe heat models for one branch of the tree, keyed by local pipe key."""
    return _pipe_thermal_from_section(
        config,
        branch_label=branch.label,
        heat_model=str(branch.setting("heat_model", "adiabatic")),
        thermal_defaults=branch.setting("thermal_defaults", {}) or {},
        pipes=branch.pipes,
    )


def make_network_pipe_thermal(config: Mapping[str, Any]) -> Dict[str, Dict[str, PipeThermal]]:
    """Build every branch's pipe heat models, keyed by branch label."""
    root = make_branch_specs(config)
    return {branch.label: branch_pipe_thermal(branch, config) for branch in root.walk()}


def make_storage_from_section(cfg: Optional[Mapping[str, Any]]) -> Optional[ColdStorage]:
    """Build a :class:`ColdStorage` from a bare ``storage`` mapping.

    Satellite plants carry their store inline rather than under the scenario's
    top-level ``storage`` key, so they need a way in that does not go through
    the whole config.
    """
    return make_storage({"storage": dict(cfg or {})})


# ---------------------------------------------------------------------------
# Scenario validation: catching a typo while it is still a typo
# ---------------------------------------------------------------------------
#
# YAML has no schema, so a misspelled key is not an error, it is a key nobody
# reads. `buidlings:` produces a scenario with no buildings; `Q_design_kW: 150`
# produces a building with no load. Both used to surface as a traceback from
# somewhere deep in the assembly, and the traceback named the line that tripped
# over the absence rather than the line that caused it.
#
# The map below is what a scenario may contain. It is checked against on the
# way in, and the shipped scenarios are tested against it, so it cannot drift
# far from the code without a test going red.
#
# A value of None means "anything goes below here": a mapping whose keys are
# user-chosen (pipe names, tariff hours, fluid names) rather than part of the
# schema.

#: Keys DisCoolPy adds itself when it loads a file.
_INTERNAL_KEYS = {"_config_path", "_config_dir", "_warnings"}

_ENVELOPE_KEYS = {
    "UA_W_K", "solar_aperture_m2", "infiltration_kg_s", "indoor_setpoint_degC",
    "internal_gain_W", "cp_air_J_kgK",
}

_THERMAL_MASS_KEYS = {
    "capacitance_MJ_K", "capacitance_J_K", "setpoint_degC",
    "min_temperature_degC", "max_temperature_degC", "max_cooling_W",
    "precool_max_cooling_W", "indoor_temperature_degC",
}

_BUILDING_KEYS = {
    "label", "Q_design_W", "Q_design_kW", "pr", "archetype", "load_model",
    "envelope", "thermal_mass", "dp",
}

_CHILLER_KEYS = {
    "label", "T_evap_degC", "T_cond_degC", "eta_s", "refrigerant",
    "pressure_ratios", "native_offdesign",
}

_COOLING_TOWER_KEYS = {
    "label", "fluid", "pr", "approach_temperature_K", "native_offdesign",
    "start_mass_flow_kg_s", "condenser_inlet_temperature_degC",
    "condenser_outlet_temperature_degC", "condenser_pressure_bar",
}

_PIPE_KEYS = {
    "L", "D", "ks", "pr", "Q", "Q_W", "heading_deg", "heat_model", "UA_W_K",
    "UA_per_m_W_mK", "ambient_temperature_degC", "ambient_source", "placement",
    "insulation", "insulation_thickness_m", "burial_depth_m", "ground",
    "pipe_wall_thickness_m", "pipe_wall_conductivity_W_mK",
    "internal_film_W_m2K", "twin_spacing_m", "wind_velocity_m_s", "emissivity",
    "material", "environment_media", "zeta",
}

_THERMAL_DEFAULTS_KEYS = {
    "placement", "insulation", "insulation_thickness_m", "burial_depth_m",
    "ground", "pipe_wall_thickness_m", "pipe_wall_conductivity_W_mK",
    "internal_film_W_m2K", "twin_spacing_m", "wind_velocity_m_s", "emissivity",
    "material", "environment_media", "ambient_source",
}

_TERMINAL_KEYS = {
    "type", "label", "m_kg_s", "m", "dp", "heading_deg", "Q_evap_W",
    "Q_evap_kW", "dispatch", "min_load_fraction", "chiller", "cooling_tower",
    "storage", "buildings", "pipes", "terminals", "thermal_defaults",
    "heat_model", "pipe_model", "pump_placement", "autofill", "label_prefix",
    "hydraulic", "auto_relax_pressure", "native_offdesign", "origin_m",
    "pump_label", "bypass_dp",
}

_BRANCH_KEYS = {
    "label", "autofill", "pump_placement", "pump_label", "pump_pressure_ratio",
    "pipe_model", "heat_model", "bypass_dp", "bypass_m_kg_s", "origin_m",
    "heading_deg", "pipes", "thermal_defaults", "terminals", "buildings",
    "native_offdesign", "fix_pump_power", "uniform_pipes", "uniform_pipe_attrs",
    "hydraulic", "auto_relax_pressure",
}

_STORAGE_KEYS = {
    "enabled", "label", "storage_type", "capacity_kWh", "initial_soc",
    "max_charge_kW", "max_discharge_kW", "charge_efficiency",
    "discharge_efficiency", "min_soc", "max_soc", "target_chiller_load_kW",
    "coupling", "hydraulic_pr", "loss_model", "standby_loss_fraction_per_day",
    "thermal", "stratified", "minimum_load_fraction", "max_tank_flow_kg_s",
}

_STORAGE_THERMAL_KEYS = {
    "volume_m3", "insulation", "insulation_thickness_m", "height_to_diameter",
    "internal_film_W_m2K", "external_film_W_m2K", "buried_fraction", "ground",
    "UA_W_K", "storage_temperature_degC", "discharged_temperature_degC",
    "temperature_varies_with_soc",
}

_STRATIFIED_KEYS = {
    "enabled", "layers", "height_to_diameter", "charged_temperature_degC",
    "discharged_temperature_degC", "volume_m3", "insulation",
    "insulation_thickness_m", "conductivity_W_mK", "max_tank_flow_kg_s",
    "height_m", "diameter_m", "wall_htc_W_m2K", "vertical_conductivity_W_mK",
    "max_courant", "max_sub_steps", "initial_soc",
}

_DESIGN_KEYS = {
    "supply_temperature_degC", "building_return_temperature_degC",
    "ambient_temperature_degC", "ground_temperature_degC",
    "condenser_inlet_temperature_degC", "condenser_outlet_temperature_degC",
    "chilled_water_pressure_bar", "condenser_pressure_bar", "cp_water_J_kgK",
    "pump_efficiency", "pump_power_W", "bypass_mass_flow_kg_s",
    "plant_control", "auto_relax_pressure",
}

_PROFILE_KEYS = {
    "start", "periods", "freq", "daily_high_degC", "daily_low_degC",
    "peak_solar_W_m2", "ground", "archetypes", "load_fraction_floor",
}

#: The whole scenario. A ``None`` value means the keys below are the user's own.
SCENARIO_KEYS: Dict[str, Any] = {
    "metadata": {"name", "description", "author", "version", "doi", "citation"},
    "network": {"chilled_water_cycle_closer", "condenser_cycle_closer"},
    "design": _DESIGN_KEYS,
    "chiller": _CHILLER_KEYS,
    "cooling_tower": _COOLING_TOWER_KEYS,
    "buildings": _BUILDING_KEYS,
    "branch": _BRANCH_KEYS,
    "storage": _STORAGE_KEYS,
    "profiles": _PROFILE_KEYS,
    "economics": {"tariff", "carbon_intensity_kg_kWh", "currency",
                  "demand_charge_per_kW"},
    "solver": {"iterinfo", "max_iter", "progress_interval",
               "initial_pressure_step_bar"},
    "outputs": {"output_dir", "profile_csv", "results_csv",
                "without_storage_csv", "with_storage_csv", "comparison_csv",
                "summary_md", "plot_png", "flexible_csv", "reference_csv",
                "baseline_csv", "layout_png", "report_dir"},
}

#: Sections that must be a list of mappings rather than a mapping.
_LIST_SECTIONS = {"buildings"}

#: Nested mappings whose keys are checked too, keyed by their path.
_NESTED: Dict[str, Any] = {
    "buildings.envelope": _ENVELOPE_KEYS,
    "buildings.thermal_mass": _THERMAL_MASS_KEYS,
    "branch.thermal_defaults": _THERMAL_DEFAULTS_KEYS,
    "branch.pipes": None,                       # pipe names are the user's
    "branch.pipes.*": _PIPE_KEYS,
    "branch.uniform_pipe_attrs": None,
    "chiller.pressure_ratios": {"evap_1", "evap_2", "cond_1", "cond_2"},
    "chiller.native_offdesign": {"enabled", "evaporator_ttd_l",
                                 "condenser_ttd_u",
                                 "use_pressure_loss_characteristics"},
    "cooling_tower.fluid": None,
    "storage.thermal": _STORAGE_THERMAL_KEYS,
    "storage.stratified": _STRATIFIED_KEYS,
    "profiles.ground": {"depth_m", "mean_annual_degC", "annual_amplitude_K",
                        "diffusivity_m2_s", "phase_shift_days"},
    "profiles.archetypes": None,
    "economics.tariff": None,
}

#: Below this a building load is almost certainly kilowatts typed into a watts
#: key. A 10 kW substation on a district network does not exist.
_IMPLAUSIBLE_BUILDING_LOAD_W = 10_000.0


def _within_one_edit(a: str, b: str) -> bool:
    """True when ``a`` becomes ``b`` by one insertion, deletion or substitution.

    difflib's similarity ratio is a poor judge of short keys: ``DD`` against
    ``D`` scores 0.67, below any cutoff that does not also start matching
    unrelated long names. A single-character slip is the commonest typo there
    is, so it gets its own test.
    """
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(long)):
        if long[:i] + long[i + 1:] == short:
            return True
    return False


def _suggest(name: str, known: Iterable[str]) -> Optional[str]:
    """The closest real key to ``name``, when one is close enough to be a typo."""
    name = str(name)
    candidates = sorted(known)
    # A pure case slip first: 'degc' for 'degC' is the one this catches most.
    for candidate in candidates:
        if candidate != name and candidate.lower() == name.lower():
            return candidate
    for candidate in candidates:
        if _within_one_edit(name, candidate):
            return candidate
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.72)
    return matches[0] if matches else None


def _check_keys(
    section: Mapping[str, Any],
    known: Optional[Iterable[str]],
    where: str,
    path: Optional[Any],
    warnings_out: List[str],
) -> None:
    """Compare one mapping's keys against the schema.

    A key close to a real one is a typo and stops the run, because the scenario
    it produces is not the one that was written. A key close to nothing is left
    alone with a warning: scenarios carry notes and private annotations, and
    refusing those would make the check a nuisance rather than a help.
    """
    if known is None:
        return
    for key in section:
        if key in known or key in _INTERNAL_KEYS:
            continue
        suggestion = _suggest(key, known)
        if suggestion is not None:
            raise ScenarioError(
                f"unknown key {key!r}",
                where=f"{where}.{key}" if where else str(key),
                hint=f"did you mean {suggestion!r}?",
                path=path,
            )
        warnings_out.append(
            f"{where}.{key}: unknown key, ignored. "
            f"Known keys here: {', '.join(sorted(known))}."
            if where else f"{key}: unknown top-level section, ignored."
        )


def _require_mapping(value: Any, where: str, path: Optional[Any]) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ScenarioError(
            f"expected a mapping of settings, got {type(value).__name__}",
            where=where,
            hint=f"write it as '{where}:' followed by indented 'key: value' lines.",
            path=path,
        )
    return value


def validate_scenario(config: Mapping[str, Any], strict: bool = False) -> List[str]:
    """Check a scenario's structure before anything is built from it.

    Catches the three mistakes that a YAML file makes silently: a misspelled
    key, a section written as the wrong shape, and a building with no load.
    Returns the warnings it did not consider fatal.

    Raises
    ------
    ScenarioError
        For a key that is a near-miss of a real one, a section of the wrong
        type, or a missing requirement. With ``strict``, for any warning too.
    """
    path = config.get("_config_path")
    warnings_out: List[str] = []

    if not isinstance(config, Mapping):
        raise ScenarioError(
            f"a scenario must be a mapping at the top level, got {type(config).__name__}",
            hint="the file should start with sections like 'buildings:' and 'branch:'.",
            path=path,
        )

    _check_keys(config, set(SCENARIO_KEYS), "", path, warnings_out)

    for name, known in SCENARIO_KEYS.items():
        if name not in config or config[name] is None:
            continue
        value = config[name]
        if name in _LIST_SECTIONS:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ScenarioError(
                    f"expected a list, got {type(value).__name__}",
                    where=name,
                    hint=f"write '{name}:' followed by '  - ' entries, one per item.",
                    path=path,
                )
            continue
        section = _require_mapping(value, name, path)
        _check_keys(section, known, name, path, warnings_out)
        for sub, sub_known in _NESTED.items():
            head, _, tail = sub.partition(".")
            # sub_known None means the keys below are the user's own, so there
            # is nothing to check and nothing to insist about the shape either:
            # a tariff is a mapping by hour or a single flat price.
            if head != name or not tail or "." in tail or sub_known is None:
                continue
            if tail in section and section[tail] is not None:
                nested = _require_mapping(section[tail], f"{name}.{tail}", path)
                _check_keys(nested, sub_known, f"{name}.{tail}", path, warnings_out)

    _validate_pipes(config, path, warnings_out)
    _validate_buildings(config, path, warnings_out)

    if strict and warnings_out:
        raise ScenarioError(
            f"{len(warnings_out)} warning(s), and strict checking is on",
            extra=warnings_out,
            hint="fix them, or drop --strict.",
            path=path,
        )
    return warnings_out


def _validate_pipes(
    config: Mapping[str, Any], path: Optional[Any], warnings_out: List[str]
) -> None:
    """Check each pipe's own keys, wherever in the tree the pipe sits."""
    def walk(section: Mapping[str, Any], where: str) -> None:
        pipes = section.get("pipes")
        if isinstance(pipes, Mapping):
            for key, spec in pipes.items():
                if spec is None:
                    continue
                mapping = _require_mapping(spec, f"{where}.pipes.{key}", path)
                _check_keys(mapping, _PIPE_KEYS, f"{where}.pipes.{key}", path,
                            warnings_out)
        defaults = section.get("thermal_defaults")
        if isinstance(defaults, Mapping):
            _check_keys(defaults, _THERMAL_DEFAULTS_KEYS,
                        f"{where}.thermal_defaults", path, warnings_out)
        terminals = section.get("terminals")
        if isinstance(terminals, Sequence) and not isinstance(terminals, (str, bytes)):
            for i, terminal in enumerate(terminals):
                if not isinstance(terminal, Mapping):
                    raise ScenarioError(
                        f"expected a mapping, got {type(terminal).__name__}",
                        where=f"{where}.terminals[{i}]",
                        hint="each terminal is a '- type: ...' entry with its own keys.",
                        path=path,
                    )
                _check_keys(terminal, _TERMINAL_KEYS, f"{where}.terminals[{i}]",
                            path, warnings_out)
                walk(terminal, f"{where}.terminals[{i}]")

    branch = config.get("branch")
    if isinstance(branch, Mapping):
        walk(branch, "branch")


def _validate_buildings(
    config: Mapping[str, Any], path: Optional[Any], warnings_out: List[str]
) -> None:
    """Every building needs a unique label and a plausible design load."""
    buildings = config.get("buildings")
    if not buildings:
        raise ScenarioError(
            "no buildings",
            hint=("a district cooling network needs at least one load. Add a "
                  "'buildings:' section with '- {label: building_1, "
                  "Q_design_W: 150000}'."),
            path=path,
        )

    seen: Dict[str, int] = {}
    for i, item in enumerate(buildings):
        where = f"buildings[{i}]"
        if not isinstance(item, Mapping):
            raise ScenarioError(
                f"expected a mapping, got {type(item).__name__}",
                where=where,
                hint="each building is a '- label: ...' entry with its own keys.",
                path=path,
            )
        _check_keys(item, _BUILDING_KEYS, where, path, warnings_out)

        label = item.get("label")
        if not label:
            raise ScenarioError(
                "no label",
                where=where,
                hint="every building needs a unique 'label'; it names the TESPy "
                     "components and every result column.",
                path=path,
            )
        if str(label) in seen:
            raise ScenarioError(
                f"duplicate label {str(label)!r}, already used by buildings[{seen[str(label)]}]",
                where=where,
                hint="labels name result columns, so they have to be unique.",
                path=path,
            )
        seen[str(label)] = i

        watts = item.get("Q_design_W")
        kilowatts = item.get("Q_design_kW")
        if watts is None and kilowatts is None:
            raise ScenarioError(
                "no design load",
                where=where,
                hint="give 'Q_design_W' in watts, or 'Q_design_kW' in kilowatts.",
                path=path,
            )
        if watts is not None and kilowatts is not None:
            raise ScenarioError(
                "both 'Q_design_W' and 'Q_design_kW'",
                where=where,
                hint="give one of them, not both.",
                path=path,
            )
        value = float(watts) if watts is not None else float(kilowatts) * 1e3
        if value <= 0:
            raise ScenarioError(
                f"design load is {value:g} W",
                where=where,
                hint="a building on a cooling network consumes cooling, so the "
                     "load must be positive.",
                path=path,
            )
        if watts is not None and value < _IMPLAUSIBLE_BUILDING_LOAD_W:
            warnings_out.append(
                f"{where}.Q_design_W: {value:g} W is very small for a district "
                f"substation. If you meant {value:g} kW, use 'Q_design_kW'."
            )
