"""Heat-gain physics shared by pipes, buildings and cold storage.

A district-cooling network is colder than everything around it, so ambient heat
transfer is always a gain the plant has to remove again. Sign convention here:

    Q > 0  ==>  heat enters the chilled medium (a parasitic cooling load)

That matches TESPy's ``SimpleHeatExchanger.Q`` and the convention
:class:`discoolpy.building.Building` already uses for building demand.

Nothing in this module imports TESPy. It works from geometry and material data
only, which keeps it unit-testable and lets the storage model and the reporting
code reuse the same conductances the solver sees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

__all__ = [
    "GROUND_CONDUCTIVITY_W_mK",
    "INSULATION_CONDUCTIVITY_W_mK",
    "TESPY_NATIVE_GROUND_MEDIA",
    "TESPY_MIN_INSULATION_THICKNESS_M",
    "lmtd_to_ambient",
    "buried_pipe_UA_per_m",
    "surface_pipe_UA_per_m",
    "twin_pipe_correction",
    "external_film_coefficient",
    "tank_surface_area_m2",
    "tank_UA_W_K",
    "PipeThermal",
    "EnvelopeThermal",
    "StorageThermal",
    "soil_temperature_degC",
]


# Conductivity of the medium around a buried pipe, W/(m*K). The keys match the
# names TESPy's Pipe uses for environment_media, so a scenario can switch
# between the explicit and the native route without renaming anything.
GROUND_CONDUCTIVITY_W_mK: Dict[str, float] = {
    "gravel": 1.1,
    "stones": 1.95,
    "dry soil": 0.5,
    "moist soil": 2.2,
    "wet sand": 2.75,
    "sand": 1.5,
    "clay": 1.4,
    "rock": 3.0,
}

# The subset of the table above that TESPy's own native buried-pipe group
# accepts for ``environment_media``. Its ground_conductivity lookup is a bare
# dict, so anything outside these four raises a KeyError from inside the
# residual evaluation, where it surfaces as a non-convergence message naming
# neither the pipe nor the material. DisCoolPy checks a scenario against this
# set while it is still a scenario. See :mod:`discoolpy.reporting`, which
# cross-checks the two formulations against each other.
TESPY_NATIVE_GROUND_MEDIA = ("gravel", "stones", "dry soil", "moist soil")

#: TESPy rejects an insulation thickness below this, so a scenario asking a
#: native group to model a bare pipe has to be told rather than left to fail.
TESPY_MIN_INSULATION_THICKNESS_M = 0.001

# Conductivity of common pipe insulation, W/(m*K).
INSULATION_CONDUCTIVITY_W_mK: Dict[str, float] = {
    "pur": 0.027,
    "polyurethane": 0.027,
    "peir": 0.024,
    "pir": 0.024,
    "eps": 0.038,
    "xps": 0.034,
    "mineral wool": 0.040,
    "glass wool": 0.040,
    "elastomeric": 0.036,
    "none": 1e6,  # effectively bare pipe
}


def _resolve_conductivity(value: Any, table: Mapping[str, float], what: str) -> float:
    """Accept either a numeric conductivity or a material name from ``table``."""
    if isinstance(value, (int, float)):
        conductivity = float(value)
        if conductivity <= 0:
            raise ValueError(f"{what} conductivity must be positive, got {conductivity}.")
        return conductivity
    key = str(value).strip().lower()
    if key not in table:
        raise ValueError(
            f"Unknown {what} material {value!r}. Use a numeric W/(m*K) value or one of "
            f"{sorted(table)}."
        )
    return table[key]


def lmtd_to_ambient(T_in: float, T_out: float, T_ambient: float) -> float:
    """Log-mean temperature difference between a stream and a constant ambient.

    Returns ``T_ambient - T_mean``, positive when ambient is the warmer of the
    two, so ``Q = UA * lmtd_to_ambient(...)`` comes out as a positive gain.
    When the two terminal differences straddle zero or agree to within 1e-9 K
    the log form blows up, so the arithmetic mean is used instead. That happens
    at start-up and at very small duties.
    """
    d_in = float(T_ambient) - float(T_in)
    d_out = float(T_ambient) - float(T_out)
    if d_in * d_out <= 0.0 or abs(d_in - d_out) < 1e-9:
        return 0.5 * (d_in + d_out)
    return (d_in - d_out) / math.log(d_in / d_out)


def soil_temperature_degC(
    day_of_year: float,
    mean_annual_degC: float,
    amplitude_K: float,
    depth_m: float = 1.0,
    diffusivity_m2_s: float = 6.0e-7,
    phase_shift_days: float = 30.0,
) -> float:
    """Undisturbed ground temperature at ``depth_m`` (Kusuda-type harmonic).

    Buried pipes see the soil, not the air. Soil at 1-2 m lags the air by weeks
    and swings by only a fraction of the surface amplitude, so pipe gain stays
    nearly flat over a day while building and tank gains swing hard. The helper
    exists mostly to stop scenarios driving buried pipes with dry-bulb air,
    which is an easy mistake to make and overstates the gain badly.
    """
    period_s = 365.25 * 24 * 3600.0
    # Damping depth d = sqrt(alpha * P / pi); ~2.5 m for typical soil over a
    # one-year period, so 1-2 m of cover already halves the annual swing.
    damping = math.sqrt(float(diffusivity_m2_s) * period_s / math.pi)
    decay = math.exp(-float(depth_m) / damping)
    lag_days = (float(depth_m) / damping) * 365.25 / (2 * math.pi)
    angle = 2 * math.pi * (float(day_of_year) - float(phase_shift_days) - lag_days) / 365.25
    return float(mean_annual_degC) - float(amplitude_K) * decay * math.cos(angle)


# Pipe conductances.

def external_film_coefficient(
    wind_velocity_m_s: float = 1.0,
    include_radiation: bool = True,
    emissivity: float = 0.9,
    surface_temperature_degC: float = 25.0,
    ambient_temperature_degC: float = 35.0,
) -> float:
    """Combined convective + radiative film coefficient for an exposed surface [W/(m^2*K)].

    Convection uses the usual engineering fit ``5.7 + 3.8 * v``; radiation is
    linearised about the mean of the surface and ambient absolute temperatures.
    Both are rough, but on a chilled line the external film is rarely the
    controlling resistance, so the error it puts into UA is smaller than the
    uncertainty in the insulation value.
    """
    h = 5.7 + 3.8 * max(float(wind_velocity_m_s), 0.0)
    if include_radiation:
        t_s = float(surface_temperature_degC) + 273.15
        t_a = float(ambient_temperature_degC) + 273.15
        t_m = 0.5 * (t_s + t_a)
        h += 4.0 * float(emissivity) * 5.670374419e-8 * t_m**3
    return h


def twin_pipe_correction(
    depth_m: float,
    centre_spacing_m: float,
    ground_conductivity_W_mK: float,
    single_resistance_m_K_W: float,
) -> float:
    """Adjust a single-pipe soil resistance for a neighbouring pipe in the trench.

    Supply and return normally share a trench, and the warmer return line
    raises the effective ground temperature the supply line sees. This is the
    first-order symmetric-trench correction: add the mutual resistance term
    ``ln(sqrt(1 + (2z/s)^2)) / (2*pi*k)`` to the self resistance, which brings
    the net gain below what two independent pipes would give. Returns the
    corrected per-metre resistance in m*K/W.
    """
    if centre_spacing_m <= 0:
        return single_resistance_m_K_W
    mutual = math.log(math.sqrt(1.0 + (2.0 * float(depth_m) / float(centre_spacing_m)) ** 2)) / (
        2.0 * math.pi * float(ground_conductivity_W_mK)
    )
    return single_resistance_m_K_W + mutual


def buried_pipe_UA_per_m(
    inner_diameter_m: float,
    insulation_thickness_m: float,
    insulation_conductivity: Any = "pur",
    burial_depth_m: float = 1.0,
    ground: Any = "moist soil",
    pipe_wall_thickness_m: float = 0.0,
    pipe_wall_conductivity_W_mK: float = 45.0,
    internal_film_W_m2K: float = 2000.0,
    twin_spacing_m: Optional[float] = None,
) -> float:
    """Per-metre heat-transfer coefficient of a buried, insulated pipe [W/(m*K)].

    Four resistances in series: internal film, steel wall, insulation, soil.
    The soil term is the semi-infinite line-source solution
    ``arccosh(2z/D)/(2*pi*k)``. It needs ``2z > D``, which any real trench
    satisfies.
    """
    d_i = float(inner_diameter_m)
    if d_i <= 0:
        raise ValueError("inner_diameter_m must be positive.")
    t_wall = max(float(pipe_wall_thickness_m), 0.0)
    t_ins = max(float(insulation_thickness_m), 0.0)
    k_ins = _resolve_conductivity(insulation_conductivity, INSULATION_CONDUCTIVITY_W_mK, "insulation")
    k_soil = _resolve_conductivity(ground, GROUND_CONDUCTIVITY_W_mK, "ground")

    d_wall_out = d_i + 2 * t_wall
    d_ins_out = d_wall_out + 2 * t_ins
    depth = max(float(burial_depth_m), 0.5 * d_ins_out * 1.01)

    r_internal = 1.0 / (math.pi * d_i * max(float(internal_film_W_m2K), 1e-6))
    r_wall = (
        math.log(d_wall_out / d_i) / (2 * math.pi * float(pipe_wall_conductivity_W_mK))
        if t_wall > 0
        else 0.0
    )
    r_ins = math.log(d_ins_out / d_wall_out) / (2 * math.pi * k_ins) if t_ins > 0 else 0.0
    r_soil = math.acosh(2.0 * depth / d_ins_out) / (2 * math.pi * k_soil)
    if twin_spacing_m:
        r_soil = twin_pipe_correction(depth, twin_spacing_m, k_soil, r_soil)

    return 1.0 / (r_internal + r_wall + r_ins + r_soil)


def surface_pipe_UA_per_m(
    inner_diameter_m: float,
    insulation_thickness_m: float,
    insulation_conductivity: Any = "pur",
    pipe_wall_thickness_m: float = 0.0,
    pipe_wall_conductivity_W_mK: float = 45.0,
    internal_film_W_m2K: float = 2000.0,
    wind_velocity_m_s: float = 1.0,
    emissivity: float = 0.9,
    ambient_temperature_degC: float = 35.0,
    surface_temperature_degC: Optional[float] = None,
) -> float:
    """Per-metre heat-transfer coefficient of an exposed / in-tunnel pipe [W/(m*K)]."""
    d_i = float(inner_diameter_m)
    if d_i <= 0:
        raise ValueError("inner_diameter_m must be positive.")
    t_wall = max(float(pipe_wall_thickness_m), 0.0)
    t_ins = max(float(insulation_thickness_m), 0.0)
    k_ins = _resolve_conductivity(insulation_conductivity, INSULATION_CONDUCTIVITY_W_mK, "insulation")

    d_wall_out = d_i + 2 * t_wall
    d_ins_out = d_wall_out + 2 * t_ins
    t_surface = (
        float(ambient_temperature_degC) - 2.0
        if surface_temperature_degC is None
        else float(surface_temperature_degC)
    )
    h_ext = external_film_coefficient(
        wind_velocity_m_s=wind_velocity_m_s,
        emissivity=emissivity,
        surface_temperature_degC=t_surface,
        ambient_temperature_degC=ambient_temperature_degC,
    )

    r_internal = 1.0 / (math.pi * d_i * max(float(internal_film_W_m2K), 1e-6))
    r_wall = (
        math.log(d_wall_out / d_i) / (2 * math.pi * float(pipe_wall_conductivity_W_mK))
        if t_wall > 0
        else 0.0
    )
    r_ins = math.log(d_ins_out / d_wall_out) / (2 * math.pi * k_ins) if t_ins > 0 else 0.0
    r_ext = 1.0 / (math.pi * d_ins_out * h_ext)
    return 1.0 / (r_internal + r_wall + r_ins + r_ext)


# Tank conductance.

def tank_surface_area_m2(volume_m3: float, height_to_diameter: float = 1.0) -> float:
    """External area of a vertical cylindrical tank of the given volume."""
    v = float(volume_m3)
    if v <= 0:
        raise ValueError("volume_m3 must be positive.")
    ratio = max(float(height_to_diameter), 1e-6)
    diameter = (4.0 * v / (math.pi * ratio)) ** (1.0 / 3.0)
    height = ratio * diameter
    return math.pi * diameter * height + 2 * math.pi * diameter**2 / 4.0


def tank_UA_W_K(
    volume_m3: float,
    insulation_thickness_m: float,
    insulation_conductivity: Any = "pur",
    height_to_diameter: float = 1.0,
    internal_film_W_m2K: float = 300.0,
    external_film_W_m2K: float = 12.0,
    buried_fraction: float = 0.0,
    ground: Any = "moist soil",
) -> float:
    """Overall tank heat-gain conductance [W/K].

    Plane-wall series resistance rather than cylindrical: tank insulation is
    thin next to the diameter, so the curvature correction sits well inside the
    uncertainty on the insulation value. ``buried_fraction`` blends a soil-side
    film in for partially buried tanks.
    """
    area = tank_surface_area_m2(volume_m3, height_to_diameter)
    k_ins = _resolve_conductivity(insulation_conductivity, INSULATION_CONDUCTIVITY_W_mK, "insulation")
    t_ins = max(float(insulation_thickness_m), 0.0)

    f = min(max(float(buried_fraction), 0.0), 1.0)
    k_soil = _resolve_conductivity(ground, GROUND_CONDUCTIVITY_W_mK, "ground")
    # A 0.5 m equivalent soil thickness is the conventional shallow-burial value.
    h_soil = k_soil / 0.5
    h_out = (1.0 - f) * float(external_film_W_m2K) + f * h_soil

    r = 1.0 / max(float(internal_film_W_m2K), 1e-6) + t_ins / k_ins + 1.0 / max(h_out, 1e-6)
    return area / r


# Declarative specifications: what a scenario file resolves to.

@dataclass
class PipeThermal:
    """Resolved thermal description of one pipe.

    ``model`` selects how the heat gain reaches the solver:

    ``adiabatic``
        ``Q = 0``. No gain at all. Fine for a first pass or a short network.
    ``fixed``
        A constant ``Q`` in W supplied by the user.
    ``ua``
        ``UA`` [W/K] and an ambient temperature. DisCoolPy works ``UA`` out from
        the geometry (or takes it as given) and hands ``UA`` and ``Tamb`` to
        TESPy, which activates TESPy's own ``UA_group`` equation
        ``Q = UA * dT_log``. The duty is therefore solved simultaneously with
        the rest of the network, from the water temperature the solver actually
        found, not computed alongside it. Use this one unless you have a reason
        not to.
    ``tespy_buried`` / ``tespy_surface``
        Hand the conductance as well as the duty to TESPy's native buried and
        surface pipe groups.

    All three conducting models are solved by TESPy. What separates them is only
    where the conductance comes from, and none of them computes a heat flow
    outside the solver. The one closed-form path, :meth:`heat_gain_W`, exists
    for reporting and for sanity checks before a solve; nothing in the solve
    path calls it.

    The trade between ``ua`` and the two native models is a real one, and
    :mod:`discoolpy.reporting` will quantify it for a given pipe rather than
    leave it to be argued. On ordinary insulated district geometry the two soil
    formulations agree closely; they separate where a trench is shared, where
    the ground is not one of TESPy's four named media, where a pipe is
    uninsulated, or where an exposed pipe's radiation matters.
    """

    model: str = "adiabatic"
    Q_W: float = 0.0
    UA_W_K: Optional[float] = None
    UA_per_m_W_mK: Optional[float] = None
    length_m: Optional[float] = None
    ambient_temperature_degC: Optional[float] = None
    ambient_source: str = "ground"  # "ground" | "air" | "fixed"
    native_attrs: Dict[str, Any] = field(default_factory=dict)
    geometry: Dict[str, Any] = field(default_factory=dict)

    def resolved_UA(self) -> float:
        """Return the pipe conductance in W/K (0 when the model does not use one)."""
        if self.UA_W_K is not None:
            return float(self.UA_W_K)
        if self.UA_per_m_W_mK is not None and self.length_m is not None:
            return float(self.UA_per_m_W_mK) * float(self.length_m)
        return 0.0

    def heat_gain_W(self, T_in: float, T_out: float, T_ambient: Optional[float] = None) -> float:
        """Estimate the gain for reporting, independent of the solver."""
        if self.model == "adiabatic":
            return 0.0
        if self.model == "fixed":
            return float(self.Q_W)
        t_amb = self.ambient_temperature_degC if T_ambient is None else T_ambient
        if t_amb is None:
            return 0.0
        return self.resolved_UA() * lmtd_to_ambient(T_in, T_out, t_amb)


@dataclass
class EnvelopeThermal:
    """Weather-driven part of a building's cooling demand.

    The total building load handed to the network is

        Q = Q_internal(t) + UA_env * (T_air - T_indoor) + g_solar * I(t) + m_inf * cp * (T_air - T_indoor)

    clipped at zero, since a building on a cooling network cannot push heat
    back into the loop. The split matters because a single scalar ``Q`` leaves
    the ambient driving force nowhere to act, and then neither weather
    sensitivity nor pre-cooling can be represented.
    """

    UA_W_K: float = 0.0
    solar_aperture_m2: float = 0.0
    infiltration_kg_s: float = 0.0
    indoor_setpoint_degC: float = 24.0
    internal_gain_W: float = 0.0
    cp_air_J_kgK: float = 1005.0

    def gain_W(
        self,
        ambient_temperature_degC: float,
        solar_irradiance_W_m2: float = 0.0,
        indoor_temperature_degC: Optional[float] = None,
        internal_gain_W: Optional[float] = None,
    ) -> Dict[str, float]:
        """Return the envelope gain split into its components, all in W."""
        t_in = self.indoor_setpoint_degC if indoor_temperature_degC is None else float(indoor_temperature_degC)
        dt = float(ambient_temperature_degC) - t_in
        conduction = self.UA_W_K * dt
        infiltration = self.infiltration_kg_s * self.cp_air_J_kgK * dt
        solar = self.solar_aperture_m2 * float(solar_irradiance_W_m2)
        internal = self.internal_gain_W if internal_gain_W is None else float(internal_gain_W)
        return {
            "conduction_W": conduction,
            "infiltration_W": infiltration,
            "solar_W": solar,
            "internal_W": internal,
            "total_gain_W": conduction + infiltration + solar + internal,
        }


@dataclass
class StorageThermal:
    """Ambient heat gain into a cold / ice / PCM store.

    ``Q = UA * (T_ambient - T_store)``, in place of a flat daily standby-loss
    fraction. An ice tank sits near 0 degC, so on a 45 degC afternoon it gains
    about three times what it gains on a 15 degC night. The loss therefore
    peaks at the hour the stored cooling is worth most, and a fixed daily
    fraction cannot say that. In a hot climate it overstates what the tank
    delivers.

    ``storage_temperature_degC`` is the medium temperature when charged. Phase
    change holds it roughly constant for ice and PCM, so
    ``temperature_varies_with_soc`` defaults to False. Sensible chilled water is
    different: the mean tank temperature climbs as it discharges and the gain
    falls with it, so set the flag and give ``discharged_temperature_degC``.
    """

    UA_W_K: float = 0.0
    storage_temperature_degC: float = 0.0
    discharged_temperature_degC: Optional[float] = None
    temperature_varies_with_soc: bool = False

    def medium_temperature_degC(self, soc: float) -> float:
        """Mean temperature of the stored medium at the given state of charge."""
        if not self.temperature_varies_with_soc or self.discharged_temperature_degC is None:
            return float(self.storage_temperature_degC)
        f = min(max(float(soc), 0.0), 1.0)
        return float(self.discharged_temperature_degC) + f * (
            float(self.storage_temperature_degC) - float(self.discharged_temperature_degC)
        )

    def heat_gain_W(self, ambient_temperature_degC: Optional[float], soc: float) -> float:
        """Heat entering the store in W (positive = stored cooling is being eroded)."""
        if self.UA_W_K <= 0 or ambient_temperature_degC is None:
            return 0.0
        return self.UA_W_K * (float(ambient_temperature_degC) - self.medium_temperature_degC(soc))
