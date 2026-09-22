"""Cold, ice and PCM thermal storage for modular TESPy district cooling.

The tank gains heat from its surroundings
-----------------------------------------
A flat ``standby_loss_fraction_per_day`` treats the loss as a constant, and it
is not. An ice tank sits near 0 degC, so on a 45 degC afternoon it loses about
three times what it loses on a 15 degC night, and the loss peaks at the hour
the stored cooling is worth most. :class:`~discoolpy.thermal.StorageThermal`
gives you ``Q = UA * (T_ambient - T_store)`` instead. The fractional model is
still there under ``loss_model="fraction"`` for scenarios that want it.

How the tank couples to the loop
--------------------------------
The simple option, ``coupling="supervisory"``, rescales every building's heat
duty so the plant load matches the post-dispatch figure. The solver is happy,
but the building heat exchangers are no longer serving their real demand and
the reported chilled-water return temperature is not the real one.

Under ``coupling="hydraulic"`` the store becomes a ``SimpleHeatExchanger`` in
the plant section of the chilled-water loop, carrying

    Q_hx = charge_power - discharge_power        [W, added to the water]

The chiller's evaporator duty is released and solved from the loop, so

    Q_evap = Q_buildings + Q_pipes + P_pump + Q_storage

comes out of the network instead of being asserted. That also picks up the
penalty which makes ice storage an interesting question in the first place: to
charge in series the plant has to make water below the distribution setpoint,
which pulls the evaporating temperature down and costs COP. A supervisory
model shifts energy at unchanged efficiency, so it flatters storage.

Sign convention
---------------
``storage_power_W > 0`` is discharge: the store supplies cooling and reduces
the chiller evaporator duty. ``storage_power_W < 0`` is charge.
``chiller_load_offset_W = charge_power_W - discharge_power_W``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, Mapping, Optional, Sequence

from .stratified import StratifiedTank, TankStepResult
from .thermal import StorageThermal, tank_UA_W_K

_LOSS_MODELS = {"fraction", "ua", "both"}
_COUPLINGS = {"supervisory", "hydraulic"}


@dataclass(frozen=True)
class StorageDispatchResult:
    """Result of one cold-storage dispatch step.

    All thermal-energy values are reported from the storage perspective. A
    positive ``storage_power_W`` means discharge and a negative value means
    charge. ``chiller_load_offset_W`` is the offset that should be added to the
    base chiller load: positive during charge and negative during discharge.
    """

    timestamp: object
    mode: str
    requested_storage_power_W: float
    storage_power_W: float
    charge_power_W: float
    discharge_power_W: float
    chiller_load_offset_W: float
    energy_before_kWh: float
    energy_after_kWh: float
    soc_before: float
    soc_after: float
    standby_loss_kWh: float
    curtailed_request_W: float
    effective_chiller_load_W: Optional[float] = None
    ambient_temperature_degC: Optional[float] = None
    medium_temperature_degC: Optional[float] = None
    ambient_heat_gain_W: float = 0.0
    ambient_heat_gain_kWh: float = 0.0
    fractional_loss_kWh: float = 0.0
    # --- stratified tanks only ------------------------------------------
    #: Water actually pushed through the tank, kg/s.
    tank_mass_flow_kg_s: float = 0.0
    #: Flow-weighted temperature at the tank's outlet port over the step. While
    #: discharging this is what the district receives, and it degrades as the
    # thermocline reaches the port, the number a scalar store cannot give.
    tank_outlet_temperature_degC: Optional[float] = None
    tank_inlet_temperature_degC: Optional[float] = None
    #: Thickness of the transition zone, metres, by the 20/80 band definition.
    thermocline_thickness_m: Optional[float] = None
    #: Bottom and top port temperatures after the step. Unlike the outlet
    #: temperature these are defined even when the tank is idle, so a plot of
    #: them shows the ports drifting during standby.
    cold_port_temperature_degC: Optional[float] = None
    warm_port_temperature_degC: Optional[float] = None
    #: Layer temperatures after the step, bottom first.
    tank_profile_degC: Optional[tuple] = None

    def to_record(self, prefix: str = "storage") -> Dict[str, object]:
        """Return a flat dictionary for CSV or pandas exports."""
        record: Dict[str, object] = {
            f"{prefix}_mode": self.mode,
            f"{prefix}_requested_power_W": self.requested_storage_power_W,
            f"{prefix}_power_W": self.storage_power_W,
            f"{prefix}_charge_power_W": self.charge_power_W,
            f"{prefix}_discharge_power_W": self.discharge_power_W,
            f"{prefix}_chiller_load_offset_W": self.chiller_load_offset_W,
            f"{prefix}_energy_before_kWh": self.energy_before_kWh,
            f"{prefix}_energy_after_kWh": self.energy_after_kWh,
            f"{prefix}_soc_before": self.soc_before,
            f"{prefix}_soc_after": self.soc_after,
            f"{prefix}_standby_loss_kWh": self.standby_loss_kWh,
            f"{prefix}_ambient_heat_gain_W": self.ambient_heat_gain_W,
            f"{prefix}_ambient_heat_gain_kWh": self.ambient_heat_gain_kWh,
            f"{prefix}_fractional_loss_kWh": self.fractional_loss_kWh,
            f"{prefix}_medium_temperature_degC": self.medium_temperature_degC,
            f"{prefix}_curtailed_request_W": self.curtailed_request_W,
            f"{prefix}_effective_chiller_load_W": self.effective_chiller_load_W,
        }
        if self.tank_profile_degC is not None:
            record.update({
                f"{prefix}_tank_mass_flow_kg_s": self.tank_mass_flow_kg_s,
                f"{prefix}_tank_outlet_temperature_degC": self.tank_outlet_temperature_degC,
                f"{prefix}_tank_inlet_temperature_degC": self.tank_inlet_temperature_degC,
                f"{prefix}_thermocline_m": self.thermocline_thickness_m,
                f"{prefix}_cold_port_degC": self.cold_port_temperature_degC,
                f"{prefix}_warm_port_degC": self.warm_port_temperature_degC,
            })
            for i, temperature in enumerate(self.tank_profile_degC):
                record[f"{prefix}_tank_T{i}_degC"] = temperature
        return record


@dataclass
class ColdStorage:
    """Aggregate chilled-water, PCM, or ice storage model.

    Parameters
    ----------
    label:
        Human-readable storage label.
    capacity_kWh:
        Usable cooling-storage capacity.
    initial_soc:
        Initial state of charge between 0 and 1, where 1 means fully charged.
    max_charge_kW, max_discharge_kW:
        Charge and discharge power limits.
    charge_efficiency, discharge_efficiency:
        One-way storage efficiencies. During charging only
        ``charge_efficiency * charge_power * dt`` is stored. During discharge the
        store loses ``discharge_power * dt / discharge_efficiency``.
    standby_loss_fraction_per_day:
        Fraction of stored cooling lost per day. Used when ``loss_model`` is
        ``"fraction"`` or ``"both"``.
    thermal:
        :class:`~discoolpy.thermal.StorageThermal` describing the ambient heat
        gain. Used when ``loss_model`` is ``"ua"`` or ``"both"``.
    loss_model:
        ``"fraction"`` (pre-upgrade behaviour), ``"ua"`` (recommended) or
        ``"both"``.
    coupling:
        ``"hydraulic"`` places the store in the chilled-water loop as a real
        TESPy element; ``"supervisory"`` keeps the legacy load-rescaling
        approximation.
    hydraulic_pr:
        Water-side pressure ratio of the storage heat exchanger when coupled
        hydraulically.
    storage_type:
        Descriptive storage type such as ``ice``, ``chilled_water``, or ``pcm``.
    target_chiller_load_kW:
        Optional default load-leveling target.
    min_soc, max_soc:
        Operating SOC bounds.
    charge_allowed_above_degC, discharge_allowed_below_degC:
        Optional temperature feasibility guards.
    """

    label: str
    capacity_kWh: float
    initial_soc: float = 0.5
    max_charge_kW: float = 250.0
    max_discharge_kW: float = 250.0
    charge_efficiency: float = 0.92
    discharge_efficiency: float = 0.94
    standby_loss_fraction_per_day: float = 0.02
    thermal: Optional[StorageThermal] = None
    loss_model: str = "fraction"
    coupling: str = "supervisory"
    hydraulic_pr: Optional[float] = 0.999
    storage_type: str = "ice"
    target_chiller_load_kW: Optional[float] = None
    min_soc: float = 0.05
    max_soc: float = 0.98
    charge_allowed_above_degC: Optional[float] = None
    discharge_allowed_below_degC: Optional[float] = None
    #: Optional 1-D stratified tank. When present the store stops being a single
    #: number and becomes a resolved temperature profile: charge and discharge
    #: power are then *outputs* of the tank physics rather than inputs, and the
    #: temperature the district receives degrades as the thermocline reaches the
    #: outlet. See :mod:`discoolpy.stratified`.
    stratified: Optional[StratifiedTank] = None
    #: Largest flow the tank's pumps and nozzles can pass, kg/s. Defaults to
    #: whatever ``max_charge_kW`` implies across the design temperature span.
    max_tank_flow_kg_s: Optional[float] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.capacity_kWh <= 0:
            raise ValueError("Storage capacity must be positive.")
        if not 0 <= self.initial_soc <= 1:
            raise ValueError("initial_soc must be between 0 and 1.")
        if not 0 <= self.min_soc <= self.max_soc <= 1:
            raise ValueError("SOC bounds must satisfy 0 <= min_soc <= max_soc <= 1.")
        if self.max_charge_kW < 0 or self.max_discharge_kW < 0:
            raise ValueError("Power limits must be non-negative.")
        if self.charge_efficiency <= 0 or self.discharge_efficiency <= 0:
            raise ValueError("Storage efficiencies must be positive.")
        if not 0 <= self.standby_loss_fraction_per_day < 1:
            raise ValueError("Daily standby loss fraction must be in [0, 1).")
        self.loss_model = str(self.loss_model).lower()
        if self.loss_model not in _LOSS_MODELS:
            raise ValueError(f"loss_model must be one of {sorted(_LOSS_MODELS)}.")
        self.coupling = str(self.coupling).lower()
        if self.coupling not in _COUPLINGS:
            raise ValueError(f"coupling must be one of {sorted(_COUPLINGS)}.")
        if self.stratified is not None and self.loss_model in {"fraction", "both"}:
            raise ValueError(
                f"Storage {self.label!r} is stratified, so its standing loss is computed layer "
                "by layer from the tank's shell. loss_model must be 'ua', not "
                f"{self.loss_model!r}, which would count that loss twice."
            )
        # A stratified tank carries its own shell UA, so it satisfies the 'ua'
        # loss model on its own and needs no scalar StorageThermal.
        if self.loss_model in {"ua", "both"} and self.thermal is None and self.stratified is None:
            raise ValueError(
                f"Storage {self.label!r} uses loss_model={self.loss_model!r} but has no "
                "StorageThermal. Provide a UA_W_K (or use ColdStorage.thermal_from_geometry)."
            )
        if self.stratified is not None:
            # The tank's geometry and temperature span define the capacity, so a
            # capacity typed into the scenario would only be a second, silently
            # conflicting source of truth.
            self.capacity_kWh = self.stratified.capacity_kWh
            self.stratified.reset(self.initial_soc)
            if self.max_tank_flow_kg_s is None:
                span = (
                    self.stratified.discharged_temperature_degC
                    - self.stratified.charged_temperature_degC
                )
                rating_kW = max(self.max_charge_kW, self.max_discharge_kW)
                self.max_tank_flow_kg_s = rating_kW * 1000.0 / (
                    self.stratified.cp_J_kgK * span
                )
        self._energy_kWh = min(
            self.max_energy_kWh,
            max(self.min_energy_kWh, float(self.initial_soc) * self.capacity_kWh),
        )
        self.history = []
        self._hx = None
        self._connections: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def energy_kWh(self) -> float:
        """Stored cooling in kWh.

        Backed by the layer profile when the store is stratified, so the two
        representations can never drift apart, and by a plain scalar otherwise.
        """
        if self.stratified is not None:
            return self.stratified.energy_kWh
        return self._energy_kWh

    @energy_kWh.setter
    def energy_kWh(self, value: float) -> None:
        self._energy_kWh = float(value)

    @property
    def is_stratified(self) -> bool:
        return self.stratified is not None

    @property
    def min_energy_kWh(self) -> float:
        """Minimum allowed stored cooling."""
        return self.capacity_kWh * self.min_soc

    @property
    def max_energy_kWh(self) -> float:
        """Maximum allowed stored cooling."""
        return self.capacity_kWh * self.max_soc

    @property
    def soc(self) -> float:
        """Current state of charge as a fraction of nominal capacity."""
        return self.energy_kWh / self.capacity_kWh

    def reset(self, soc: Optional[float] = None) -> None:
        """Reset the storage state and clear dispatch history."""
        new_soc = self.initial_soc if soc is None else float(soc)
        if not 0 <= new_soc <= 1:
            raise ValueError("Reset SOC must be between 0 and 1.")
        if self.stratified is not None:
            self.stratified.reset(new_soc)
        self._energy_kWh = min(
            self.max_energy_kWh, max(self.min_energy_kWh, new_soc * self.capacity_kWh)
        )
        self.history = []

    @staticmethod
    def thermal_from_geometry(
        volume_m3: float,
        insulation_thickness_m: float,
        insulation_conductivity: Any = "pur",
        storage_temperature_degC: float = 0.0,
        **kwargs: Any,
    ) -> StorageThermal:
        """Build a :class:`StorageThermal` from tank geometry."""
        ua = tank_UA_W_K(
            volume_m3=volume_m3,
            insulation_thickness_m=insulation_thickness_m,
            insulation_conductivity=insulation_conductivity,
            **{k: v for k, v in kwargs.items() if k in {
                "height_to_diameter", "internal_film_W_m2K", "external_film_W_m2K",
                "buried_fraction", "ground",
            }},
        )
        return StorageThermal(
            UA_W_K=ua,
            storage_temperature_degC=storage_temperature_degC,
            discharged_temperature_degC=kwargs.get("discharged_temperature_degC"),
            temperature_varies_with_soc=bool(kwargs.get("temperature_varies_with_soc", False)),
        )

    # ------------------------------------------------------------------
    # Hydraulic coupling
    # ------------------------------------------------------------------

    @property
    def heat_exchanger(self):
        """TESPy element representing the store in the chilled-water loop."""
        if self._hx is None:
            raise RuntimeError(
                "This ColdStorage has no hydraulic element. Use coupling='hydraulic' and call "
                "create_hydraulic_element() before connecting it."
            )
        return self._hx

    def create_hydraulic_element(self):
        """Create (once) the ``SimpleHeatExchanger`` that represents the store."""
        if self._hx is None:
            from tespy.components import SimpleHeatExchanger

            self._hx = SimpleHeatExchanger(f"{self.label} storage hx")
        return self._hx

    def connect_between(
        self,
        source,
        source_port: str,
        sink,
        sink_port: str,
        inlet_label: Optional[str] = None,
        outlet_label: Optional[str] = None,
    ) -> Sequence[Any]:
        """Insert the store in series between two plant-side components."""
        from tespy.connections import Connection

        hx = self.create_hydraulic_element()
        safe = self.label.replace(" ", "_").lower()
        self._connections["in"] = Connection(
            source, source_port, hx, "in1", label=inlet_label or f"{safe}_in"
        )
        self._connections["out"] = Connection(
            hx, "out1", sink, sink_port, label=outlet_label or f"{safe}_out"
        )
        return self._connections["in"], self._connections["out"]

    @property
    def connections(self) -> Sequence[Any]:
        if not self._connections:
            raise RuntimeError("Call connect_between before requesting storage connections.")
        return tuple(self._connections.values())

    def set_design(self, idle_Q_W: float = 0.0) -> None:
        """Apply design attributes to the hydraulic element.

        The design point is the *idle* store (``Q = 0`` by default): sizing the
        network around a charging or discharging tank would bake a transient
        operating mode into every offdesign solve.
        """
        hx = self.heat_exchanger
        attrs: Dict[str, Any] = {"Q": float(idle_Q_W)}
        if self.hydraulic_pr is not None:
            attrs["pr"] = float(self.hydraulic_pr)
        hx.set_attr(**attrs)

    def apply_dispatch_to_network(self, result: "StorageDispatchResult") -> float:
        """Push a dispatch result onto the hydraulic element. Returns ``Q_hx`` in W."""
        q = float(result.chiller_load_offset_W)
        self.heat_exchanger.set_attr(Q=q)
        return q

    # ------------------------------------------------------------------
    # Losses
    # ------------------------------------------------------------------

    @staticmethod
    def _duration_hours(resolution: object) -> float:
        if isinstance(resolution, timedelta):
            hours = resolution.total_seconds() / 3600.0
        else:
            # Numeric values are interpreted as hours for consistency with
            # SnapshotSchedule.parse_resolution.
            hours = float(resolution)
        if hours <= 0:
            raise ValueError("Snapshot duration must be positive.")
        return hours

    def _fractional_loss(self, energy_before_kWh: float, dt_h: float) -> float:
        if self.loss_model not in {"fraction", "both"}:
            return 0.0
        if energy_before_kWh <= self.min_energy_kWh:
            return 0.0
        loss_fraction = 1.0 - (1.0 - self.standby_loss_fraction_per_day) ** (dt_h / 24.0)
        return min(max(energy_before_kWh - self.min_energy_kWh, 0.0), energy_before_kWh * loss_fraction)

    def ambient_heat_gain_W(self, ambient_temperature_degC: Optional[float]) -> float:
        """Heat entering the store right now, in W (0 when no UA model is active)."""
        if self.loss_model not in {"ua", "both"} or self.thermal is None:
            return 0.0
        return max(0.0, self.thermal.heat_gain_W(ambient_temperature_degC, self.soc))

    def _storage_ambient(self, snapshot: object) -> Optional[float]:
        """Ambient temperature seen by the tank shell."""
        metadata = getattr(snapshot, "metadata", None) or {}
        get = metadata.get if hasattr(metadata, "get") else (lambda *_: None)
        for key in (f"{self.label}_ambient_temperature_degC", "storage_ambient_temperature_degC"):
            value = get(key, None)
            if value is not None:
                return float(value)
        ambient = getattr(snapshot, "ambient_temperature", None)
        return None if ambient is None else float(ambient)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _metadata_value(self, snapshot: object, key: str, default: Optional[object] = None) -> Optional[object]:
        metadata = getattr(snapshot, "metadata", None) or {}
        return metadata.get(key, default) if hasattr(metadata, "get") else default

    def request_from_snapshot(
        self,
        snapshot: object,
        base_chiller_load_W: Optional[float] = None,
        mode: Optional[str] = None,
        target_chiller_load_W: Optional[float] = None,
        charge_power_W: Optional[float] = None,
        discharge_power_W: Optional[float] = None,
    ) -> tuple[str, float]:
        """Return ``(mode, requested_storage_power_W)`` for a snapshot."""
        selected_mode = str(
            mode
            or self._metadata_value(snapshot, "storage_mode", None)
            or self._metadata_value(snapshot, f"{self.label}_mode", None)
            or "auto"
        ).lower()

        explicit_power = self._metadata_value(snapshot, "storage_power_W", None)
        if explicit_power is None:
            explicit_power = self._metadata_value(snapshot, f"{self.label}_power_W", None)
        if explicit_power is not None:
            return selected_mode, float(explicit_power)

        charge_req = charge_power_W
        if charge_req is None:
            charge_req = self._metadata_value(snapshot, "storage_charge_power_W", None)
        if charge_req is None:
            charge_req = self._metadata_value(snapshot, f"{self.label}_charge_power_W", None)

        discharge_req = discharge_power_W
        if discharge_req is None:
            discharge_req = self._metadata_value(snapshot, "storage_discharge_power_W", None)
        if discharge_req is None:
            discharge_req = self._metadata_value(snapshot, f"{self.label}_discharge_power_W", None)

        if selected_mode == "charge":
            power = -float(charge_req if charge_req is not None else self.max_charge_kW * 1000.0)
        elif selected_mode == "discharge":
            power = float(discharge_req if discharge_req is not None else self.max_discharge_kW * 1000.0)
        elif selected_mode == "idle":
            power = 0.0
        elif selected_mode in {"auto", "load_leveling", "load-levelling", "load_level"}:
            target = target_chiller_load_W
            if target is None:
                target = self._metadata_value(snapshot, "target_chiller_load_W", None)
            if target is None and self.target_chiller_load_kW is not None:
                target = self.target_chiller_load_kW * 1000.0
            if target is None:
                power = 0.0
                selected_mode = "idle"
            else:
                if base_chiller_load_W is None:
                    base_chiller_load_W = getattr(snapshot, "total_building_load", 0.0)
                power = float(base_chiller_load_W) - float(target)
                selected_mode = "discharge" if power > 0 else ("charge" if power < 0 else "idle")
        else:
            raise ValueError(f"Unsupported storage mode '{selected_mode}'.")

        return selected_mode, float(power)

    def _temperature_allows(self, snapshot: object, requested_power_W: float) -> bool:
        """Return whether optional temperature guards allow operation."""
        if requested_power_W == 0:
            return True
        condenser_in = getattr(snapshot, "condenser_inlet_temperature", None)
        ambient = getattr(snapshot, "ambient_temperature", None)
        # Use condenser inlet as the most common explicit operating temperature;
        # fall back to ambient if it is the only available boundary.
        temp = condenser_in if condenser_in is not None else ambient
        if temp is None:
            return True
        temp = float(temp)
        if requested_power_W < 0 and self.charge_allowed_above_degC is not None:
            return temp <= float(self.charge_allowed_above_degC)
        if requested_power_W > 0 and self.discharge_allowed_below_degC is not None:
            return temp >= float(self.discharge_allowed_below_degC)
        return True

    def dispatch(
        self,
        snapshot: object,
        base_chiller_load_W: Optional[float] = None,
        mode: Optional[str] = None,
        requested_storage_power_W: Optional[float] = None,
        target_chiller_load_W: Optional[float] = None,
        charge_power_W: Optional[float] = None,
        discharge_power_W: Optional[float] = None,
        supply_temperature_degC: Optional[float] = None,
        return_temperature_degC: Optional[float] = None,
    ) -> StorageDispatchResult:
        """Advance storage state by one snapshot and return the dispatch result.

        ``supply_temperature_degC`` and ``return_temperature_degC`` are the
        chilled-water temperatures the tank's ports actually see. They matter
        only for a stratified store, where they are the inlet conditions of the
        charge and discharge paths; a scalar store ignores them. When they are
        not given the tank's design temperatures are used.
        """
        dt_h = self._duration_hours(getattr(snapshot, "resolution", 1.0))
        energy_before = float(self.energy_kWh)
        soc_before = self.soc

        ambient = self._storage_ambient(snapshot)
        if self.stratified is not None:
            # The tank owns its own losses: it computes the shell gain layer by
            # layer, so charging the scalar channels as well would double-count.
            medium_T = self.stratified.cold_port_temperature_degC
            gain_W = gain_kWh = fractional_kWh = 0.0
        else:
            medium_T = self.thermal.medium_temperature_degC(soc_before) if self.thermal else None
            gain_W = self.ambient_heat_gain_W(ambient)
            gain_kWh = gain_W * dt_h / 1000.0
            fractional_kWh = self._fractional_loss(energy_before, dt_h)
        # Both loss channels erode stored cooling but can never take the store
        # below its reserve floor.
        loss = min(max(energy_before - self.min_energy_kWh, 0.0), gain_kWh + fractional_kWh)
        available_after_loss = max(self.min_energy_kWh, energy_before - loss)

        if requested_storage_power_W is None:
            selected_mode, requested = self.request_from_snapshot(
                snapshot,
                base_chiller_load_W=base_chiller_load_W,
                mode=mode,
                target_chiller_load_W=target_chiller_load_W,
                charge_power_W=charge_power_W,
                discharge_power_W=discharge_power_W,
            )
        else:
            requested = float(requested_storage_power_W)
            selected_mode = mode or ("discharge" if requested > 0 else ("charge" if requested < 0 else "idle"))

        if not self._temperature_allows(snapshot, requested):
            requested = 0.0
            selected_mode = "idle_temperature_guard"

        if self.stratified is not None:
            return self._dispatch_stratified(
                snapshot=snapshot,
                requested=float(requested),
                selected_mode=str(selected_mode),
                dt_h=dt_h,
                energy_before=energy_before,
                soc_before=soc_before,
                ambient=ambient,
                base_chiller_load_W=base_chiller_load_W,
                supply_temperature_degC=supply_temperature_degC,
                return_temperature_degC=return_temperature_degC,
            )

        charge_power_W = 0.0
        discharge_power_W = 0.0
        actual_power_W = 0.0

        if requested < 0:
            requested_charge_kW = abs(requested) / 1000.0
            remaining_storable_kWh = max(self.max_energy_kWh - available_after_loss, 0.0)
            energy_limited_charge_kW = remaining_storable_kWh / (self.charge_efficiency * dt_h)
            actual_charge_kW = min(requested_charge_kW, self.max_charge_kW, energy_limited_charge_kW)
            charge_power_W = actual_charge_kW * 1000.0
            actual_power_W = -charge_power_W
            energy_after = available_after_loss + self.charge_efficiency * actual_charge_kW * dt_h
        elif requested > 0:
            requested_discharge_kW = requested / 1000.0
            available_deliverable_kWh = max(available_after_loss - self.min_energy_kWh, 0.0) * self.discharge_efficiency
            energy_limited_discharge_kW = available_deliverable_kWh / dt_h
            actual_discharge_kW = min(requested_discharge_kW, self.max_discharge_kW, energy_limited_discharge_kW)
            discharge_power_W = actual_discharge_kW * 1000.0
            actual_power_W = discharge_power_W
            energy_after = available_after_loss - actual_discharge_kW * dt_h / self.discharge_efficiency
        else:
            energy_after = available_after_loss

        energy_after = min(self.max_energy_kWh, max(self.min_energy_kWh, energy_after))
        self.energy_kWh = energy_after
        chiller_offset_W = charge_power_W - discharge_power_W
        effective_chiller_load_W = None if base_chiller_load_W is None else float(base_chiller_load_W) + chiller_offset_W
        curtailed = abs(float(requested) - actual_power_W)
        result = StorageDispatchResult(
            timestamp=getattr(snapshot, "timestamp", None),
            mode=str(selected_mode),
            requested_storage_power_W=float(requested),
            storage_power_W=float(actual_power_W),
            charge_power_W=float(charge_power_W),
            discharge_power_W=float(discharge_power_W),
            chiller_load_offset_W=float(chiller_offset_W),
            energy_before_kWh=energy_before,
            energy_after_kWh=energy_after,
            soc_before=soc_before,
            soc_after=self.soc,
            standby_loss_kWh=float(loss),
            curtailed_request_W=float(curtailed),
            effective_chiller_load_W=effective_chiller_load_W,
            ambient_temperature_degC=ambient,
            medium_temperature_degC=medium_T,
            ambient_heat_gain_W=float(gain_W),
            ambient_heat_gain_kWh=float(gain_kWh),
            fractional_loss_kWh=float(fractional_kWh),
        )
        self.history.append(result)
        return result

    def _dispatch_stratified(
        self,
        snapshot: object,
        requested: float,
        selected_mode: str,
        dt_h: float,
        energy_before: float,
        soc_before: float,
        ambient: Optional[float],
        base_chiller_load_W: Optional[float],
        supply_temperature_degC: Optional[float],
        return_temperature_degC: Optional[float],
    ) -> StorageDispatchResult:
        """Dispatch a stratified tank for one snapshot.

        The controller still asks for a *power*, exactly as it does for a scalar
        store, so every mode, guard and precedence rule above applies unchanged.
        What differs is that the power is not simply granted: it is converted
        into the mass flow that would deliver it against the temperature
        difference the tank can offer *right now*, that flow is clipped to the
        tank's rating and to the state-of-charge band, and the power that comes
        back is whatever the layer physics actually produced.

        This is the whole point of the stratified model. As the thermocline
        reaches the outlet the available temperature difference collapses, so the
        same requested power needs a flow the tank cannot pass, and the delivered
        power falls away, where a scalar store would happily keep delivering
        its rated power until state of charge hit zero.
        """
        tank = self.stratified
        t_supply = (
            tank.charged_temperature_degC
            if supply_temperature_degC is None
            else float(supply_temperature_degC)
        )
        t_return = (
            tank.discharged_temperature_degC
            if return_temperature_degC is None
            else float(return_temperature_degC)
        )

        # Clip the request so one step cannot drive the tank through its band.
        # This is an energy-rate bound, so it is approximate at the edges; the
        # tank's own physics is the backstop.
        if requested < 0:
            headroom_kWh = max(self.max_energy_kWh - energy_before, 0.0)
            requested = -min(abs(requested), headroom_kWh * 1000.0 / max(dt_h, 1e-9))
        elif requested > 0:
            available_kWh = max(energy_before - self.min_energy_kWh, 0.0)
            requested = min(requested, available_kWh * 1000.0 / max(dt_h, 1e-9))

        mode, inlet, flow = "idle", None, 0.0
        if requested < -1e-6:
            mode, inlet = "charge", t_supply
            flow = tank.mass_flow_for_power(requested, inlet, "charge")
        elif requested > 1e-6:
            mode, inlet = "discharge", t_return
            flow = tank.mass_flow_for_power(requested, inlet, "discharge")

        if mode != "idle":
            rating = float(self.max_tank_flow_kg_s or math.inf)
            flow = min(flow, rating)
            if not math.isfinite(flow) or flow <= 1e-9:
                mode, inlet, flow = "idle", None, 0.0

        step = tank.step(
            duration_h=dt_h,
            mass_flow_kg_s=flow,
            inlet_temperature_degC=inlet,
            mode=mode,
            ambient_temperature_degC=ambient,
        )

        actual_power_W = step.discharge_power_W - step.charge_power_W
        chiller_offset_W = step.charge_power_W - step.discharge_power_W
        effective = (
            None if base_chiller_load_W is None
            else float(base_chiller_load_W) + chiller_offset_W
        )
        # Losses show up as the difference between what the loop exchanged and
        # what the tank's stored energy actually did, so report the shell gain
        # from the tank rather than recomputing it.
        gain_kWh = step.ambient_heat_gain_W * dt_h / 1000.0

        reported_mode = (
            selected_mode if selected_mode.startswith("idle_") else step.mode
        )
        result = StorageDispatchResult(
            timestamp=getattr(snapshot, "timestamp", None),
            mode=reported_mode,
            requested_storage_power_W=float(requested),
            storage_power_W=float(actual_power_W),
            charge_power_W=float(step.charge_power_W),
            discharge_power_W=float(step.discharge_power_W),
            chiller_load_offset_W=float(chiller_offset_W),
            energy_before_kWh=energy_before,
            energy_after_kWh=step.energy_after_kWh,
            soc_before=soc_before,
            soc_after=step.soc_after,
            standby_loss_kWh=float(max(gain_kWh, 0.0)),
            curtailed_request_W=float(max(abs(requested) - abs(actual_power_W), 0.0)),
            effective_chiller_load_W=effective,
            ambient_temperature_degC=ambient,
            medium_temperature_degC=tank.cold_port_temperature_degC,
            ambient_heat_gain_W=float(step.ambient_heat_gain_W),
            ambient_heat_gain_kWh=float(gain_kWh),
            fractional_loss_kWh=0.0,
            tank_mass_flow_kg_s=float(step.mass_flow_kg_s),
            tank_outlet_temperature_degC=(
                None if step.outlet_temperature_degC != step.outlet_temperature_degC
                else float(step.outlet_temperature_degC)
            ),
            tank_inlet_temperature_degC=(
                None if inlet is None else float(inlet)
            ),
            thermocline_thickness_m=float(step.thermocline_thickness_m),
            cold_port_temperature_degC=float(tank.cold_port_temperature_degC),
            warm_port_temperature_degC=float(tank.warm_port_temperature_degC),
            tank_profile_degC=tuple(step.profile_degC),
        )
        self.history.append(result)
        return result

    def apply_to_snapshot(
        self,
        snapshot: object,
        buildings: object,
        chiller: Optional[object] = None,
        cooling_tower: Optional[object] = None,
        base_chiller_load_offset_W: float = 0.0,
        update_chiller: bool = True,
        update_cooling_tower: bool = True,
        mode: Optional[str] = None,
        target_chiller_load_W: Optional[float] = None,
    ) -> StorageDispatchResult:
        """Dispatch storage and apply the modified load to modular components."""
        base_load = float(getattr(snapshot, "total_building_load", 0.0)) + float(base_chiller_load_offset_W)
        result = self.dispatch(
            snapshot,
            base_chiller_load_W=base_load,
            mode=mode,
            target_chiller_load_W=target_chiller_load_W,
        )
        if not hasattr(snapshot, "apply"):
            raise TypeError("snapshot must provide an apply(...) method compatible with TimeSnapshot.")
        snapshot.apply(
            buildings=buildings,
            chiller=chiller,
            cooling_tower=cooling_tower,
            update_chiller=update_chiller,
            update_cooling_tower=update_cooling_tower,
            chiller_load_offset_W=float(base_chiller_load_offset_W) + result.chiller_load_offset_W,
        )
        return result

    def make_effective_snapshot(
        self,
        snapshot: object,
        dispatch_result: StorageDispatchResult,
        minimum_load_fraction: float = 0.05,
    ) -> object:
        """Return a TimeSnapshot whose building loads equal the net plant load.

        .. warning::
           This is the legacy ``coupling="supervisory"`` approximation. It keeps
           the network energy-balanced by **rescaling every building's heat
           duty**, so in a storage run the building heat exchangers no longer
           serve their real demand and the solved chilled-water return
           temperature is not the return temperature of the real system. It also
           assumes charging is free of any efficiency penalty. Prefer
           ``coupling="hydraulic"``, which represents the store as an actual heat
           flow in the chilled-water loop.
        """
        from .time_snapshot import TimeSnapshot

        original_loads = dict(getattr(snapshot, "building_loads", {}))
        actual_total = float(sum(original_loads.values()))
        net_total = actual_total + float(dispatch_result.chiller_load_offset_W)
        minimum_total = max(0.0, minimum_load_fraction * actual_total)
        net_total = max(minimum_total, net_total)

        if actual_total > 0:
            factor = net_total / actual_total
            effective_loads = {label: float(q) * factor for label, q in original_loads.items()}
        else:
            effective_loads = original_loads

        metadata = dict(getattr(snapshot, "metadata", {}) or {})
        metadata.update(
            {
                "actual_building_load_W": actual_total,
                "effective_building_load_W": net_total,
                "storage_power_W": dispatch_result.storage_power_W,
                "storage_charge_power_W": dispatch_result.charge_power_W,
                "storage_discharge_power_W": dispatch_result.discharge_power_W,
                "storage_chiller_load_offset_W": dispatch_result.chiller_load_offset_W,
                "storage_soc_after": dispatch_result.soc_after,
                "storage_mode": dispatch_result.mode,
            }
        )
        return TimeSnapshot(
            timestamp=getattr(snapshot, "timestamp"),
            building_loads=effective_loads,
            resolution=getattr(snapshot, "resolution"),
            ambient_temperature=getattr(snapshot, "ambient_temperature", None),
            condenser_inlet_temperature=getattr(snapshot, "condenser_inlet_temperature", None),
            metadata=metadata,
        )

    def dispatch_and_make_effective_snapshot(
        self,
        snapshot: object,
        base_chiller_load_offset_W: float = 0.0,
        mode: Optional[str] = None,
        target_chiller_load_W: Optional[float] = None,
        minimum_load_fraction: float = 0.05,
        supply_temperature_degC: Optional[float] = None,
        return_temperature_degC: Optional[float] = None,
    ) -> tuple[StorageDispatchResult, object]:
        """Dispatch storage and return ``(dispatch_result, effective_snapshot)``."""
        base_load = float(getattr(snapshot, "total_building_load", 0.0)) + float(base_chiller_load_offset_W)
        result = self.dispatch(
            snapshot,
            base_chiller_load_W=base_load,
            mode=mode,
            target_chiller_load_W=target_chiller_load_W,
            supply_temperature_degC=supply_temperature_degC,
            return_temperature_degC=return_temperature_degC,
        )
        return result, self.make_effective_snapshot(snapshot, result, minimum_load_fraction=minimum_load_fraction)

    def history_records(self, prefix: str = "storage") -> list[Dict[str, object]]:
        """Return dispatch history as flat records."""
        return [result.to_record(prefix=prefix) for result in self.history]


__all__ = ["ColdStorage", "StorageDispatchResult"]
