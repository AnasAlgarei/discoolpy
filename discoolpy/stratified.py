"""One-dimensional stratified chilled-water tank.

A :class:`~discoolpy.cold_storage.ColdStorage` on its own is a single number,
stored cooling in kWh. For most district planning that is the right resolution.
It cannot answer the question a chilled-water store raises in operation: at
what temperature does the cooling come out, and for how long?

A real stratified tank has no membrane. Cold water sits at the bottom because
it is denser, warm return water floats on top, and a thermocline a fraction of
a metre thick separates them. Cycle the tank and that thermocline thickens,
partly by conduction and mostly by the mixing every inflow causes, so water
leaving the cold port warms up well before the tank is empty. A scalar state of
charge says nothing about this, and a plant sized on one runs short of capacity
at the worst moment.

The model
---------
The tank is divided into ``layers`` equal-volume nodes, indexed **0 at the
bottom** (coldest) to ``n-1`` at the top (warmest). Each node is treated as
fully mixed, so the stack is a chain of stirred tanks in series. That is the
standard 1-D multinode formulation, and the same structure mosaik-heatpump uses
for its hot water tank. District cooling inverts the direction of use: charging
pushes cold water in at the bottom and displaces warm water out of the top,
while discharging draws cold from the bottom as warm return water enters the
top.

Each node carries, per unit time,

* advection from its upstream neighbour at the through-flow rate,
* ambient gain ``UA_i (T_env - T_i)`` through its own share of the shell,
* vertical conduction to the nodes above and below.

After every sub-step the profile is checked for buoyant instability, and any
pair where the lower node is warmer than the one above is mixed to their mean.
Mosaik swaps such pairs instead. Mixing conserves the same energy and is closer
to what happens, since an unstable pair overturns and blends rather than
trading places cleanly. It also converges monotonically rather than
oscillating.

Nothing here imports TESPy, so the physics is testable in closed form. The
network-facing wiring lives in :mod:`discoolpy.cold_storage`.

Sign convention
---------------
Consistent with the rest of the storage code: a **positive power means
discharge**. ``charge_power_W`` and ``discharge_power_W`` are separate
non-negative quantities, and the heat handed to the chilled-water loop is
``charge - discharge``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

__all__ = [
    "StratifiedTank",
    "TankStepResult",
    "CP_WATER_J_kgK",
    "RHO_WATER_kg_m3",
    "WATER_CONDUCTIVITY_W_mK",
]

#: Chilled water at ~10 degC. Fixed rather than fluid-property-resolved: the
#: variation over a 4-15 degC working range is far below the uncertainty in the
#: effective vertical conductivity.
CP_WATER_J_kgK = 4180.0
RHO_WATER_kg_m3 = 999.0

#: Effective vertical conductivity. Larger than still water (~0.6 W/m/K) because
#: it stands in for wall conduction and small-scale mixing as well as molecular
#: conduction. This is mosaik-heatpump's value and a common choice in the
#: stratified-tank literature.
WATER_CONDUCTIVITY_W_mK = 0.897


@dataclass
class TankStepResult:
    """What one :meth:`StratifiedTank.step` did."""

    mode: str                        # "charge" | "discharge" | "idle"
    mass_flow_kg_s: float
    duration_h: float
    #: Flow-weighted mean temperature of the water leaving the tank's outlet
    #: port over the step, in degC. This is the number a scalar model cannot
    #: give you: while discharging it is the temperature the district actually
    #: receives.
    outlet_temperature_degC: float
    inlet_temperature_degC: float
    charge_power_W: float
    discharge_power_W: float
    #: Heat handed to the chilled-water loop, ``charge - discharge`` in W.
    loop_heat_W: float
    ambient_heat_gain_W: float
    energy_before_kWh: float
    energy_after_kWh: float
    soc_before: float
    soc_after: float
    #: Height of the thermocline in metres, by the 20/80 band definition.
    thermocline_thickness_m: float
    #: Layer temperatures after the step, bottom first.
    profile_degC: List[float] = field(default_factory=list)
    sub_steps: int = 0

    def to_record(self, prefix: str = "tank") -> Dict[str, object]:
        """Flat record for CSV or pandas export."""
        record: Dict[str, object] = {
            f"{prefix}_mode": self.mode,
            f"{prefix}_mass_flow_kg_s": self.mass_flow_kg_s,
            f"{prefix}_outlet_temperature_degC": self.outlet_temperature_degC,
            f"{prefix}_inlet_temperature_degC": self.inlet_temperature_degC,
            f"{prefix}_charge_power_W": self.charge_power_W,
            f"{prefix}_discharge_power_W": self.discharge_power_W,
            f"{prefix}_loop_heat_W": self.loop_heat_W,
            f"{prefix}_ambient_heat_gain_W": self.ambient_heat_gain_W,
            f"{prefix}_energy_after_kWh": self.energy_after_kWh,
            f"{prefix}_soc_after": self.soc_after,
            f"{prefix}_thermocline_m": self.thermocline_thickness_m,
        }
        for i, temperature in enumerate(self.profile_degC):
            record[f"{prefix}_T{i}_degC"] = temperature
        return record


@dataclass
class StratifiedTank:
    """A vertical cylindrical chilled-water store resolved into layers.

    Parameters
    ----------
    volume_m3:
        Tank volume. Give this with ``height_m`` **or** with
        ``height_to_diameter``; the remaining dimension is derived.
    height_m, diameter_m:
        Explicit geometry. Any two of volume/height/diameter determine the third.
    height_to_diameter:
        Used when only a volume is given. Tall, slender tanks stratify better,
        which is why real chilled-water TES vessels are shaped the way they are.
    layers:
        Number of equal-volume nodes. More layers resolve a sharper thermocline
        and cost proportionally more sub-steps. Twelve is a reasonable default;
        below about six the thermocline is wider than the discretisation can
        represent and the model will understate the usable capacity.
    charged_temperature_degC:
        Design temperature of the cold water the plant supplies. The bottom of
        the tank approaches this when fully charged.
    discharged_temperature_degC:
        Design temperature of the warm return. The energy reference: stored
        cooling is measured against this, so a tank sitting entirely at this
        temperature has a state of charge of zero.
    initial_soc:
        Sets the starting profile. ``1.0`` fills the tank at the charged
        temperature, ``0.0`` at the discharged temperature, and values between
        place a sharp thermocline at the corresponding height, the state a
        real tank is in partway through a cycle.
    wall_htc_W_m2K:
        Shell heat transfer coefficient to ambient, per unit area. Defaults to
        mosaik-heatpump's 0.28 W/(m^2 K), a well-insulated tank.
    vertical_conductivity_W_mK:
        Effective conductivity for node-to-node conduction.
    max_courant:
        Sub-step control. The advective Courant number is kept below this, so a
        node can never be over-filled within one sub-step. Lower is more
        accurate and slower.
    max_sub_steps:
        Hard cap on sub-steps per call, so a pathological flow cannot hang a run.
    """

    volume_m3: Optional[float] = None
    height_m: Optional[float] = None
    diameter_m: Optional[float] = None
    height_to_diameter: float = 2.5
    layers: int = 12
    charged_temperature_degC: float = 5.0
    discharged_temperature_degC: float = 13.0
    initial_soc: float = 0.5
    wall_htc_W_m2K: float = 0.28
    vertical_conductivity_W_mK: float = WATER_CONDUCTIVITY_W_mK
    cp_J_kgK: float = CP_WATER_J_kgK
    rho_kg_m3: float = RHO_WATER_kg_m3
    max_courant: float = 0.5
    max_sub_steps: int = 4000

    #: Layer temperatures, bottom first. Populated in ``__post_init__``.
    temperatures_degC: List[float] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if int(self.layers) < 2:
            raise ValueError("A stratified tank needs at least 2 layers.")
        self.layers = int(self.layers)
        if self.discharged_temperature_degC <= self.charged_temperature_degC:
            raise ValueError(
                "discharged_temperature_degC must be above charged_temperature_degC: "
                "a cold store holds cooling relative to its warm return."
            )
        self._resolve_geometry()
        if not 0.0 <= self.initial_soc <= 1.0:
            raise ValueError("initial_soc must be between 0 and 1.")
        self.reset(self.initial_soc)

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _resolve_geometry(self) -> None:
        v, h, d = self.volume_m3, self.height_m, self.diameter_m
        if h is not None and d is not None:
            v = math.pi * float(d) ** 2 / 4.0 * float(h)
        elif v is not None and h is not None:
            d = math.sqrt(4.0 * float(v) / (math.pi * float(h)))
        elif v is not None and d is not None:
            h = 4.0 * float(v) / (math.pi * float(d) ** 2)
        elif v is not None:
            ratio = max(float(self.height_to_diameter), 1e-6)
            d = (4.0 * float(v) / (math.pi * ratio)) ** (1.0 / 3.0)
            h = ratio * d
        else:
            raise ValueError(
                "Tank geometry is under-specified: give volume_m3, or height_m and "
                "diameter_m, or a volume with height_to_diameter."
            )
        if min(float(v), float(h), float(d)) <= 0:
            raise ValueError("Tank volume, height and diameter must all be positive.")
        self.volume_m3, self.height_m, self.diameter_m = float(v), float(h), float(d)

    @property
    def layer_height_m(self) -> float:
        return self.height_m / self.layers

    @property
    def cross_section_m2(self) -> float:
        return math.pi * self.diameter_m**2 / 4.0

    @property
    def layer_mass_kg(self) -> float:
        return self.rho_kg_m3 * self.volume_m3 / self.layers

    @property
    def total_mass_kg(self) -> float:
        return self.rho_kg_m3 * self.volume_m3

    def _layer_shell_area_m2(self, index: int) -> float:
        """Outer area of one layer, including the end cap for the top and bottom."""
        area = math.pi * self.diameter_m * self.layer_height_m
        if index in (0, self.layers - 1):
            area += self.cross_section_m2
        return area

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def reset(self, soc: Optional[float] = None) -> None:
        """Set the profile to a given state of charge.

        A partially charged tank is given a *sharp* thermocline at the
        corresponding height rather than a uniform intermediate temperature.
        That is the honest starting point: a tank at 50 % has half its height
        cold and half warm, not all of it lukewarm, and the two states behave
        completely differently on the next discharge.
        """
        target = self.initial_soc if soc is None else float(soc)
        target = min(max(target, 0.0), 1.0)
        cold, warm = self.charged_temperature_degC, self.discharged_temperature_degC

        filled = target * self.layers
        profile: List[float] = []
        for i in range(self.layers):
            # Layer i spans [i, i+1) in units of layers, counting from the bottom.
            overlap = min(max(filled - i, 0.0), 1.0)
            profile.append(overlap * cold + (1.0 - overlap) * warm)
        self.temperatures_degC = profile

    @property
    def capacity_kWh(self) -> float:
        """Cooling the tank holds when fully charged, against the warm reference."""
        span = self.discharged_temperature_degC - self.charged_temperature_degC
        return self.total_mass_kg * self.cp_J_kgK * span / 3.6e6

    @property
    def energy_kWh(self) -> float:
        """Cooling currently stored, against the warm reference.

        Measured per layer and floored at zero, so a layer that has somehow
        drifted above the reference does not lend the tank negative capacity.
        """
        reference = self.discharged_temperature_degC
        total = sum(max(0.0, reference - t) for t in self.temperatures_degC)
        return self.layer_mass_kg * self.cp_J_kgK * total / 3.6e6

    @property
    def soc(self) -> float:
        capacity = self.capacity_kWh
        return self.energy_kWh / capacity if capacity > 0 else 0.0

    @property
    def cold_port_temperature_degC(self) -> float:
        """Temperature at the bottom port: what a discharge would deliver."""
        return self.temperatures_degC[0]

    @property
    def warm_port_temperature_degC(self) -> float:
        """Temperature at the top port: what a charge returns to the plant."""
        return self.temperatures_degC[-1]

    def thermocline_thickness_m(self, low: float = 0.2, high: float = 0.8) -> float:
        """Thickness of the transition zone, by the 20/80 band convention.

        The height over which the temperature passes between ``low`` and
        ``high`` of the way from charged to discharged. A freshly filled tank
        gives roughly one layer height; the figure grows as the tank cycles,
        and when it approaches the tank height the store has effectively lost
        its usable capacity even though its mean temperature may look healthy.
        """
        cold, warm = self.charged_temperature_degC, self.discharged_temperature_degC
        span = warm - cold
        if span <= 0:
            return 0.0
        # Fraction of the way from cold to warm, per layer, bottom first. The
        # stability pass guarantees this is non-decreasing, which is what lets
        # the crossings below be found by a single scan.
        f = [min(max((t - cold) / span, 0.0), 1.0) for t in self.temperatures_degC]
        heights = [(i + 0.5) * self.layer_height_m for i in range(self.layers)]

        def height_at(level: float) -> float:
            """Height where the profile passes ``level``, clamped to the tank.

            A transition that runs past either end of the tank is clipped there
            rather than being reported as spanning the whole vessel: a nearly
            full tank has a *thin* thermocline sitting near the top, not one as
            tall as the tank.
            """
            if f[0] >= level:
                return 0.0
            if f[-1] <= level:
                return self.height_m
            for i in range(len(f) - 1):
                a, b = f[i], f[i + 1]
                if a <= level <= b and b > a:
                    weight = (level - a) / (b - a)
                    return heights[i] + weight * (heights[i + 1] - heights[i])
            return self.height_m

        return max(0.0, height_at(high) - height_at(low))

    # ------------------------------------------------------------------
    # Stepping
    # ------------------------------------------------------------------

    def mass_flow_for_power(
        self,
        power_W: float,
        inlet_temperature_degC: float,
        mode: str,
    ) -> float:
        """Flow needed to move ``power_W`` right now, given the current profile.

        The available temperature difference is the one the tank can actually
        offer at this instant: while charging, between the warm top and the cold
        water arriving; while discharging, between the warm return and the cold
        bottom. As the tank approaches either end of its cycle that difference
        collapses, so the flow needed for a given power runs away, which is
        precisely the limit a scalar model cannot see, and why this returns a
        flow rather than accepting a power directly.
        """
        power = abs(float(power_W))
        if power <= 0:
            return 0.0
        if mode == "charge":
            delta = self.warm_port_temperature_degC - float(inlet_temperature_degC)
        elif mode == "discharge":
            delta = float(inlet_temperature_degC) - self.cold_port_temperature_degC
        else:
            return 0.0
        if delta <= 1e-6:
            return math.inf          # the caller clips this to its flow rating
        return power / (self.cp_J_kgK * delta)

    def step(
        self,
        duration_h: float,
        mass_flow_kg_s: float = 0.0,
        inlet_temperature_degC: Optional[float] = None,
        mode: str = "idle",
        ambient_temperature_degC: Optional[float] = None,
    ) -> TankStepResult:
        """Advance the tank by one interval.

        ``mode`` is ``"charge"`` (cold water in at the bottom, warm out of the
        top), ``"discharge"`` (warm return in at the top, cold out of the
        bottom), or ``"idle"`` (losses and conduction only).
        """
        dt_h = float(duration_h)
        if dt_h <= 0:
            raise ValueError("Step duration must be positive.")
        dt_s = dt_h * 3600.0

        mode = str(mode).lower()
        if mode not in {"charge", "discharge", "idle"}:
            raise ValueError("mode must be 'charge', 'discharge' or 'idle'.")
        flow = max(float(mass_flow_kg_s), 0.0)
        if flow <= 0 or inlet_temperature_degC is None:
            mode, flow = "idle", 0.0

        energy_before = self.energy_kWh
        soc_before = self.soc
        t_env = ambient_temperature_degC
        t_in = None if inlet_temperature_degC is None else float(inlet_temperature_degC)

        n = self.layers
        m_layer = self.layer_mass_kg
        cp = self.cp_J_kgK
        c_layer = m_layer * cp                                   # J/K
        cond = self.vertical_conductivity_W_mK * self.cross_section_m2 / self.layer_height_m
        ua = [self.wall_htc_W_m2K * self._layer_shell_area_m2(i) for i in range(n)]

        # Sub-step so the explicit update stays stable: no node may exchange more
        # than `max_courant` of its own capacity in one go, through flow or
        # through conduction and shell loss.
        rate = flow * cp + 2.0 * cond + max(ua)
        limit = self.max_courant * c_layer / max(rate, 1e-12)
        sub_steps = max(1, min(int(math.ceil(dt_s / limit)), int(self.max_sub_steps)))
        h = dt_s / sub_steps

        temps = list(self.temperatures_degC)
        outlet_accum = 0.0
        ambient_accum = 0.0

        for _ in range(sub_steps):
            # The outlet is read before the update, so the water leaving over
            # this sub-step is the water that was in the port node.
            if mode == "charge":
                outlet = temps[-1]
            elif mode == "discharge":
                outlet = temps[0]
            else:
                outlet = float("nan")
            outlet_accum += 0.0 if outlet != outlet else outlet

            new = list(temps)
            for i in range(n):
                # Advection from the upstream node. Charging pushes the stack
                # upward from the bottom inlet; discharging pushes it downward
                # from the top inlet.
                if mode == "charge":
                    upstream = t_in if i == 0 else temps[i - 1]
                elif mode == "discharge":
                    upstream = t_in if i == n - 1 else temps[i + 1]
                else:
                    upstream = temps[i]
                q = flow * cp * (upstream - temps[i]) if flow > 0 else 0.0

                if t_env is not None:
                    q += ua[i] * (float(t_env) - temps[i])
                if i > 0:
                    q += cond * (temps[i - 1] - temps[i])
                if i < n - 1:
                    q += cond * (temps[i + 1] - temps[i])

                new[i] = temps[i] + q * h / c_layer

            if t_env is not None:
                ambient_accum += sum(
                    ua[i] * (float(t_env) - temps[i]) for i in range(n)
                )

            temps = _mix_unstable(new)

        self.temperatures_degC = temps

        outlet_mean = outlet_accum / sub_steps if mode != "idle" else float("nan")
        ambient_W = ambient_accum / sub_steps if t_env is not None else 0.0

        charge_W = discharge_W = 0.0
        if mode == "charge":
            # The loop hands over cold water and gets warm water back, so it
            # gains exactly the cooling the tank absorbed.
            charge_W = max(0.0, flow * cp * (outlet_mean - t_in))
        elif mode == "discharge":
            discharge_W = max(0.0, flow * cp * (t_in - outlet_mean))

        return TankStepResult(
            mode=mode,
            mass_flow_kg_s=flow,
            duration_h=dt_h,
            outlet_temperature_degC=outlet_mean,
            inlet_temperature_degC=float("nan") if t_in is None else t_in,
            charge_power_W=charge_W,
            discharge_power_W=discharge_W,
            loop_heat_W=charge_W - discharge_W,
            ambient_heat_gain_W=ambient_W,
            energy_before_kWh=energy_before,
            energy_after_kWh=self.energy_kWh,
            soc_before=soc_before,
            soc_after=self.soc,
            thermocline_thickness_m=self.thermocline_thickness_m(),
            profile_degC=list(self.temperatures_degC),
            sub_steps=sub_steps,
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def profile(self) -> List[Dict[str, float]]:
        """Layer-by-layer state, bottom first, for plotting or inspection."""
        cold, warm = self.charged_temperature_degC, self.discharged_temperature_degC
        span = max(warm - cold, 1e-9)
        return [
            {
                "layer": i,
                "height_m": (i + 0.5) * self.layer_height_m,
                "temperature_degC": t,
                "charged_fraction": min(max((warm - t) / span, 0.0), 1.0),
            }
            for i, t in enumerate(self.temperatures_degC)
        ]

    def describe(self) -> Dict[str, float]:
        """Static description of the tank, for logs and notebook output."""
        return {
            "volume_m3": self.volume_m3,
            "height_m": self.height_m,
            "diameter_m": self.diameter_m,
            "layers": self.layers,
            "layer_height_m": self.layer_height_m,
            "capacity_kWh": self.capacity_kWh,
            "charged_temperature_degC": self.charged_temperature_degC,
            "discharged_temperature_degC": self.discharged_temperature_degC,
            "shell_UA_W_K": sum(
                self.wall_htc_W_m2K * self._layer_shell_area_m2(i) for i in range(self.layers)
            ),
        }


def _mix_unstable(temperatures: Sequence[float]) -> List[float]:
    """Blend any buoyantly unstable adjacent pair until the profile is stable.

    Cold water is denser, so a stable chilled-water tank has temperature
    increasing from bottom to top. Wherever a lower node ends up warmer than the
    one above it, the pair overturns; mixing them to their mean conserves energy
    exactly (the nodes have equal mass) and, unlike swapping, cannot oscillate.
    """
    temps = list(temperatures)
    changed = True
    while changed:
        changed = False
        for i in range(len(temps) - 1):
            if temps[i] > temps[i + 1] + 1e-12:
                mean = 0.5 * (temps[i] + temps[i + 1])
                temps[i] = temps[i + 1] = mean
                changed = True
    return temps
