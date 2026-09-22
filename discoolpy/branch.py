"""Reusable TESPy branch module for district cooling street-level networks.

A :class:`Branch` is one street or feeder segment: sequential supply taps,
building heat exchangers, supply pipes, return pipes, an optional pump, and one
or more terminals at the far end. Hand it a list of
:class:`~discoolpy.building.Building` objects and it creates the TESPy
components and connections for you.

Branching
---------
Real street grids fork, so a branch ends in a list of terminals rather than in
a single bypass valve. A terminal is one of

``EndBypass``
    A valve returning end-of-line flow to the return header. Every dead-end
    street needs this minimum-flow path, and it is the default.
``SatellitePlant``
    A distributed chiller, optionally with a cold store in series, that takes a
    slipstream off the end of the line, cools it, and delivers it into the
    return header. It carries part of the district duty locally, so the main
    plant sees a colder return and a smaller load.
``SubBranch``
    Another :class:`Branch` fed from the end of this one. Two or more of these
    make a street that splits. Each child ends in its own list of terminals, so
    the network is a tree of whatever depth you need.

Every terminal spans the same two hydraulic nodes, the end-of-line supply node
and the end-of-line return node. That is what keeps the pressure-topology
analysis in :mod:`discoolpy.hydraulics` valid on a tree as well as on a
ladder. Where a branch carries more than one terminal, a TESPy
``Splitter``/``Merge`` pair goes in at the fork. Neither component drops
pressure, so the fork adds loops to the pressure graph but no nodes.

A branch with no buildings at all is legal, and useful: it is a trunk link
carrying one supply and one return pipe from its parent to its own fork.

Pipes
-----
Pipes are not assumed adiabatic. Each one carries a
:class:`~discoolpy.thermal.PipeThermal` description, and when that specifies a
conductance the ``UA`` and the ambient temperature go to TESPy, so the solver
works the gain out from the water temperature it solved for at that snapshot.
Couple it that way and a scenario will show supply temperature climbing along
the street, return-side delta-T collapsing, and plant load running above the
sum of the building loads. With ``Q = 0`` pipes you see none of it.

Geometry
--------
A pipe may also carry a ``heading_deg`` bearing, measured counter-clockwise
from east so that ``0`` is +x and ``90`` is +y, alongside its length ``L``. The
solver ignores both. They are there so :mod:`discoolpy.layout` can draw the
network as the street grid it stands for instead of an abstract graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, Iterator, List, Optional, Sequence, Tuple

from tespy.components import Merge, Pipe, Pump, Splitter, Valve
from tespy.connections import Connection

from .building import Building
from .thermal import PipeThermal, lmtd_to_ambient


_VALID_PUMP_PLACEMENTS = {None, "supply_inlet", "return_outlet"}


def _conductance_attr(component) -> str:
    """Return the conductance parameter name for the installed TESPy version.

    TESPy renamed ``kA`` to ``UA`` and warns on the old name. Recent releases
    carry both, so take ``UA`` where it exists and fall back to ``kA``. Beats
    pinning the tool to one TESPy generation.
    """
    params = getattr(component, "parameters", {})
    return "UA" if "UA" in params else "kA"


def _solved(component, name: str) -> Optional[float]:
    """Read one solved scalar off a TESPy component, or ``None`` if it has none.

    TESPy leaves a parameter's ``val`` at nan until the equation that produces
    it has run, and not every heat model produces every parameter. Returning
    ``None`` rather than nan keeps the difference between "not modelled" and
    "modelled as zero" visible in the report, which matters when a scenario
    mixes adiabatic and conducting pipes.
    """
    value = getattr(getattr(component, name, None), "val", None)
    if value is None:
        return None
    value = float(value)
    return None if value != value else value


def _connection_T(component, side: str) -> Optional[float]:
    """Temperature on a component's first inlet or outlet, in degC."""
    conns = getattr(component, side, None) or ()
    if not conns:
        return None
    value = getattr(getattr(conns[0], "T", None), "val", None)
    if value is None:
        return None
    value = float(value)
    return None if value != value else value


# Terminals: what closes the end of a branch.

@dataclass
class Terminal:
    """Base class for whatever closes the end of a branch.

    A terminal always spans the branch's end-of-line supply node and its
    end-of-line return node. Subclasses decide what sits in between.
    """

    label: str = "terminal"
    # Design mass flow in kg/s. None lets whatever the terminal contains decide,
    # which only really means SubBranch, whose flow is the sum of its subtree.
    m: Optional[float] = None
    # Plan-view bearing, degrees CCW from east. Layout plotter only; None lets
    # the layout pick.
    heading_deg: Optional[float] = None
    connections: Dict[str, Connection] = field(default_factory=dict, init=False)
    kind: ClassVar[str] = "terminal"

    def build(self, prefix: str) -> None:
        """Create the TESPy components for this terminal."""
        raise NotImplementedError

    def connect(
        self,
        supply_source,
        supply_port: str,
        return_sink,
        return_port: str,
        prefix: str,
    ) -> Dict[str, Connection]:
        """Create the connections spanning the end-of-line nodes."""
        raise NotImplementedError

    def set_design(self) -> None:
        """Apply design specifications to this terminal."""
        raise NotImplementedError

    # Introspection.
    def all_connections(self) -> Sequence[Connection]:
        return tuple(self.connections.values())

    def sub_branches(self) -> Sequence["Branch"]:
        return ()

    def pressure_spec(self) -> Optional[str]:
        """What fixes this terminal's pressure drop, or ``None`` when it floats."""
        return None

    def release_pressure_spec(self) -> None:
        """Let this terminal's pressure drop become a solved output."""
        raise TypeError(
            f"The pressure drop of terminal {self.label!r} cannot be released; it is "
            "set by physical components rather than by a free specification."
        )

    def inlet_connection(self) -> Optional[Connection]:
        """The connection entering the terminal from the end-of-line supply node."""
        return None

    def outlet_connection(self) -> Optional[Connection]:
        """The connection leaving the terminal into the end-of-line return node."""
        return None

    def start_values(self, m: float, T: float, p: float, dp: float = 0.15) -> None:
        """Seed starting values on this terminal's own connections."""
        inlet, outlet = self.inlet_connection(), self.outlet_connection()
        if inlet is not None:
            inlet.m.set_val0(float(m))
            inlet.T.set_val0(float(T))
            inlet.p.set_val0(float(p))
        if outlet is not None:
            outlet.m.set_val0(float(m))
            outlet.T.set_val0(float(T))
            outlet.p.set_val0(float(p) - float(dp))


@dataclass
class EndBypass(Terminal):
    """End-of-line bypass valve returning flow to the return header.

    ``dp`` is the pressure change across the valve, in the network pressure
    unit. ``0.0`` anchors the end-of-line pressure island, which is usually
    what a single non-branching ladder wants. ``None`` leaves the valve free so
    that every pipe can fix its own drop from geometry instead; the bypass then
    reports the balancing drop, the way a real end-of-line bypass valve does.
    Once a branch forks, ``None`` is the only arrangement that works, because
    each parallel path adds a loop and every loop needs a free element.
    """

    dp: Optional[float] = 0.0
    valve: Valve = field(init=False, default=None)
    kind: ClassVar[str] = "bypass"

    def build(self, prefix: str) -> None:
        self.valve = Valve(f"{prefix} {self.label}")

    def connect(self, supply_source, supply_port, return_sink, return_port, prefix):
        safe = f"{prefix}_{self.label}".replace(" ", "_")
        self.connections = {
            "in": Connection(supply_source, supply_port, self.valve, "in1", label=f"{safe}_in"),
            "out": Connection(self.valve, "out1", return_sink, return_port, label=f"{safe}_out"),
        }
        return self.connections

    def set_design(self) -> None:
        if self.m is not None:
            self.connections["in"].set_attr(m=float(self.m))
        # dp rather than pr=1: pr.val_SI is nan until the first solve runs the
        # unit conversion, and that leaves a degenerate row in the structure
        # matrix. dp is an absolute difference, so it is always well formed.
        if self.dp is not None:
            self.valve.set_attr(dp=float(self.dp))

    def pressure_spec(self) -> Optional[str]:
        return None if self.dp is None else f"dp={self.dp}"

    def release_pressure_spec(self) -> None:
        self.dp = None

    def inlet_connection(self):
        return self.connections.get("in")

    def outlet_connection(self):
        return self.connections.get("out")


@dataclass
class SatellitePlant(Terminal):
    """A distributed chiller (optionally with a cold store) at the end of a line.

    The plant takes a slipstream from the end-of-line supply node, cools it,
    and delivers it into the end-of-line return node. Hydraulically it sits in
    the same slot as a bypass valve, so it provides the minimum-flow path too.
    Thermally it removes real duty from the district, which is why the main
    plant sees a colder return and a smaller evaporator load whenever one is
    running.

    The satellite's evaporator duty stays asserted. The chilled-water loop has
    exactly one free energy variable, the main plant's duty, and a second free
    duty would leave the loop under-determined. So setting the satellite duty
    is a dispatch decision, which is how a distributed plant gets operated
    anyway.

    Parameters
    ----------
    chiller:
        A :class:`~discoolpy.chiller.Chiller` subsystem. Its ``in1``/``out1``
        ports carry the chilled water.
    cooling_tower:
        The satellite's own condenser-water loop. Required, because a chiller
        that cannot reject heat is not a chiller.
    storage:
        Optional :class:`~discoolpy.cold_storage.ColdStorage` placed in series
        downstream of the satellite evaporator.
    """

    chiller: Any = None
    cooling_tower: Any = None
    storage: Any = None
    # Pressure change across the plant's balancing valve, network pressure
    # unit. None (the default) leaves it free, which is what a real balancing
    # valve does and what a fork needs: pin the drop on a plant leg and the
    # parallel path is one specification over.
    balancing_dp: Optional[float] = None
    # How the duty gets decided each snapshot. "fixed" holds the design duty.
    # "proportional" scales the duty it delivers with the district load, which
    # is how a distributed plant is dispatched, and gives a store at the
    # satellite something to level.
    dispatch: str = "fixed"
    # Floor on the proportional duty, as a fraction of design. A chiller told
    # to make 3 kW is a chiller that has tripped.
    min_load_fraction: float = 0.2
    # Design (and maximum) evaporator duty in W. Kept here because
    # Chiller.update_Q_evap overwrites the chiller's own copy.
    design_Q_evap_W: Optional[float] = None
    valve: Valve = field(init=False, default=None)
    kind: ClassVar[str] = "plant"

    def build(self, prefix: str) -> None:
        if self.chiller is None:
            raise ValueError(f"SatellitePlant {self.label!r} needs a Chiller.")
        if self.cooling_tower is None:
            raise ValueError(
                f"SatellitePlant {self.label!r} needs a CoolingTower: a chiller that cannot "
                "reject its condenser heat has no defined operating point."
            )
        if str(self.dispatch).lower() not in {"fixed", "proportional"}:
            raise ValueError(
                f"SatellitePlant {self.label!r}: dispatch must be 'fixed' or 'proportional', "
                f"got {self.dispatch!r}."
            )
        self.dispatch = str(self.dispatch).lower()
        if self.design_Q_evap_W is None:
            self.design_Q_evap_W = float(self.chiller._Q_evap)
        self.valve = Valve(f"{prefix} {self.label} balancing valve")
        if self.storage is not None:
            self.storage.create_hydraulic_element()

    def connect(self, supply_source, supply_port, return_sink, return_port, prefix):
        safe = f"{prefix}_{self.label}".replace(" ", "_")
        conns: Dict[str, Connection] = {
            "in": Connection(supply_source, supply_port, self.valve, "in1", label=f"{safe}_in"),
            "valve_to_chiller": Connection(
                self.valve, "out1", self.chiller, "in1", label=f"{safe}_valve_out"
            ),
        }
        if self.storage is None:
            conns["out"] = Connection(
                self.chiller, "out1", return_sink, return_port, label=f"{safe}_out"
            )
        else:
            hx = self.storage.heat_exchanger
            conns["to_storage"] = Connection(
                self.chiller, "out1", hx, "in1", label=f"{safe}_to_store"
            )
            conns["out"] = Connection(
                hx, "out1", return_sink, return_port, label=f"{safe}_out"
            )
        self.cooling_tower.connect_to_chiller(
            self.chiller,
            condenser_in_port="in2",
            condenser_out_port="out2",
            cond_in_label=f"{safe}_cond_in",
            cond_out_label=f"{safe}_cond_out",
            close_label=f"{safe}_ct_close",
        )
        for i, conn in enumerate(self.cooling_tower.connections):
            conns[f"cond_{i}"] = conn
        self.connections = conns
        return conns

    def set_design(self) -> None:
        if self.m is not None:
            self.connections["in"].set_attr(m=float(self.m))
        if self.balancing_dp is not None:
            self.valve.set_attr(dp=float(self.balancing_dp))
        if self.storage is not None:
            self.storage.set_design(idle_Q_W=0.0)
        self.cooling_tower.set_design()

    def pressure_spec(self) -> Optional[str]:
        # The evaporator's pr1 and the store's pr already fix their own drops;
        # what decides whether the *leg* pins a pressure is the balancing valve.
        return None if self.balancing_dp is None else f"balancing valve dp={self.balancing_dp}"

    def release_pressure_spec(self) -> None:
        self.balancing_dp = None

    def sub_branches(self):
        return ()

    def inlet_connection(self):
        return self.connections.get("in")

    def outlet_connection(self):
        return self.connections.get("out")

    def start_values(self, m: float, T: float, p: float, dp: float = 0.15) -> None:
        super().start_values(m, T, p, dp)
        for name, fraction in (("valve_to_chiller", 0.25), ("to_storage", 0.75)):
            mid = self.connections.get(name)
            if mid is None:
                continue
            mid.m.set_val0(float(m))
            mid.T.set_val0(float(T))
            mid.p.set_val0(float(p) - float(dp) * fraction)
        self.cooling_tower.set_start(m=max(float(m), 1.0), pr_start=0.999)

    def apply_snapshot(self, snapshot) -> None:
        """Track the weather on the satellite's own condenser-water loop."""
        self.cooling_tower.apply_snapshot(snapshot)

    def delivered_duty_W(self, load_fraction: float = 1.0) -> float:
        """Duty the plant should hand the district at this snapshot, in W.

        Under ``dispatch="fixed"`` that is the design duty regardless of the
        weather. Under ``"proportional"`` it follows the district's load, floored
        at :attr:`min_load_fraction` so the machine is never asked for a duty it
        could not hold.
        """
        design = float(self.design_Q_evap_W or 0.0)
        if self.dispatch != "proportional":
            return design
        fraction = min(1.0, max(float(self.min_load_fraction), float(load_fraction)))
        return design * fraction

    def apply_dispatch(self, delivered_W: float, storage_Q_W: float = 0.0) -> float:
        """Set the evaporator duty for one snapshot and return it.

        The store sits downstream of the evaporator, so the district receives
        ``Q_evap - storage_Q``. Asking the plant to deliver ``delivered_W``
        while the store absorbs ``storage_Q_W`` means running the chiller at
        the sum of the two. That is how a store lets a distributed plant run
        flat while its output follows demand.
        """
        duty = max(float(delivered_W) + float(storage_Q_W), 0.0)
        self.chiller.update_Q_evap(duty)
        return duty


@dataclass
class SubBranch(Terminal):
    """A child :class:`Branch` fed from the end of its parent."""

    branch: "Branch" = None
    kind: ClassVar[str] = "branch"

    def __post_init__(self) -> None:
        if self.branch is not None and self.label == "terminal":
            self.label = self.branch.label

    def build(self, prefix: str) -> None:
        if self.branch is None:
            raise ValueError("SubBranch needs a Branch.")

    def connect(self, supply_source, supply_port, return_sink, return_port, prefix):
        self.branch.connect_between(supply_source, supply_port, return_sink, return_port)
        self.connections = {}
        return {}

    def set_design(self) -> None:
        # The child branch is configured through Branch.set_design, which
        # recurses; nothing extra belongs here.
        return None

    def sub_branches(self):
        return (self.branch,)

    def pressure_spec(self) -> Optional[str]:
        return None  # the subtree's own elements carry the specification

    def inlet_connection(self):
        return self.branch.connections.get("branch_in")

    def outlet_connection(self):
        return self.branch.connections.get("branch_out")

    def start_values(self, m: float, T: float, p: float, dp: float = 0.15) -> None:
        # Seeding happens through the child branch's own routine.
        return None


# ---------------------------------------------------------------------------
# Branch
# ---------------------------------------------------------------------------

@dataclass
class Branch:
    """District cooling branch: pipes, taps, buildings, and end terminals.

    Parameters
    ----------
    label:
        Branch label, used as a prefix for automatically created component
        labels. It must be unique across the whole network.
    buildings:
        Ordered list of Building objects along the supply direction. The first
        entry is the first tap after the branch inlet. May be empty, in which
        case the branch is a single supply/return trunk link to its terminals.
    terminals:
        What closes the end of the branch. Defaults to a single
        :class:`EndBypass` built from ``bypass_m`` and ``bypass_dp``.
    bypass_m:
        Design bypass mass flow in kg/s, used only for the default terminal.
    bypass_dp:
        Pressure change across the default bypass valve. See :class:`EndBypass`.
    pump_placement:
        Optional pump location: ``None``, ``"supply_inlet"`` or
        ``"return_outlet"``. Child branches normally use ``None``.
    pump_label:
        Optional explicit pump label.
    uniform_pipes:
        If ``True``, ``uniform_pipe_attrs`` is applied to every supply and return
        pipe. If ``False``, use ``pipe_attrs`` for individual pipes.
    uniform_pipe_attrs:
        TESPy pipe attributes applied to all pipes when ``uniform_pipes=True``.
    pipe_attrs:
        Individual pipe specifications by local pipe key (``supply_1``,
        ``supply_2``, ..., ``return_1``, ...).
    pipe_thermal:
        Optional mapping of local pipe key to
        :class:`~discoolpy.thermal.PipeThermal`. Pipes without an entry stay
        adiabatic.
    pipe_headings:
        Optional mapping of local pipe key to a plan-view bearing in degrees
        counter-clockwise from east. Layout metadata only.
    pipe_lengths:
        Optional mapping of local pipe key to length in metres. Layout metadata;
        the solver reads ``L`` from ``pipe_attrs`` instead, and a pipe modelled
        by pressure ratio has no ``L`` there at all.
    origin:
        Plan-view ``(x, y)`` of the branch inlet in metres. Layout metadata only;
        normally set on the root branch and derived for children.
    heading_deg:
        Default bearing for pipes in this branch that do not name their own.
    """

    label: str
    buildings: Sequence[Building] = field(default_factory=list)
    terminals: Optional[Sequence[Terminal]] = None
    bypass_m: Optional[float] = None
    pump_placement: Optional[str] = "supply_inlet"
    pump_label: Optional[str] = None
    uniform_pipes: bool = False
    uniform_pipe_attrs: Optional[Dict[str, float]] = None
    pipe_attrs: Optional[Dict[str, Dict[str, float]]] = None
    pipe_thermal: Optional[Dict[str, PipeThermal]] = None
    bypass_dp: Optional[float] = 0.0
    pipe_headings: Optional[Dict[str, float]] = None
    pipe_lengths: Optional[Dict[str, float]] = None
    origin: Optional[Tuple[float, float]] = None
    heading_deg: float = 0.0
    pump: Optional[Pump] = field(default=None, init=False)
    supply_pipes: List[Pipe] = field(default_factory=list, init=False)
    return_pipes: List[Pipe] = field(default_factory=list, init=False)
    splitters: List[Splitter] = field(default_factory=list, init=False)
    merges: List[Merge] = field(default_factory=list, init=False)
    end_splitter: Optional[Splitter] = field(default=None, init=False)
    end_merge: Optional[Merge] = field(default=None, init=False)
    connections: Dict[str, Connection] = field(default_factory=dict, init=False)
    #: Design mass flow per building on *this* branch, filled in by the builder
    #: so ``set_design`` can recurse without being handed the whole tree.
    design_building_mass_flows: Optional[List[float]] = field(default=None, init=False)
    #: Pump attributes for *this* branch, likewise filled in by the builder.
    design_pump_attrs: Optional[Dict[str, float]] = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.pump_placement not in _VALID_PUMP_PLACEMENTS:
            raise ValueError(
                f"pump_placement must be one of {_VALID_PUMP_PLACEMENTS}, got {self.pump_placement!r}."
            )
        self.buildings = list(self.buildings)
        if self.terminals is None:
            self.terminals = [
                EndBypass(label="end of line bypass", m=self.bypass_m, dp=self.bypass_dp)
            ]
        self.terminals = list(self.terminals)
        if not self.terminals:
            raise ValueError(
                f"Branch {self.label!r} has no terminals: the end of a branch must return its "
                "flow somewhere. Give it a bypass, a satellite plant, or a sub-branch."
            )
        keys = [t.label for t in self.terminals]
        if len(set(keys)) != len(keys):
            raise ValueError(f"Branch {self.label!r} has duplicate terminal labels: {keys}.")

        n = len(self.buildings)
        prefix = self.label
        # A branch with no buildings still needs one supply and one return pipe:
        # it is the trunk link that carries flow from its parent to its fork.
        n_pipes = n if n > 0 else 1
        self.supply_pipes = [Pipe(f"{prefix} supply_pipe_{i}") for i in range(1, n_pipes + 1)]
        self.return_pipes = [Pipe(f"{prefix} return_pipe_{i}") for i in range(1, n_pipes + 1)]
        self.splitters = [Splitter(f"{prefix} B{i} supply tap") for i in range(1, n + 1)]
        self.merges = [Merge(f"{prefix} B{i} return junction") for i in range(1, n + 1)]

        if len(self.terminals) > 1:
            self.end_splitter = Splitter(f"{prefix} end split", num_out=len(self.terminals))
            self.end_merge = Merge(f"{prefix} end merge", num_in=len(self.terminals))
        for terminal in self.terminals:
            terminal.build(prefix)

        if self.pump_placement is not None:
            self.pump = Pump(self.pump_label or f"{prefix} pump")

    # Topology helpers.

    @property
    def n_buildings(self) -> int:
        return len(self.buildings)

    @property
    def n_pipes_per_side(self) -> int:
        return len(self.supply_pipes)

    @property
    def is_link(self) -> bool:
        """True when the branch carries no buildings, only a trunk pipe pair."""
        return not self.buildings

    def end_supply_port(self) -> Tuple[Any, str]:
        """Component and port feeding the end-of-line terminals."""
        if self.buildings:
            return self.splitters[-1], "out2"
        return self.supply_pipes[-1], "out1"

    def end_return_port(self) -> Tuple[Any, str]:
        """Component and port collecting flow back from the terminals."""
        if self.buildings:
            return self.merges[-1], "in2"
        return self.return_pipes[-1], "in1"

    @property
    def bypass(self) -> Optional[Valve]:
        """The first end-of-line bypass valve, for backward compatibility."""
        for terminal in self.terminals:
            if isinstance(terminal, EndBypass):
                return terminal.valve
        return None

    def sub_branches(self) -> List["Branch"]:
        """Immediate child branches, in terminal order."""
        children: List[Branch] = []
        for terminal in self.terminals:
            children.extend(terminal.sub_branches())
        return children

    def walk(self) -> Iterator["Branch"]:
        """Yield this branch and every descendant, depth-first in terminal order."""
        yield self
        for child in self.sub_branches():
            yield from child.walk()

    def all_buildings(self) -> List[Building]:
        """Every building in this subtree, branch by branch, in supply order."""
        out: List[Building] = []
        for branch in self.walk():
            out.extend(branch.buildings)
        return out

    def satellite_plants(self) -> List[SatellitePlant]:
        """Every satellite plant in this subtree."""
        return [
            terminal
            for branch in self.walk()
            for terminal in branch.terminals
            if isinstance(terminal, SatellitePlant)
        ]

    def qualify(self, key: str) -> str:
        """Namespace a local pipe key for whole-network reporting.

        The root branch keeps bare keys (``supply_1``) so single-branch scenarios
        report exactly what they always did; descendants are prefixed with their
        branch label (``north spur/supply_1``).
        """
        return key

    # Wiring up the connections.

    def connect_between(
        self,
        supply_source,
        supply_source_port: str,
        return_sink,
        return_sink_port: str,
        inlet_label: Optional[str] = None,
        outlet_label: Optional[str] = None,
    ) -> Sequence[Connection]:
        """Create all external and internal TESPy connections for the subtree."""
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

        n = len(self.buildings)
        if n:
            self.connections["supply_1_to_splitter_1"] = Connection(
                self.supply_pipes[0],
                "out1",
                self.splitters[0],
                "in1",
                label=f"{self.label}_supply_pipe_1_to_sp1",
            )

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

        self._connect_terminals()

        for i in reversed(range(self.n_pipes_per_side)):
            pipe = self.return_pipes[i]
            if self.buildings:
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

    def _connect_terminals(self) -> None:
        """Wire the end-of-line fork and every terminal hanging off it."""
        supply_src, supply_port = self.end_supply_port()
        return_sink, return_port = self.end_return_port()

        if self.end_splitter is not None:
            self.connections["end_split_in"] = Connection(
                supply_src, supply_port, self.end_splitter, "in1",
                label=f"{self.label}_end_split_in",
            )
            self.connections["end_merge_out"] = Connection(
                self.end_merge, "out1", return_sink, return_port,
                label=f"{self.label}_end_merge_out",
            )
            supply_src, return_sink = self.end_splitter, self.end_merge

        for index, terminal in enumerate(self.terminals, start=1):
            if self.end_splitter is not None:
                src_port, sink_port = f"out{index}", f"in{index}"
            else:
                src_port, sink_port = supply_port, return_port
            created = terminal.connect(
                supply_src, src_port, return_sink, sink_port, self.label
            )
            for name, conn in created.items():
                self.connections[f"terminal_{index}_{name}"] = conn
            # A lone bypass keeps the historic connection names so pre-branching
            # code and saved scenarios keep working unchanged.
            if len(self.terminals) == 1 and isinstance(terminal, EndBypass):
                self.connections["bypass_in"] = created["in"]
                self.connections["bypass_out"] = created["out"]

    def own_connections(self) -> Sequence[Connection]:
        """This branch's connections, each exactly once.

        A lone end-of-line bypass is registered under both its generic
        ``terminal_1_*`` keys and the historic ``bypass_*`` aliases, so the
        values view contains duplicates that TESPy would reject.
        """
        seen: Dict[int, Connection] = {}
        for conn in self.connections.values():
            seen.setdefault(id(conn), conn)
        return tuple(seen.values())

    def add_to_network(self, network) -> None:
        """Add every connection in this subtree to a TESPy network."""
        if not self.connections:
            raise RuntimeError("Call connect_between before add_to_network.")
        for branch in self.walk():
            network.add_conns(*branch.own_connections())
        for plant in self.satellite_plants():
            network.add_subsystems(plant.chiller)

    def all_connections(self) -> Sequence[Connection]:
        """Every connection in this subtree, in creation order, without duplicates."""
        out: List[Connection] = []
        for branch in self.walk():
            out.extend(branch.own_connections())
        return tuple(out)

    # Design-point specification.

    def set_design(
        self,
        pump_attrs: Optional[Dict[str, float]] = None,
        building_mass_flows: Optional[Sequence[float]] = None,
        native_offdesign: bool = False,
    ) -> None:
        """Apply pipe, pump, building, and terminal design attributes.

        ``building_mass_flows`` covers this branch's own buildings only; child
        branches read theirs from :attr:`Branch.design_building_mass_flows`,
        which :func:`discoolpy.utils.build_system` fills in.

        ``native_offdesign=True`` forwards TESPy design/offdesign metadata to
        compatible subcomponents. The branch keeps its existing hydraulic anchors
        for robustness, but pump efficiency and building pressure drops can use
        native characteristic switching where the corresponding design data are
        present.
        """
        pump_attrs = pump_attrs or self.design_pump_attrs
        if self.pump is not None and pump_attrs:
            attrs = dict(pump_attrs)
            if native_offdesign and "eta_s" in attrs:
                attrs.setdefault("design", ["eta_s"])
                attrs.setdefault("offdesign", ["eta_s_char"])
            self.pump.set_attr(**attrs)

        self._apply_pipe_specs()

        flows = building_mass_flows
        if flows is None:
            flows = getattr(self, "design_building_mass_flows", None)
        for i, building in enumerate(self.buildings):
            mass_flow = None if flows is None else flows[i]
            building.set_design(mass_flow=mass_flow, native_offdesign=native_offdesign)

        for terminal in self.terminals:
            terminal.set_design()

        for child in self.sub_branches():
            child.set_design(native_offdesign=native_offdesign)

    def pipe_items(self):
        """Yield ``(local_pipe_key, Pipe)`` for this branch's pipes, in order."""
        for i, pipe in enumerate(self.supply_pipes, start=1):
            yield f"supply_{i}", pipe
        for i, pipe in enumerate(self.return_pipes, start=1):
            yield f"return_{i}", pipe

    def tree_pipe_items(self, root: bool = True):
        """Yield ``(qualified_key, Pipe)`` across the whole subtree.

        The root branch's keys stay bare (``supply_1``) so single-branch results
        keep their historic column names; descendants are namespaced by branch
        label (``north spur/supply_1``).
        """
        for key, pipe in self.pipe_items():
            yield (key if root else f"{self.label}/{key}"), pipe
        for child in self.sub_branches():
            yield from child.tree_pipe_items(root=False)

    def _apply_pipe_specs(self) -> None:
        if self.uniform_pipes:
            attrs = self.uniform_pipe_attrs or {}
            for pipe in [*self.supply_pipes, *self.return_pipes]:
                if attrs:
                    pipe.set_attr(**attrs)
        else:
            specs = self.pipe_attrs or {}
            for key, pipe in self.pipe_items():
                attrs = specs.get(key, {})
                if attrs:
                    pipe.set_attr(**attrs)
        self._apply_pipe_thermal()

    def _apply_pipe_thermal(self) -> None:
        """Push each pipe's heat-gain model onto its TESPy component."""
        thermal = self.pipe_thermal or {}
        for key, pipe in self.pipe_items():
            spec = thermal.get(key)
            if spec is None or spec.model == "adiabatic":
                continue
            if spec.model == "fixed":
                pipe.set_attr(Q=float(spec.Q_W))
            elif spec.model == "ua":
                ua = spec.resolved_UA()
                if ua <= 0:
                    continue
                if spec.ambient_temperature_degC is None:
                    raise ValueError(
                        f"Pipe {key!r} on branch {self.label!r} uses the 'ua' heat model but has "
                        "no ambient temperature. Set an explicit value or supply one via "
                        "Branch.apply_ambient()."
                    )
                pipe.set_attr(
                    **{
                        _conductance_attr(pipe): ua,
                        "Tamb": float(spec.ambient_temperature_degC),
                    }
                )
            elif spec.model in {"tespy_buried", "tespy_surface"}:
                attrs = dict(spec.native_attrs)
                if spec.ambient_temperature_degC is not None:
                    attrs.setdefault("Tamb", float(spec.ambient_temperature_degC))
                pipe.set_attr(**attrs)
            else:
                raise ValueError(f"Unsupported pipe heat model {spec.model!r} for pipe {key!r}.")

    def apply_ambient(
        self,
        ground_temperature_degC: Optional[float] = None,
        air_temperature_degC: Optional[float] = None,
    ) -> Dict[str, float]:
        """Update every pipe's ambient temperature in the subtree for a new snapshot.

        Each pipe follows its own ``ambient_source``: ``"ground"`` pipes track the
        soil temperature (slow, damped), ``"air"`` pipes track dry-bulb air, and
        ``"fixed"`` pipes keep the value they were configured with. Buried and
        exposed sections of the same network therefore see different driving
        temperatures, which is the realistic case and materially changes how much
        gain shows up during an afternoon peak.
        """
        applied: Dict[str, float] = {}
        for branch in self.walk():
            root = branch is self
            for key, pipe in branch.pipe_items():
                spec = (branch.pipe_thermal or {}).get(key)
                if spec is None or spec.model in {"adiabatic", "fixed"}:
                    continue
                if spec.ambient_source == "ground" and ground_temperature_degC is not None:
                    value = float(ground_temperature_degC)
                elif spec.ambient_source == "air" and air_temperature_degC is not None:
                    value = float(air_temperature_degC)
                else:
                    continue
                spec.ambient_temperature_degC = value
                pipe.set_attr(Tamb=value)
                applied[key if root else f"{branch.label}/{key}"] = value
        return applied

    def apply_snapshot(self, snapshot) -> None:
        """Push a snapshot's weather onto every satellite plant in the subtree."""
        for plant in self.satellite_plants():
            plant.apply_snapshot(snapshot)

    # Reporting.

    def heat_gain_report(self) -> Dict[str, Any]:
        """Per-pipe and aggregate heat gain in W across the subtree, after a solve.

        Every number here is read back off the solved TESPy component, so the
        report describes the equations the network actually balanced on rather
        than a post-hoc estimate. That holds for all four heat models: TESPy
        back-reports ``UA`` and ``lmtd`` as results whenever ``Tamb`` is set, so
        a pipe handed the native buried group reports an effective conductance
        in the same column as one handed a closed-form ``UA``.

        ``per_pipe_W`` keeps its historic meaning. ``per_pipe`` carries the
        detail: the solved duty, the effective conductance and log-mean driving
        difference, the terminal water temperatures, and the heat model that
        produced them.
        """
        per_pipe_W: Dict[str, float] = {}
        per_pipe: Dict[str, Dict[str, Any]] = {}
        supply_total = 0.0
        return_total = 0.0
        ua_total = 0.0
        for branch, key, pipe in self._tree_pipe_entries():
            q = _solved(pipe, "Q")
            q = 0.0 if q is None else q
            spec = (branch.pipe_thermal or {}).get(key.rpartition("/")[2])
            ua = _solved(pipe, "UA")
            if ua is None:
                ua = _solved(pipe, "kA")
            length = None if spec is None else spec.length_m
            detail = {
                "Q_W": q,
                "UA_W_K": ua,
                "UA_per_m_W_mK": (
                    None if ua is None or not length else ua / float(length)
                ),
                "lmtd_K": _solved(pipe, "lmtd"),
                "T_in_degC": _connection_T(pipe, "inl"),
                "T_out_degC": _connection_T(pipe, "outl"),
                "length_m": length,
                "model": "adiabatic" if spec is None else spec.model,
                "ambient_source": None if spec is None else spec.ambient_source,
                "ambient_temperature_degC": (
                    None if spec is None else spec.ambient_temperature_degC
                ),
                "side": "supply" if key.rpartition("/")[2].startswith("supply") else "return",
                "branch": branch.label,
            }
            per_pipe_W[key] = q
            per_pipe[key] = detail
            if ua is not None:
                ua_total += ua
            if detail["side"] == "supply":
                supply_total += q
            else:
                return_total += q
        return {
            "per_pipe_W": per_pipe_W,
            "per_pipe": per_pipe,
            "supply_heat_gain_W": supply_total,
            "return_heat_gain_W": return_total,
            "total_heat_gain_W": supply_total + return_total,
            "network_UA_W_K": ua_total,
        }

    def _tree_pipe_entries(self, root: bool = True):
        """Yield ``(owning_branch, qualified_key, Pipe)`` across the subtree."""
        for key, pipe in self.pipe_items():
            yield self, (key if root else f"{self.label}/{key}"), pipe
        for child in self.sub_branches():
            yield from child._tree_pipe_entries(root=False)

    def satellite_report(self) -> Dict[str, Any]:
        """Solved duty, compressor power and store exchange of every satellite plant.

        ``storage_Q_W`` is the heat the satellite's own store puts back into the
        water downstream of its evaporator, so the duty the satellite actually
        delivers to the district is ``Q_evap_W - storage_Q_W``: while the store
        charges, the satellite runs harder than the district feels.
        """
        per_plant: Dict[str, Dict[str, float]] = {}
        duty = 0.0
        power = 0.0
        store_duty = 0.0
        for plant in self.satellite_plants():
            q = float(plant.chiller.solved_Q_evap_W)
            p = float(plant.chiller.compressor.P.val)
            q_store = 0.0
            if plant.storage is not None:
                value = getattr(getattr(plant.storage.heat_exchanger, "Q", None), "val", None)
                q_store = 0.0 if value is None or value != value else float(value)
            per_plant[plant.label] = {
                "Q_evap_W": q,
                "compressor_power_W": p,
                "cop": q / p if p else float("nan"),
                "heat_rejection_W": float(plant.cooling_tower.heat_rejection),
                "storage_Q_W": q_store,
                "delivered_Q_W": q - q_store,
                "storage_soc": (
                    float("nan") if plant.storage is None else float(plant.storage.soc)
                ),
            }
            duty += q
            power += p
            store_duty += q_store
        return {
            "per_plant": per_plant,
            "total_Q_evap_W": duty,
            "total_compressor_power_W": power,
            "total_storage_Q_W": store_duty,
            "total_delivered_Q_W": duty - store_duty,
        }

    def pressure_feasibility(self, tolerance_bar: float = 1e-6) -> Dict[str, Any]:
        """Flag elements in the subtree whose solved pressure *rises* along the flow.

        Building mass flows are specified, so TESPy will happily hand back a
        negative pressure drop across a building leg or a terminal. Nobody
        asked the solver whether the pump could push that flow, only what
        pressures balance the equations. A negative drop means the pump is
        undersized: round-trip pipe friction to that point has exceeded the
        available head, and the far building would in reality be starved.

        Skip the check and the only symptom is a run of TESPy "Invalid value
        for pr" warnings in the log, easy to write off as noise.
        """
        offenders: Dict[str, float] = {}
        for branch in self.walk():
            for building in branch.buildings:
                if building.inlet is None or building.outlet is None:
                    continue
                dp = building.inlet.p.val - building.outlet.p.val
                if dp == dp and dp < -tolerance_bar:
                    offenders[building.label] = dp
            for terminal in branch.terminals:
                inlet, outlet = terminal.inlet_connection(), terminal.outlet_connection()
                if inlet is None or outlet is None:
                    continue
                dp = inlet.p.val - outlet.p.val
                if dp == dp and dp < -tolerance_bar:
                    offenders[f"{branch.label} / {terminal.label}"] = dp
        worst = min(offenders.values()) if offenders else 0.0
        return {
            "feasible": not offenders,
            "negative_pressure_drops_bar": offenders,
            "shortfall_bar": -worst,
            "message": (
                "Branch pressure specification is feasible: every element drops pressure "
                "along the flow direction."
                if not offenders
                else (
                    "Infeasible hydraulics: pressure RISES across "
                    + ", ".join(f"{k} ({v:+.3f} bar)" for k, v in offenders.items())
                    + f". The pump is short of head by about {-worst:.3f} bar; at design flow "
                    "the far end of the branch cannot actually be served. Raise the pump head "
                    "(branch.pump_pressure_ratio or design.pump_power_W), increase the pipe "
                    "diameters, or shorten the index run."
                )
            ),
        }

    def estimated_heat_gain_W(self, T_supply: float, T_return: float) -> float:
        """Solver-independent estimate of total pipe gain in the subtree."""
        total = 0.0
        for branch in self.walk():
            for key, _ in branch.pipe_items():
                spec = (branch.pipe_thermal or {}).get(key)
                if spec is None:
                    continue
                t = T_supply if key.startswith("supply") else T_return
                total += spec.heat_gain_W(t, t, spec.ambient_temperature_degC)
        return total

    def set_connection_start(self, key: str, m: float, T: float, p: float) -> None:
        """Set starting values for one named branch connection."""
        conn = self.connections[key]
        conn.m.set_val0(m)
        conn.T.set_val0(T)
        conn.p.set_val0(p)


__all__ = [
    "Branch",
    "Terminal",
    "EndBypass",
    "SatellitePlant",
    "SubBranch",
    "lmtd_to_ambient",
]
