"""
Optional cold/ice thermal storage component for modular TESPy district cooling.

The component is intentionally implemented as a plant-side supervisory component
rather than as an additional TESPy hydraulic element. It keeps an inter-snapshot
state of charge and modifies the evaporator load imposed on an existing Chiller
before each TESPy offdesign solve. This allows the storage model to be added to
any already-working modular network without changing splitters, merges, pumps, or
cycle closers.

Sign convention
---------------
``storage_power_W > 0`` means discharge: storage supplies cooling and therefore
reduces the chiller evaporator duty. ``storage_power_W < 0`` means charge:
storage consumes cooling from the chiller and therefore increases evaporator
load. The load offset passed to ``TimeSnapshot.apply`` is
``charge_power_W - discharge_power_W``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, Mapping, Optional


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

    def to_record(self, prefix: str = "storage") -> Dict[str, object]:
        """Return a flat dictionary for CSV or pandas exports."""
        return {
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
            f"{prefix}_curtailed_request_W": self.curtailed_request_W,
            f"{prefix}_effective_chiller_load_W": self.effective_chiller_load_W,
        }


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
        Fraction of stored cooling lost per day while idle or operating.
    storage_type:
        Descriptive storage type such as ``ice``, ``chilled_water``, or ``pcm``.
    target_chiller_load_kW:
        Optional default load-leveling target. If the building load plus base
        offset is above the target, storage discharges. If below the target,
        storage charges.
    min_soc, max_soc:
        Operating SOC bounds. They allow a reserve margin without changing the
        nominal capacity.
    charge_allowed_above_degC, discharge_allowed_below_degC:
        Optional temperature feasibility guards. For ice storage, charging is
        commonly restricted to sufficiently cold chilled-water supply conditions,
        while discharging can be restricted when the plant boundary is already
        cold enough.
    """

    label: str
    capacity_kWh: float
    initial_soc: float = 0.5
    max_charge_kW: float = 250.0
    max_discharge_kW: float = 250.0
    charge_efficiency: float = 0.92
    discharge_efficiency: float = 0.94
    standby_loss_fraction_per_day: float = 0.02
    storage_type: str = "ice"
    target_chiller_load_kW: Optional[float] = None
    min_soc: float = 0.05
    max_soc: float = 0.98
    charge_allowed_above_degC: Optional[float] = None
    discharge_allowed_below_degC: Optional[float] = None
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
        self.energy_kWh = min(
            self.max_energy_kWh,
            max(self.min_energy_kWh, float(self.initial_soc) * self.capacity_kWh),
        )
        self.history = []

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
        self.energy_kWh = min(self.max_energy_kWh, max(self.min_energy_kWh, new_soc * self.capacity_kWh))
        self.history = []

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

    def _standby_loss(self, energy_before_kWh: float, dt_h: float) -> float:
        """Return the storage standby loss for one time step."""
        if energy_before_kWh <= self.min_energy_kWh:
            return 0.0
        loss_fraction = 1.0 - (1.0 - self.standby_loss_fraction_per_day) ** (dt_h / 24.0)
        return min(max(energy_before_kWh - self.min_energy_kWh, 0.0), energy_before_kWh * loss_fraction)

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
        """Return ``(mode, requested_storage_power_W)`` for a snapshot.

        The method first checks explicit function arguments and then snapshot
        metadata keys. Positive requested power discharges storage; negative
        requested power charges storage.
        """
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
    ) -> StorageDispatchResult:
        """Advance storage state by one snapshot and return the dispatch result."""
        dt_h = self._duration_hours(getattr(snapshot, "resolution", 1.0))
        energy_before = float(self.energy_kWh)
        soc_before = self.soc
        loss = self._standby_loss(energy_before, dt_h)
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
        """Dispatch storage and apply the modified load to modular components.

        ``base_chiller_load_offset_W`` is typically pump heat. The snapshot is
        then applied with the combined offset
        ``base_chiller_load_offset_W + storage_result.chiller_load_offset_W``.
        """
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

        The physical building demand is preserved in metadata while the load seen by
        the TESPy hydraulic network is scaled to
        ``building_demand + charge_power - discharge_power``. This is useful when
        the storage tank is represented as an aggregate plant-side supervisory
        component rather than as an explicit hydraulic branch. It keeps the TESPy
        steady-state network energy-balanced for each offdesign solve while still
        reporting the actual load, charge, discharge, and state of charge.
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
    ) -> tuple[StorageDispatchResult, object]:
        """Dispatch storage and return ``(dispatch_result, effective_snapshot)``.

        Use the returned effective snapshot with ``TimeSnapshot.apply(...,
        chiller_load_offset_W=base_chiller_load_offset_W)``. This pattern is the
        recommended approach for optional aggregate storage in an existing modular
        network because the network solves the net plant load while post-processing
        retains the actual building cooling demand.
        """
        base_load = float(getattr(snapshot, "total_building_load", 0.0)) + float(base_chiller_load_offset_W)
        result = self.dispatch(
            snapshot,
            base_chiller_load_W=base_load,
            mode=mode,
            target_chiller_load_W=target_chiller_load_W,
        )
        return result, self.make_effective_snapshot(snapshot, result, minimum_load_fraction=minimum_load_fraction)

    def history_records(self, prefix: str = "storage") -> list[Dict[str, object]]:
        """Return dispatch history as flat records."""
        return [result.to_record(prefix=prefix) for result in self.history]
