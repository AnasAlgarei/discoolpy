"""
Reusable TESPy building module for district cooling networks.

The Building class is intentionally lightweight: it wraps a TESPy
SimpleHeatExchanger and creates the two TESPy connections from a supply splitter
or tap to a return merge. It can hold a constant design cooling demand or load an
hourly demand profile and apply one timestep at a time.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from tespy.components import SimpleHeatExchanger
from tespy.connections import Connection


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
    """

    label: str
    Q_design: float
    pr: Optional[float] = None
    demand_profile: Optional[List[float]] = None
    heat_exchanger: SimpleHeatExchanger = field(init=False)
    inlet: Optional[Connection] = field(default=None, init=False)
    outlet: Optional[Connection] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.heat_exchanger = SimpleHeatExchanger(self.label)

    @property
    def component(self) -> SimpleHeatExchanger:
        """Return the underlying TESPy component."""
        return self.heat_exchanger

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
        """Apply the design heat load and optional branch mass-flow anchor.

        When ``native_offdesign=True``, the building heat load remains a snapshot
        specification, while the water-side pressure ratio can switch to TESPy's
        native ``zeta`` offdesign characteristic if a design pressure ratio is
        available.
        """
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
        """Load an hourly demand profile from a CSV column.

        The file must contain a header row. Values are multiplied by
        ``multiplier`` so the method can import kW data with
        ``multiplier=1000`` or W data with the default multiplier.
        """
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
        q = self.demand_profile[hour_index]
        return self.set_demand(q)

    def set_demand(self, q_W: float) -> float:
        """Apply a positive building cooling demand to the TESPy heat exchanger.

        Building loads are represented as positive heat gains to the chilled
        water stream. The value is returned to simplify aggregation in time-step
        loops.
        """
        q = float(q_W)
        if q < 0:
            raise ValueError("Building cooling demand must be positive in W.")
        self.heat_exchanger.set_attr(Q=q)
        return q

    def set_snapshot_demand(self, snapshot) -> float:
        """Read this building's load from a TimeSnapshot and apply it."""
        if hasattr(snapshot, "get_building_load"):
            q = snapshot.get_building_load(self.label)
        elif hasattr(snapshot, "building_loads"):
            q = snapshot.building_loads[self.label]
        else:
            raise TypeError("snapshot must expose get_building_load() or building_loads.")
        return self.set_demand(q)

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


__all__ = ["Building"]
