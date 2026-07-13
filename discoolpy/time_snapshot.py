"""
Reusable time snapshot utilities for modular TESPy district cooling models.

The classes in this module intentionally do not create TESPy components. They
represent time-dependent operating data and provide an ``apply`` method that
updates already-created modular Building, Chiller, and CoolingTower instances
before each steady-state solve. This keeps the network topology unchanged while
making both load and ambient operating conditions time-dependent.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Union


TimestampLike = Union[str, datetime]


_RESOLUTION_ALIASES: Dict[str, timedelta] = {
    "10min": timedelta(minutes=10),
    "10-min": timedelta(minutes=10),
    "10_min": timedelta(minutes=10),
    "10 minutes": timedelta(minutes=10),
    "15min": timedelta(minutes=15),
    "15-min": timedelta(minutes=15),
    "15_min": timedelta(minutes=15),
    "15 minutes": timedelta(minutes=15),
    "30min": timedelta(minutes=30),
    "30-min": timedelta(minutes=30),
    "30_min": timedelta(minutes=30),
    "30 minutes": timedelta(minutes=30),
    "hourly": timedelta(hours=1),
    "1h": timedelta(hours=1),
    "hour": timedelta(hours=1),
    "daily": timedelta(days=1),
    "1d": timedelta(days=1),
    "day": timedelta(days=1),
}


def parse_timestamp(value: TimestampLike) -> datetime:
    """Return a ``datetime`` from either a datetime object or an ISO-like string."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def parse_resolution(resolution: Union[str, timedelta, int, float]) -> timedelta:
    """Convert a supported resolution descriptor to ``datetime.timedelta``.

    String aliases cover 10-minute, 15-minute, 30-minute, hourly, multi-hour,
    and daily snapshots. Numeric values are interpreted as hours, so ``2`` means
    a two-hour snapshot interval.
    """
    if isinstance(resolution, timedelta):
        return resolution
    if isinstance(resolution, (int, float)):
        if resolution <= 0:
            raise ValueError("Resolution in hours must be positive.")
        return timedelta(hours=float(resolution))

    key = str(resolution).strip().lower().replace(" ", "")
    if key in _RESOLUTION_ALIASES:
        return _RESOLUTION_ALIASES[key]

    if key.endswith("h"):
        hours = float(key[:-1])
        if hours <= 0:
            raise ValueError("Multi-hour resolution must be positive.")
        return timedelta(hours=hours)
    if key.endswith("min"):
        minutes = float(key[:-3])
        if minutes <= 0:
            raise ValueError("Minute resolution must be positive.")
        return timedelta(minutes=minutes)
    if key.endswith("d"):
        days = float(key[:-1])
        if days <= 0:
            raise ValueError("Daily resolution must be positive.")
        return timedelta(days=days)

    raise ValueError(
        "Unsupported resolution. Use one of 10min, 15min, 30min, hourly, "
        "multi-hour descriptors such as 2h, or daily."
    )


@dataclass(frozen=True)
class TimeSnapshot:
    """One steady-state operating point for the district cooling model.

    Parameters
    ----------
    timestamp:
        Snapshot timestamp as a ``datetime`` or ISO-like string.
    building_loads:
        Mapping from building labels to cooling loads in W. Values are positive
        because buildings add heat to the chilled-water loop.
    resolution:
        Time interval represented by the snapshot. Strings such as ``10min``,
        ``15min``, ``30min``, ``hourly``, ``2h``, and ``daily`` are supported.
    ambient_temperature:
        Ambient dry-bulb or wet-bulb proxy temperature in degC for the snapshot.
        It is kept explicit because realistic offdesign operation normally varies
        cooling-source conditions together with the building demand.
    condenser_inlet_temperature:
        Condenser-water temperature entering the chiller in degC, usually the
        cooling-tower leaving-water temperature. If omitted, compatible cooling
        tower wrappers may derive it from ``ambient_temperature`` and their
        configured approach.
    metadata:
        Optional additional variables such as relative humidity, wet-bulb
        temperature, electricity price, or operator notes.
    """

    timestamp: TimestampLike
    building_loads: Mapping[str, float]
    resolution: Union[str, timedelta, int, float] = "hourly"
    ambient_temperature: Optional[float] = None
    condenser_inlet_temperature: Optional[float] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", parse_timestamp(self.timestamp))
        object.__setattr__(self, "resolution", parse_resolution(self.resolution))
        object.__setattr__(
            self,
            "building_loads",
            {str(label): float(q) for label, q in self.building_loads.items()},
        )
        if self.ambient_temperature is not None:
            object.__setattr__(self, "ambient_temperature", float(self.ambient_temperature))
        if self.condenser_inlet_temperature is not None:
            object.__setattr__(
                self,
                "condenser_inlet_temperature",
                float(self.condenser_inlet_temperature),
            )

    @property
    def total_building_load(self) -> float:
        """Return the sum of all building loads in W."""
        return float(sum(self.building_loads.values()))

    def get_building_load(self, building_label: str) -> float:
        """Return the demand for a named building or raise a clear error."""
        if building_label not in self.building_loads:
            raise KeyError(f"Snapshot has no demand value for building '{building_label}'.")
        return float(self.building_loads[building_label])

    def apply(
        self,
        buildings: Union[Mapping[str, object], Sequence[object]],
        chiller: Optional[object] = None,
        cooling_tower: Optional[object] = None,
        update_chiller: bool = True,
        update_cooling_tower: bool = True,
        chiller_load_offset_W: float = 0.0,
    ) -> float:
        """Apply this snapshot to compatible modular model objects.

        ``buildings`` can be a mapping of labels to Building instances or a
        sequence of Building instances. The method calls ``set_snapshot_demand``
        when available and otherwise updates ``building.component.Q`` directly.
        If ``chiller`` provides ``update_Q_evap``, the chiller evaporator duty is
        updated to the sum of all building heat gains plus
        ``chiller_load_offset_W``. If ``cooling_tower`` provides
        ``apply_snapshot`` or ``set_offdesign_ambient``, ambient and
        condenser-water fields are applied before the network solve.
        """
        if isinstance(buildings, Mapping):
            building_items = list(buildings.items())
        else:
            building_items = [(getattr(b, "label", None), b) for b in buildings]

        for label, building in building_items:
            if label is None:
                raise ValueError("Every building must expose a 'label' attribute.")
            q = self.get_building_load(str(label))
            if hasattr(building, "set_snapshot_demand"):
                building.set_snapshot_demand(self)
            elif hasattr(building, "component"):
                building.component.set_attr(Q=q)
            else:
                raise TypeError(f"Object for '{label}' is not a supported Building instance.")

        total_q = self.total_building_load
        if update_chiller and chiller is not None:
            if not hasattr(chiller, "update_Q_evap"):
                raise TypeError("Chiller object must provide update_Q_evap(new_Q_W).")
            chiller.update_Q_evap(total_q + float(chiller_load_offset_W))

        if update_cooling_tower and cooling_tower is not None:
            if hasattr(cooling_tower, "apply_snapshot"):
                cooling_tower.apply_snapshot(self)
            elif hasattr(cooling_tower, "set_offdesign_ambient"):
                cooling_tower.set_offdesign_ambient(
                    ambient_temperature=self.ambient_temperature,
                    condenser_inlet_temperature=self.condenser_inlet_temperature,
                )
            else:
                raise TypeError(
                    "Cooling tower object must provide apply_snapshot(snapshot) or "
                    "set_offdesign_ambient(...)."
                )
        return total_q

    def to_record(self) -> Dict[str, object]:
        """Return a flat record suitable for CSV or pandas export."""
        record: Dict[str, object] = {
            "timestamp": self.timestamp.isoformat(),
            "resolution_seconds": int(self.resolution.total_seconds()),
            "total_building_load_W": self.total_building_load,
            "ambient_temperature_degC": self.ambient_temperature,
            "condenser_inlet_temperature_degC": self.condenser_inlet_temperature,
        }
        for label, q in self.building_loads.items():
            record[f"{label}_Q_W"] = q
        record.update({f"metadata_{k}": v for k, v in self.metadata.items()})
        return record


@dataclass
class SnapshotSchedule:
    """Ordered collection of ``TimeSnapshot`` objects."""

    snapshots: List[TimeSnapshot]
    name: str = "snapshot_schedule"

    def __post_init__(self) -> None:
        self.snapshots = sorted(self.snapshots, key=lambda snapshot: snapshot.timestamp)

    def __iter__(self) -> Iterator[TimeSnapshot]:
        return iter(self.snapshots)

    def __len__(self) -> int:
        return len(self.snapshots)

    def __getitem__(self, index: int) -> TimeSnapshot:
        return self.snapshots[index]

    @classmethod
    def from_profile_arrays(
        cls,
        start: TimestampLike,
        resolution: Union[str, timedelta, int, float],
        building_profiles: Mapping[str, Sequence[float]],
        name: str = "profile_schedule",
        ambient_temperatures: Optional[Sequence[float]] = None,
        condenser_inlet_temperatures: Optional[Sequence[float]] = None,
        metadata_profile: Optional[Mapping[str, Sequence[object]]] = None,
    ) -> "SnapshotSchedule":
        """Create snapshots from equal-length demand, ambient, and metadata arrays."""
        if not building_profiles:
            raise ValueError("At least one building profile is required.")
        lengths = {len(values) for values in building_profiles.values()}
        if len(lengths) != 1:
            raise ValueError("All building demand profiles must have the same length.")

        step = parse_resolution(resolution)
        timestamp = parse_timestamp(start)
        count = lengths.pop()
        if ambient_temperatures is not None and len(ambient_temperatures) != count:
            raise ValueError("Ambient temperature profile must match the demand profile length.")
        if condenser_inlet_temperatures is not None and len(condenser_inlet_temperatures) != count:
            raise ValueError("Condenser inlet temperature profile must match the demand profile length.")
        if metadata_profile is not None:
            for key, values in metadata_profile.items():
                if len(values) != count:
                    raise ValueError(f"Metadata profile '{key}' must match the demand profile length.")

        snapshots: List[TimeSnapshot] = []
        for idx in range(count):
            loads = {label: float(values[idx]) for label, values in building_profiles.items()}
            metadata = (
                {key: values[idx] for key, values in metadata_profile.items()}
                if metadata_profile is not None
                else {}
            )
            snapshots.append(
                TimeSnapshot(
                    timestamp + idx * step,
                    loads,
                    step,
                    ambient_temperature=(
                        None if ambient_temperatures is None else ambient_temperatures[idx]
                    ),
                    condenser_inlet_temperature=(
                        None
                        if condenser_inlet_temperatures is None
                        else condenser_inlet_temperatures[idx]
                    ),
                    metadata=metadata,
                )
            )
        return cls(snapshots=snapshots, name=name)

    @classmethod
    def from_csv(
        cls,
        csv_path: Union[str, Path],
        timestamp_column: str,
        building_columns: Mapping[str, str],
        resolution: Union[str, timedelta, int, float] = "hourly",
        multiplier: float = 1.0,
        delimiter: str = ",",
        name: Optional[str] = None,
        ambient_column: Optional[str] = None,
        condenser_inlet_column: Optional[str] = None,
        metadata_columns: Optional[Mapping[str, str]] = None,
    ) -> "SnapshotSchedule":
        """Create snapshots from a CSV file with one demand column per building.

        ``building_columns`` maps modular Building labels to CSV column names.
        ``multiplier`` converts the file units to W; use ``1000`` when the CSV is
        in kW and the default ``1`` when the CSV is already in W. Optional
        ambient and condenser-water columns are interpreted in degC.
        """
        path = Path(csv_path)
        snapshots: List[TimeSnapshot] = []
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            required = [timestamp_column, *building_columns.values()]
            if ambient_column is not None:
                required.append(ambient_column)
            if condenser_inlet_column is not None:
                required.append(condenser_inlet_column)
            if metadata_columns:
                required.extend(metadata_columns.values())
            missing = [column for column in required if column not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"Missing required CSV columns: {missing}")
            for row in reader:
                loads = {
                    label: float(row[column]) * multiplier
                    for label, column in building_columns.items()
                }
                metadata = (
                    {key: row[column] for key, column in metadata_columns.items()}
                    if metadata_columns
                    else {}
                )
                snapshots.append(
                    TimeSnapshot(
                        row[timestamp_column],
                        loads,
                        resolution,
                        ambient_temperature=(
                            None if ambient_column is None else float(row[ambient_column])
                        ),
                        condenser_inlet_temperature=(
                            None
                            if condenser_inlet_column is None
                            else float(row[condenser_inlet_column])
                        ),
                        metadata=metadata,
                    )
                )
        return cls(snapshots=snapshots, name=name or path.stem)

    def apply(
        self,
        index: int,
        buildings: Union[Mapping[str, object], Sequence[object]],
        chiller: Optional[object] = None,
        cooling_tower: Optional[object] = None,
        update_chiller: bool = True,
        update_cooling_tower: bool = True,
        chiller_load_offset_W: float = 0.0,
    ) -> float:
        """Apply the snapshot at ``index`` and return total building load in W."""
        return self.snapshots[index].apply(
            buildings,
            chiller,
            cooling_tower=cooling_tower,
            update_chiller=update_chiller,
            update_cooling_tower=update_cooling_tower,
            chiller_load_offset_W=chiller_load_offset_W,
        )

    def records(self) -> List[Dict[str, object]]:
        """Return all snapshots as flat records suitable for DataFrame creation."""
        return [snapshot.to_record() for snapshot in self.snapshots]


__all__ = ["TimeSnapshot", "SnapshotSchedule", "parse_resolution", "parse_timestamp"]
