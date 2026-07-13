"""
Reusable TESPy branch module for district cooling street-level networks.

A Branch represents one street or feeder segment with sequential supply taps,
building heat exchangers, an end bypass, return merges, supply pipes, return
pipes, and an optional pump. The class creates TESPy components and connections
automatically from a list of Building objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from tespy.components import Merge, Pipe, Pump, Splitter, Valve
from tespy.connections import Connection

from .building import Building


_VALID_PUMP_PLACEMENTS = {None, "supply_inlet", "return_outlet"}


@dataclass
class Branch:
    """District cooling branch with splitters, buildings, bypass, and return merges.

    Parameters
    ----------
    label:
        Branch label used as a prefix for automatically created component labels.
    buildings:
        Ordered list of Building objects along the supply direction.
    bypass_m:
        Optional design bypass mass flow in kg/s at the end of the branch.
    pump_placement:
        Optional pump location. Supported values are ``None``, ``"supply_inlet"``
        and ``"return_outlet"``.
    pump_label:
        Optional explicit pump label.
    uniform_pipes:
        If ``True``, ``uniform_pipe_attrs`` is applied to every supply and return
        pipe. If ``False``, use ``pipe_attrs`` to specify individual pipe
        parameters.
    uniform_pipe_attrs:
        TESPy pipe attributes applied to all pipes when ``uniform_pipes=True``.
        Examples are ``{"pr": 0.999, "Q": 0}`` for simplified pipes, or
        geometry attributes such as ``{"L": 100, "D": 0.2, "ks": 1e-4}``.
    pipe_attrs:
        Individual pipe specifications by pipe key. Pipe keys are ``supply_1``,
        ``supply_2``, ..., ``return_1``, ``return_2``, ...
    """

    label: str
    buildings: Sequence[Building]
    bypass_m: Optional[float] = None
    pump_placement: Optional[str] = "supply_inlet"
    pump_label: Optional[str] = None
    uniform_pipes: bool = False
    uniform_pipe_attrs: Optional[Dict[str, float]] = None
    pipe_attrs: Optional[Dict[str, Dict[str, float]]] = None
    pump: Optional[Pump] = field(default=None, init=False)
    supply_pipes: List[Pipe] = field(default_factory=list, init=False)
    return_pipes: List[Pipe] = field(default_factory=list, init=False)
    splitters: List[Splitter] = field(default_factory=list, init=False)
    merges: List[Merge] = field(default_factory=list, init=False)
    bypass: Valve = field(init=False)
    connections: Dict[str, Connection] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not self.buildings:
            raise ValueError("A Branch requires at least one Building.")
        if self.pump_placement not in _VALID_PUMP_PLACEMENTS:
            raise ValueError(
                f"pump_placement must be one of {_VALID_PUMP_PLACEMENTS}, got {self.pump_placement!r}."
            )

        n = len(self.buildings)
        prefix = self.label
        self.supply_pipes = [Pipe(f"{prefix} supply_pipe_{i}") for i in range(1, n + 1)]
        self.return_pipes = [Pipe(f"{prefix} return_pipe_{i}") for i in range(1, n + 1)]
        self.splitters = [Splitter(f"{prefix} B{i} supply tap") for i in range(1, n + 1)]
        self.merges = [Merge(f"{prefix} B{i} return junction") for i in range(1, n + 1)]
        self.bypass = Valve(f"{prefix} end of line bypass")
        if self.pump_placement is not None:
            self.pump = Pump(self.pump_label or f"{prefix} pump")

    def connect_between(
        self,
        supply_source,
        supply_source_port: str,
        return_sink,
        return_sink_port: str,
        inlet_label: Optional[str] = None,
        outlet_label: Optional[str] = None,
    ) -> Sequence[Connection]:
        """Create all external and internal TESPy connections for the branch."""
        self.connections.clear()
        first_supply_target = self.supply_pipes[0]

        if self.pump_placement == "supply_inlet":
            self.connections["branch_in"] = Connection(
                supply_source,
                supply_source_port,
                self.pump,
                "in1",
                label=inlet_label or f"{self.label}_inlet_to_pump",
            )
            self.connections["pump_to_supply_1"] = Connection(
                self.pump,
                "out1",
                first_supply_target,
                "in1",
                label=f"{self.label}_pump_to_supply_pipe_1",
            )
        else:
            self.connections["branch_in"] = Connection(
                supply_source,
                supply_source_port,
                first_supply_target,
                "in1",
                label=inlet_label or f"{self.label}_inlet_to_supply_pipe_1",
            )

        self.connections["supply_1_to_splitter_1"] = Connection(
            self.supply_pipes[0],
            "out1",
            self.splitters[0],
            "in1",
            label=f"{self.label}_supply_pipe_1_to_sp1",
        )

        n = len(self.buildings)
        for i, building in enumerate(self.buildings):
            building_in, building_out = building.connect_between(
                self.splitters[i],
                "out1",
                self.merges[i],
                "in1",
                inlet_label=f"{self.label}_b{i + 1}_in",
                outlet_label=f"{self.label}_b{i + 1}_out",
            )
            self.connections[f"building_{i + 1}_in"] = building_in
            self.connections[f"building_{i + 1}_out"] = building_out

            if i < n - 1:
                self.connections[f"splitter_{i + 1}_to_supply_{i + 2}"] = Connection(
                    self.splitters[i],
                    "out2",
                    self.supply_pipes[i + 1],
                    "in1",
                    label=f"{self.label}_sp{i + 1}_to_supply_pipe_{i + 2}",
                )
                self.connections[f"supply_{i + 2}_to_splitter_{i + 2}"] = Connection(
                    self.supply_pipes[i + 1],
                    "out1",
                    self.splitters[i + 1],
                    "in1",
                    label=f"{self.label}_supply_pipe_{i + 2}_to_sp{i + 2}",
                )

        self.connections["bypass_in"] = Connection(
            self.splitters[-1],
            "out2",
            self.bypass,
            "in1",
            label=f"{self.label}_bypass_in",
        )
        self.connections["bypass_out"] = Connection(
            self.bypass,
            "out1",
            self.merges[-1],
            "in2",
            label=f"{self.label}_bypass_out",
        )

        for i in reversed(range(n)):
            pipe = self.return_pipes[i]
            self.connections[f"merge_{i + 1}_to_return_{i + 1}"] = Connection(
                self.merges[i],
                "out1",
                pipe,
                "in1",
                label=f"{self.label}_mg{i + 1}_to_return_pipe_{i + 1}",
            )
            if i > 0:
                self.connections[f"return_{i + 1}_to_merge_{i}"] = Connection(
                    pipe,
                    "out1",
                    self.merges[i - 1],
                    "in2",
                    label=f"{self.label}_return_pipe_{i + 1}_to_mg{i}",
                )
            else:
                if self.pump_placement == "return_outlet":
                    self.connections["return_1_to_pump"] = Connection(
                        pipe,
                        "out1",
                        self.pump,
                        "in1",
                        label=f"{self.label}_return_pipe_1_to_pump",
                    )
                    self.connections["branch_out"] = Connection(
                        self.pump,
                        "out1",
                        return_sink,
                        return_sink_port,
                        label=outlet_label or f"{self.label}_pump_to_return_sink",
                    )
                else:
                    self.connections["branch_out"] = Connection(
                        pipe,
                        "out1",
                        return_sink,
                        return_sink_port,
                        label=outlet_label or f"{self.label}_return_pipe_1_to_sink",
                    )

        return tuple(self.connections.values())

    def add_to_network(self, network) -> None:
        """Add all created branch connections to a TESPy network."""
        if not self.connections:
            raise RuntimeError("Call connect_between before add_to_network.")
        network.add_conns(*self.connections.values())

    def set_design(
        self,
        pump_attrs: Optional[Dict[str, float]] = None,
        building_mass_flows: Optional[Sequence[float]] = None,
        native_offdesign: bool = False,
    ) -> None:
        """Apply pipe, pump, building, and bypass design attributes.

        ``native_offdesign=True`` forwards TESPy design/offdesign metadata to
        compatible subcomponents. The branch keeps its existing hydraulic anchors
        for robustness, but pump efficiency and building pressure drops can use
        native characteristic switching where the corresponding design data are
        present.
        """
        if self.pump is not None and pump_attrs:
            attrs = dict(pump_attrs)
            if native_offdesign and "eta_s" in attrs:
                attrs.setdefault("design", ["eta_s"])
                attrs.setdefault("offdesign", ["eta_s_char"])
            self.pump.set_attr(**attrs)

        self._apply_pipe_specs()

        for i, building in enumerate(self.buildings):
            mass_flow = None
            if building_mass_flows is not None:
                mass_flow = building_mass_flows[i]
            building.set_design(mass_flow=mass_flow, native_offdesign=native_offdesign)

        if self.bypass_m is not None:
            self.connections["bypass_in"].set_attr(m=self.bypass_m)
        # Always fix bypass valve dp=0 (no pressure drop across the bypass line).
        # This is required to anchor the bypass outlet pressure in the TESPy equation system;
        # without it, the Valve's free pr parameter leaves one pressure island unconstrained.
        # NOTE: dp=0 is used instead of pr=1 because pr.val_SI is nan before the first solve
        # (unit conversion has not yet run), making the pr_structure_matrix row degenerate.
        # dp=0 uses an absolute pressure difference that is always valid in the structure matrix.
        self.bypass.set_attr(dp=0)

    def _apply_pipe_specs(self) -> None:
        if self.uniform_pipes:
            attrs = self.uniform_pipe_attrs or {}
            for pipe in [*self.supply_pipes, *self.return_pipes]:
                if attrs:
                    pipe.set_attr(**attrs)
            return

        specs = self.pipe_attrs or {}
        for i, pipe in enumerate(self.supply_pipes, start=1):
            attrs = specs.get(f"supply_{i}", {})
            if attrs:
                pipe.set_attr(**attrs)
        for i, pipe in enumerate(self.return_pipes, start=1):
            attrs = specs.get(f"return_{i}", {})
            if attrs:
                pipe.set_attr(**attrs)

    def set_connection_start(self, key: str, m: float, T: float, p: float) -> None:
        """Set starting values for one named branch connection."""
        conn = self.connections[key]
        conn.m.set_val0(m)
        conn.T.set_val0(T)
        conn.p.set_val0(p)

    def all_connections(self) -> Sequence[Connection]:
        """Return branch connections in the order they were created."""
        return tuple(self.connections.values())


__all__ = ["Branch"]
