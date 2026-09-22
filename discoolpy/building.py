"""Reusable TESPy building module for district cooling networks.

A Building wraps a TESPy SimpleHeatExchanger and makes the two connections from
a supply splitter or tap through to a return merge. On top of that core it can
say where the cooling load comes from:

``load_model = "profile"``
    Whatever demand the profile or snapshot hands in is the whole story.

``load_model = "envelope"``
    The profile supplies internal and process gains only, and an
    :class:`~discoolpy.thermal.EnvelopeThermal` adds the weather-driven part,
    ``UA*(T_air - T_indoor) + solar + infiltration``. The building now responds
    to ambient conditions rather than replaying a fixed curve.

``load_model = "thermal_mass"``
    The same, with a first-order RC model of the indoor temperature on top.
    Indoor temperature becomes a state instead of a constant, so the building
    can be pre-cooled below setpoint and left to coast back up. That is the
    largest single source of demand-side flexibility in a district-cooling
    system, and a scalar ``Q`` cannot represent it at all.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from tespy.components import SimpleHeatExchanger
from tespy.connections import Connection

from .thermal import EnvelopeThermal

_LOAD_MODELS = {"profile", "envelope", "thermal_mass"}


@dataclass
class ThermalMassState:
    """Result of advancing a building's indoor temperature by one time step."""

    indoor_temperature_before_degC: float
    indoor_temperature_after_degC: float
    cooling_delivered_W: float
    gain_W: float
    mode: str
    stored_energy_change_kWh: float
    setpoint_deviation_K: float
    #: How far outside the comfort band the zone ended, in K. Non-zero only when
    #: the terminal units ran out of capacity, i.e. the coil is undersized for
    #: the weather. Reported rather than silently tolerated, because otherwise a
    #: scenario can appear to deliver flexibility it is actually stealing from
    #: occupant comfort.
    band_violation_K: float = 0.0
    capacity_limited: bool = False

    def to_record(self, prefix: str) -> Dict[str, float]:
        return {
            f"{prefix}_T_indoor_before_degC": self.indoor_temperature_before_degC,
            f"{prefix}_T_indoor_degC": self.indoor_temperature_after_degC,
            f"{prefix}_cooling_W": self.cooling_delivered_W,
            f"{prefix}_gain_W": self.gain_W,
            f"{prefix}_mass_mode": self.mode,
            f"{prefix}_stored_energy_change_kWh": self.stored_energy_change_kWh,
            f"{prefix}_setpoint_deviation_K": self.setpoint_deviation_K,
            f"{prefix}_band_violation_K": self.band_violation_K,
            f"{prefix}_capacity_limited": self.capacity_limited,
        }


@dataclass
class ThermalMass:
    """First-order (1R1C) indoor-temperature model of a conditioned building.

    ``C dT/dt = UA*(T_air - T) + G - Q_cool``

    The step is integrated **exactly** rather than with forward Euler:

        T(t+dt) = T_inf + (T(t) - T_inf) * exp(-dt/tau),
        tau = C/UA,  T_inf = T_air + (G - Q_cool)/UA

    Exact integration matters here because a comfort band is only a few kelvin
    wide while the time constant of a real building is many hours; an explicit
    step large enough to be useful would overshoot the band and report
    flexibility that does not exist.

    Parameters
    ----------
    capacitance_J_K:
        Effective thermal capacitance of the conditioned zone including
        furniture and exposed structure.
    setpoint_degC, min_temperature_degC, max_temperature_degC:
        Nominal setpoint and the comfort band the operator is willing to use.
        The width of this band *is* the flexibility resource.
    max_cooling_W:
        Terminal-unit capacity limit. Pre-cooling cannot proceed faster than the
        coils in the building can deliver.
    precool_max_cooling_W:
        Separate, usually lower, limit applied only while pre-cooling. Without
        one, "cool to the band floor" means "draw full coil capacity the instant
        pre-cooling starts", and if several buildings do that together the
        pre-cooling block becomes a **larger** peak than the afternoon it was
        meant to avoid. Rate-limiting is what makes pre-cooling useful rather
        than merely load-shifting the problem into the night.
    """

    capacitance_J_K: float
    setpoint_degC: float = 24.0
    min_temperature_degC: float = 21.0
    max_temperature_degC: float = 26.0
    max_cooling_W: Optional[float] = None
    precool_max_cooling_W: Optional[float] = None
    indoor_temperature_degC: Optional[float] = None

    def __post_init__(self) -> None:
        if self.capacitance_J_K <= 0:
            raise ValueError("capacitance_J_K must be positive.")
        if not self.min_temperature_degC <= self.setpoint_degC <= self.max_temperature_degC:
            raise ValueError(
                "Comfort band must satisfy min_temperature <= setpoint <= max_temperature."
            )
        if self.indoor_temperature_degC is None:
            self.indoor_temperature_degC = float(self.setpoint_degC)

    def reset(self, indoor_temperature_degC: Optional[float] = None) -> None:
        self.indoor_temperature_degC = float(
            self.setpoint_degC if indoor_temperature_degC is None else indoor_temperature_degC
        )

    @staticmethod
    def _decay(dt_h: float, tau_h: float) -> float:
        return math.exp(-float(dt_h) / max(float(tau_h), 1e-9))

    def _cooling_to_reach(
        self, target_degC: float, dt_h: float, UA_W_K: float, ambient_degC: float, gain_W: float
    ) -> float:
        """Cooling power that lands exactly on ``target_degC`` after ``dt_h``."""
        tau_h = self.capacitance_J_K / (UA_W_K * 3600.0)
        decay = self._decay(dt_h, tau_h)
        if abs(1.0 - decay) < 1e-12:
            return max(0.0, UA_W_K * (ambient_degC - target_degC) + gain_W)
        t_inf = (float(target_degC) - self.indoor_temperature_degC * decay) / (1.0 - decay)
        return UA_W_K * (float(ambient_degC) - t_inf) + float(gain_W)

    def _integrate(
        self, cooling_W: float, dt_h: float, UA_W_K: float, ambient_degC: float, gain_W: float
    ) -> float:
        tau_h = self.capacitance_J_K / (UA_W_K * 3600.0)
        decay = self._decay(dt_h, tau_h)
        t_inf = float(ambient_degC) + (float(gain_W) - float(cooling_W)) / UA_W_K
        return t_inf + (self.indoor_temperature_degC - t_inf) * decay

    def step(
        self,
        dt_h: float,
        UA_W_K: float,
        ambient_degC: float,
        gain_W: float,
        mode: str = "track",
        cooling_W: Optional[float] = None,
    ) -> ThermalMassState:
        """Advance the indoor temperature by ``dt_h`` hours.

        ``mode`` is one of:

        ``track``    hold the setpoint (the reference, zero-flexibility case);
        ``precool``  cool towards the band floor, limited by ``precool_max_cooling_W``;
        ``coast``    deliver no cooling and let the zone drift, capped at the band ceiling;
        ``recover``  return towards the setpoint at the pre-cooling rate limit;
        ``manual``   deliver exactly ``cooling_W``, clipped to keep the zone in band.

        ``recover`` exists because snapping straight back to ``track`` after a
        coast period is the classic demand-response own goal: the zone sits at
        its ceiling, the controller asks for the setpoint in one step, and every
        coil in the district goes to full output simultaneously. The resulting
        rebound peak can be larger than the peak the event was called to avoid.
        Recovering at the pre-cooling rate spreads it out.
        """
        if UA_W_K <= 0:
            raise ValueError("Thermal-mass stepping needs a positive envelope UA.")
        before = float(self.indoor_temperature_degC)
        mode = str(mode).lower()

        if mode in {"track", "recover"}:
            request = self._cooling_to_reach(self.setpoint_degC, dt_h, UA_W_K, ambient_degC, gain_W)
        elif mode == "precool":
            request = self._cooling_to_reach(
                self.min_temperature_degC, dt_h, UA_W_K, ambient_degC, gain_W
            )
        elif mode == "coast":
            request = 0.0
        elif mode == "manual":
            if cooling_W is None:
                raise ValueError("mode='manual' requires cooling_W.")
            request = float(cooling_W)
        else:
            raise ValueError(f"Unsupported thermal-mass mode {mode!r}.")

        request = max(0.0, request)
        cap = self.max_cooling_W
        if mode in {"precool", "recover"} and self.precool_max_cooling_W is not None:
            cap = min(self.precool_max_cooling_W, cap if cap is not None else float("inf"))
        if cap is not None:
            request = min(request, float(cap))

        after = self._integrate(request, dt_h, UA_W_K, ambient_degC, gain_W)
        # Enforce the comfort band: never overshoot below the floor, and always
        # spend whatever cooling is needed to stay under the ceiling.
        capacity_limited = False
        if after < self.min_temperature_degC - 1e-9:
            request = max(
                0.0,
                self._cooling_to_reach(
                    self.min_temperature_degC, dt_h, UA_W_K, ambient_degC, gain_W
                ),
            )
            if cap is not None:
                request = min(request, float(cap))
            after = self._integrate(request, dt_h, UA_W_K, ambient_degC, gain_W)
        elif after > self.max_temperature_degC + 1e-9:
            needed = max(
                0.0,
                self._cooling_to_reach(
                    self.max_temperature_degC, dt_h, UA_W_K, ambient_degC, gain_W
                ),
            )
            request = max(request, needed)
            # Holding the ceiling is a comfort obligation, not a control choice,
            # so it may use the full coil rather than the pre-cooling limit.
            if self.max_cooling_W is not None:
                capacity_limited = request > float(self.max_cooling_W) + 1e-6
                request = min(request, float(self.max_cooling_W))
            after = self._integrate(request, dt_h, UA_W_K, ambient_degC, gain_W)

        self.indoor_temperature_degC = after
        violation = max(
            0.0,
            after - self.max_temperature_degC,
            self.min_temperature_degC - after,
        )
        stored = self.capacitance_J_K * (before - after) / 3.6e6  # kWh of "coolth" added
        return ThermalMassState(
            indoor_temperature_before_degC=before,
            indoor_temperature_after_degC=after,
            cooling_delivered_W=request,
            gain_W=float(gain_W),
            mode=mode,
            stored_energy_change_kWh=stored,
            setpoint_deviation_K=after - self.setpoint_degC,
            band_violation_K=violation,
            capacity_limited=capacity_limited,
        )

    def flexibility_W(
        self, dt_h: float, UA_W_K: float, ambient_degC: float, gain_W: float
    ) -> Dict[str, float]:
        """Up/down cooling-power flexibility available for the next ``dt_h`` hours.

        ``increase_W`` is how much *extra* cooling the zone could absorb by
        pre-cooling to the band floor; ``decrease_W`` is how much cooling could
        be shed by coasting to the band ceiling. Both are measured against the
        setpoint-tracking baseline, so they are directly comparable to a
        demand-response bid.

        Power alone does not say how long a bid can be held, so the result also
        carries the **energy** headroom implied by the remaining band width:
        ``precool_energy_kWh`` is what the zone can still absorb before hitting
        the floor, ``coast_energy_kWh`` what it can still shed before hitting
        the ceiling. Both go to zero at the respective bound even while the
        corresponding power figure is still large, since a zone already held at the
        floor can keep drawing power, but it cannot store any more.
        """
        baseline = max(
            0.0, self._cooling_to_reach(self.setpoint_degC, dt_h, UA_W_K, ambient_degC, gain_W)
        )
        floor = max(
            0.0, self._cooling_to_reach(self.min_temperature_degC, dt_h, UA_W_K, ambient_degC, gain_W)
        )
        ceiling = max(
            0.0, self._cooling_to_reach(self.max_temperature_degC, dt_h, UA_W_K, ambient_degC, gain_W)
        )
        if self.max_cooling_W is not None:
            floor = min(floor, float(self.max_cooling_W))
            baseline = min(baseline, float(self.max_cooling_W))
        return {
            "baseline_W": baseline,
            "increase_W": max(0.0, floor - baseline),
            "decrease_W": max(0.0, baseline - ceiling),
            "precool_energy_kWh": max(
                0.0,
                self.capacitance_J_K
                * (self.indoor_temperature_degC - self.min_temperature_degC)
                / 3.6e6,
            ),
            "coast_energy_kWh": max(
                0.0,
                self.capacitance_J_K
                * (self.max_temperature_degC - self.indoor_temperature_degC)
                / 3.6e6,
            ),
        }


@dataclass
class Building:
    """Building load module based on a TESPy SimpleHeatExchanger.

    Parameters
    ----------
    label:
        Human-readable building label.
    Q_design:
        Design cooling demand in W. Use a positive value because heat is added
        to the chilled-water stream inside the building heat exchanger.
    pr:
        Optional pressure ratio across the building heat exchanger. Leave as
        ``None`` if the branch hydraulic spanning tree should not include this
        building pressure equation.
    demand_profile:
        Optional list of hourly cooling demands in W. Positive values are used
        directly as heat gains to the chilled-water stream.
    load_model:
        ``"profile"``, ``"envelope"`` or ``"thermal_mass"``. See the module
        docstring.
    envelope:
        Weather-driven gain description, required for the non-profile models.
    thermal_mass:
        RC zone model, required for ``load_model="thermal_mass"``.
    """

    label: str
    Q_design: float
    pr: Optional[float] = None
    demand_profile: Optional[List[float]] = None
    load_model: str = "profile"
    envelope: Optional[EnvelopeThermal] = None
    thermal_mass: Optional[ThermalMass] = None
    heat_exchanger: SimpleHeatExchanger = field(init=False)
    inlet: Optional[Connection] = field(default=None, init=False)
    outlet: Optional[Connection] = field(default=None, init=False)
    #: Breakdown of the most recently applied demand, for reporting.
    last_breakdown: Dict[str, float] = field(default_factory=dict, init=False)
    #: Most recent thermal-mass step, when one applies.
    last_mass_state: Optional[ThermalMassState] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.heat_exchanger = SimpleHeatExchanger(self.label)
        self.load_model = str(self.load_model).lower()
        if self.load_model not in _LOAD_MODELS:
            raise ValueError(f"load_model must be one of {sorted(_LOAD_MODELS)}.")
        if self.load_model != "profile" and self.envelope is None:
            raise ValueError(
                f"Building {self.label!r} uses load_model={self.load_model!r} but has no envelope. "
                "Provide an EnvelopeThermal with at least a UA_W_K."
            )
        if self.load_model == "thermal_mass":
            if self.thermal_mass is None:
                raise ValueError(
                    f"Building {self.label!r} uses load_model='thermal_mass' but has no ThermalMass."
                )
            if self.envelope is not None and self.envelope.UA_W_K <= 0:
                raise ValueError(
                    f"Building {self.label!r} needs a positive envelope UA_W_K for the RC model."
                )

    @property
    def component(self) -> SimpleHeatExchanger:
        """Return the underlying TESPy component."""
        return self.heat_exchanger

    @property
    def indoor_temperature_degC(self) -> Optional[float]:
        """Current indoor temperature when a thermal-mass model is in use."""
        if self.thermal_mass is not None:
            return self.thermal_mass.indoor_temperature_degC
        if self.envelope is not None:
            return self.envelope.indoor_setpoint_degC
        return None

    def connect_between(
        self,
        supply_component,
        supply_port: str,
        return_component,
        return_port: str,
        inlet_label: Optional[str] = None,
        outlet_label: Optional[str] = None,
    ) -> Sequence[Connection]:
        """Create connections from a supply node through the building to a return node."""
        safe = self.label.replace(" ", "_").lower()
        self.inlet = Connection(
            supply_component,
            supply_port,
            self.heat_exchanger,
            "in1",
            label=inlet_label or f"{safe}_in",
        )
        self.outlet = Connection(
            self.heat_exchanger,
            "out1",
            return_component,
            return_port,
            label=outlet_label or f"{safe}_out",
        )
        return self.inlet, self.outlet

    def set_design(self, mass_flow: Optional[float] = None, native_offdesign: bool = False) -> None:
        """Apply the design heat load and optional branch mass-flow anchor."""
        attrs: Dict[str, float] = {"Q": self.Q_design}
        if self.pr is not None:
            attrs["pr"] = self.pr
            if native_offdesign:
                attrs["design"] = ["pr"]
                attrs["offdesign"] = ["zeta"]
        self.heat_exchanger.set_attr(**attrs)
        if mass_flow is not None:
            if self.inlet is None:
                raise RuntimeError("Create building connections before setting mass flow.")
            self.inlet.set_attr(m=mass_flow)

    def load_hourly_demand_from_csv(
        self,
        csv_path: str,
        column: str = "Q",
        delimiter: str = ",",
        multiplier: float = 1.0,
    ) -> List[float]:
        """Load an hourly demand profile from a CSV column."""
        values: List[float] = []
        with open(csv_path, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            if column not in (reader.fieldnames or []):
                raise ValueError(f"Column '{column}' not found in {csv_path}.")
            for row in reader:
                values.append(float(row[column]) * multiplier)
        self.demand_profile = values
        return values

    def set_hourly_demand(self, hour_index: int) -> float:
        """Apply one hourly demand value to the TESPy heat exchanger."""
        if self.demand_profile is None:
            raise RuntimeError("No demand profile has been loaded.")
        return self.set_demand(self.demand_profile[hour_index])

    def set_demand(self, q_W: float) -> float:
        """Apply a positive building cooling demand to the TESPy heat exchanger."""
        q = float(q_W)
        if q < 0:
            raise ValueError("Building cooling demand must be positive in W.")
        self.heat_exchanger.set_attr(Q=q)
        return q

    # ------------------------------------------------------------------
    # Weather-driven demand
    # ------------------------------------------------------------------

    def compute_demand(
        self,
        base_load_W: float,
        ambient_temperature_degC: Optional[float] = None,
        solar_irradiance_W_m2: float = 0.0,
        dt_h: float = 1.0,
        mode: str = "track",
        cooling_W: Optional[float] = None,
    ) -> Dict[str, float]:
        """Return the demand breakdown for one time step, without applying it.

        ``base_load_W`` is the profile value: total demand under
        ``load_model="profile"``, internal/process gain otherwise.
        """
        base = float(base_load_W)
        if self.load_model == "profile" or self.envelope is None:
            breakdown = {
                "internal_W": base,
                "conduction_W": 0.0,
                "infiltration_W": 0.0,
                "solar_W": 0.0,
                "envelope_gain_W": 0.0,
                "total_gain_W": base,
                "demand_W": max(0.0, base),
                "stored_energy_change_kWh": 0.0,
                "T_indoor_degC": (
                    self.envelope.indoor_setpoint_degC if self.envelope is not None else float("nan")
                ),
            }
            self.last_mass_state = None
            return breakdown

        if ambient_temperature_degC is None:
            raise ValueError(
                f"Building {self.label!r} uses load_model={self.load_model!r} and therefore needs "
                "an ambient temperature on the snapshot."
            )

        indoor = (
            self.thermal_mass.indoor_temperature_degC
            if self.load_model == "thermal_mass" and self.thermal_mass is not None
            else self.envelope.indoor_setpoint_degC
        )
        parts = self.envelope.gain_W(
            ambient_temperature_degC=ambient_temperature_degC,
            solar_irradiance_W_m2=solar_irradiance_W_m2,
            indoor_temperature_degC=indoor,
            internal_gain_W=base,
        )
        envelope_gain = parts["conduction_W"] + parts["infiltration_W"] + parts["solar_W"]

        if self.load_model == "envelope" or self.thermal_mass is None:
            demand = max(0.0, parts["total_gain_W"])
            stored = 0.0
            self.last_mass_state = None
        else:
            non_conduction = parts["solar_W"] + parts["internal_W"] + parts["infiltration_W"]
            state = self.thermal_mass.step(
                dt_h=dt_h,
                UA_W_K=self.envelope.UA_W_K,
                ambient_degC=float(ambient_temperature_degC),
                gain_W=non_conduction,
                mode=mode,
                cooling_W=cooling_W,
            )
            demand = state.cooling_delivered_W
            stored = state.stored_energy_change_kWh
            self.last_mass_state = state
            indoor = state.indoor_temperature_after_degC

        breakdown = {
            "internal_W": parts["internal_W"],
            "conduction_W": parts["conduction_W"],
            "infiltration_W": parts["infiltration_W"],
            "solar_W": parts["solar_W"],
            "envelope_gain_W": envelope_gain,
            "total_gain_W": parts["total_gain_W"],
            "demand_W": demand,
            "stored_energy_change_kWh": stored,
            "T_indoor_degC": indoor,
        }
        return breakdown

    def set_snapshot_demand(self, snapshot) -> float:
        """Read this building's load from a TimeSnapshot, expand it, and apply it."""
        if hasattr(snapshot, "get_building_load"):
            base = snapshot.get_building_load(self.label)
        elif hasattr(snapshot, "building_loads"):
            base = snapshot.building_loads[self.label]
        else:
            raise TypeError("snapshot must expose get_building_load() or building_loads.")

        if self.load_model == "profile":
            self.last_breakdown = self.compute_demand(base)
            return self.set_demand(self.last_breakdown["demand_W"])

        metadata = getattr(snapshot, "metadata", None) or {}
        get = metadata.get if hasattr(metadata, "get") else (lambda *_: None)
        solar = get("solar_irradiance_W_m2", None)
        if solar is None:
            solar = get(f"{self.label}_solar_irradiance_W_m2", 0.0) or 0.0
        mode = get(f"{self.label}_mass_mode", None) or get("mass_mode", None) or "track"
        cooling = get(f"{self.label}_cooling_W", None)
        resolution = getattr(snapshot, "resolution", None)
        dt_h = resolution.total_seconds() / 3600.0 if hasattr(resolution, "total_seconds") else 1.0

        self.last_breakdown = self.compute_demand(
            base_load_W=base,
            ambient_temperature_degC=getattr(snapshot, "ambient_temperature", None),
            solar_irradiance_W_m2=float(solar),
            dt_h=dt_h,
            mode=str(mode),
            cooling_W=None if cooling is None else float(cooling),
        )
        return self.set_demand(self.last_breakdown["demand_W"])

    def flexibility_W(
        self, dt_h: float, ambient_temperature_degC: float, solar_irradiance_W_m2: float = 0.0,
        internal_gain_W: float = 0.0,
    ) -> Dict[str, float]:
        """Up/down demand flexibility of this building for the next ``dt_h`` hours."""
        if self.thermal_mass is None or self.envelope is None:
            return {"baseline_W": float("nan"), "increase_W": 0.0, "decrease_W": 0.0}
        parts = self.envelope.gain_W(
            ambient_temperature_degC=ambient_temperature_degC,
            solar_irradiance_W_m2=solar_irradiance_W_m2,
            indoor_temperature_degC=self.thermal_mass.indoor_temperature_degC,
            internal_gain_W=internal_gain_W,
        )
        return self.thermal_mass.flexibility_W(
            dt_h=dt_h,
            UA_W_K=self.envelope.UA_W_K,
            ambient_degC=float(ambient_temperature_degC),
            gain_W=parts["solar_W"] + parts["internal_W"] + parts["infiltration_W"],
        )

    def set_start(self, m: float, T_in: float, T_out: float, p_in: float, p_out: float) -> None:
        """Set TESPy starting values for the building inlet and outlet connections."""
        if self.inlet is None or self.outlet is None:
            raise RuntimeError("Create building connections before setting start values.")
        self.inlet.m.set_val0(m)
        self.inlet.T.set_val0(T_in)
        self.inlet.p.set_val0(p_in)
        self.outlet.m.set_val0(m)
        self.outlet.T.set_val0(T_out)
        self.outlet.p.set_val0(p_out)


__all__ = ["Building", "ThermalMass", "ThermalMassState"]
