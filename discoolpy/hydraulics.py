"""Structural checking of a network's pressure and energy specification.

A DisCoolPy branch is a hydraulic ladder: supply header with taps, one
building leg per tap, one or more end-of-line terminals, return header. Ladders
contain loops, and in a loop you can fix the pressure drop of every element but
one. Whatever is left over takes the drop the loop closure gives it.

Get that wrong and TESPy raises ``TESPyNetworkError`` with fifteen variable
names, eighteen equation names, and no hint about which specification to
remove. Over-determined pressure is easy to write by accident and miserable to
diagnose from the solver's output.

So this module builds the pressure topology as a graph and runs union-find over
it to find the first redundant specification, then says so in words: "building
3.pr and the bypass both fix the drop between the same two nodes, drop one of
them".

Branching networks
------------------
A branch may end in several terminals and a terminal may be another branch, so
the topology is really a tree of ladders. Every terminal spans the same two
nodes, its branch's end-of-line supply node and end-of-line return node. A fork
with *k* terminals therefore puts *k* paths in parallel between one node pair
and adds *k - 1* loops. That is why a network which solves happily as a single
street stops the moment it forks with both end bypasses pinned at ``dp = 0``.
Two pinned parallel paths between the same nodes is one specification too many.

:func:`analyse_network_topology` walks the tree directly.
:func:`analyse_pressure_topology` is a thin wrapper over it for the
single-branch case.

Node naming
-----------
``S0``            root branch inlet (anchored externally by the inlet spec + pump)
``<b>:S1..Sn``    supply node at tap *i* of branch ``b`` (splitter *i*: all three
                  ports share a pressure)
``<b>:R1..Rn``    return node at junction *i* of branch ``b``
``R0``            root branch outlet

``S0`` and ``R0`` are **one** node for this analysis whenever the branch is
closed through the plant (the normal case): the cycle closer plus the chiller
evaporator's ``pr1`` tie the branch outlet pressure to the branch inlet
pressure, so the ladder has ``n + 1`` loops rather than ``n``. Missing that
external tie is what makes the specification look consistent on paper while
TESPy still rejects it.

A child branch does not get its own ``S0``/``R0``: it starts at its parent's
end-of-line nodes, because the fork's ``Splitter``/``Merge`` pair drops no
pressure and therefore adds no node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = [
    "PressureEdge",
    "PressureTopologyReport",
    "BranchTopology",
    "TerminalTopology",
    "build_pressure_edges",
    "build_network_pressure_edges",
    "analyse_pressure_topology",
    "analyse_network_topology",
    "validate_pressure_topology",
    "validate_network_topology",
    "suggest_pressure_specification",
    "resolve_pressure_specification",
    "resolve_network_pressure_specification",
    "validate_thermal_degrees_of_freedom",
]


def validate_thermal_degrees_of_freedom(
    pipe_keys: Sequence[str],
    pipes_with_energy_spec: Sequence[str],
    plant_control: str,
) -> None:
    """Check the chilled-water loop has exactly one free energy degree of freedom.

    Every pipe in the network needs exactly one energy specification: ``Q``, or
    ``UA`` with an ambient temperature, or one of TESPy's buried and surface
    groups. The closed chilled-water loop then needs exactly one unspecified
    energy variable to absorb the loop balance.

    Under ``plant_control="supply_temperature"`` that free variable is the main
    chiller's evaporator duty, so every pipe has to be specified. Satellite
    plants keep their duty asserted and add no free variable of their own;
    release a second duty and the loop goes under-determined. Under
    ``"evaporator_duty"`` the main duty is asserted instead, so exactly one
    pipe must be left unspecified.

    Leaving a pipe unspecified by accident is worth catching. It becomes a
    hidden slack variable, and any mismatch between the asserted evaporator
    duty and the real loads lands in it as a fictitious pipe heat duty, with
    nothing in the results to say so.
    """
    specified = set(pipes_with_energy_spec)
    missing = [key for key in pipe_keys if key not in specified]
    mode = str(plant_control).lower()

    if mode == "supply_temperature":
        if missing:
            raise ValueError(
                "Chilled-water loop is under-specified: pipe(s) "
                + ", ".join(repr(k) for k in missing)
                + " have no energy specification, and the chiller duty is already free under "
                "plant_control='supply_temperature'. Give every pipe a heat model "
                "(heat_model: adiabatic sets Q = 0)."
            )
    elif mode == "evaporator_duty":
        if len(missing) != 1:
            raise ValueError(
                f"plant_control='evaporator_duty' asserts the chiller duty, so exactly one pipe "
                f"must be left without an energy specification to close the loop; "
                f"{len(missing)} are ({', '.join(repr(k) for k in missing) or 'none'}). "
                "Switch to plant_control='supply_temperature' and specify every pipe. The "
                "plant duty then becomes a solved output instead of a hidden slack."
            )
    else:
        raise ValueError("plant_control must be 'supply_temperature' or 'evaporator_duty'.")


@dataclass(frozen=True)
class PressureEdge:
    """One element that can fix (or leave free) the pressure drop between two nodes."""

    key: str
    node_a: str
    node_b: str
    specified: bool
    spec: str  # human-readable description of what fixes it, or "free"


# ---------------------------------------------------------------------------
# A solver-free description of the network's pressure topology
# ---------------------------------------------------------------------------

@dataclass
class TerminalTopology:
    """One end-of-line terminal, as far as the pressure graph is concerned."""

    key: str
    kind: str = "bypass"          # "bypass" | "plant" | "branch"
    specified: bool = True
    spec: str = "dp=0"
    #: True when this terminal's specification may be released to repair a loop.
    #: A bypass valve may (its drop is set by a balancing valve in reality); a
    #: satellite plant may not (its drop comes from physical components).
    releasable: bool = True
    child: Optional["BranchTopology"] = None


@dataclass
class BranchTopology:
    """One branch of the tree: its buildings, its pipes and its terminals."""

    label: str = "branch"
    building_pr: List[Optional[float]] = field(default_factory=list)
    pipe_attrs: Dict[str, Mapping[str, Any]] = field(default_factory=dict)
    terminals: List[TerminalTopology] = field(default_factory=list)

    @property
    def n_buildings(self) -> int:
        return len(self.building_pr)

    @property
    def n_pipes_per_side(self) -> int:
        # A branch with no buildings is a trunk link: one supply, one return pipe.
        return self.n_buildings or 1

    def walk(self):
        yield self
        for terminal in self.terminals:
            if terminal.child is not None:
                yield from terminal.child.walk()


def _pipe_spec_label(attrs: Mapping[str, Any]) -> Optional[str]:
    """Return what fixes this pipe's pressure drop, or None when it is free."""
    if attrs.get("pr") is not None:
        return "pr"
    if attrs.get("dp") is not None:
        return "dp"
    if attrs.get("zeta") is not None:
        return "zeta"
    if all(attrs.get(k) is not None for k in ("L", "D", "ks")):
        return "darcy_group (L+D+ks)"
    return None


def build_network_pressure_edges(root: BranchTopology) -> List[PressureEdge]:
    """Describe every pressure-carrying element of a (possibly branching) network."""
    edges: List[PressureEdge] = []
    _emit_branch_edges(root, "S0", "R0", edges, is_root=True)
    return edges


def _emit_branch_edges(
    branch: BranchTopology,
    supply_in: str,
    return_out: str,
    edges: List[PressureEdge],
    is_root: bool,
) -> None:
    n = branch.n_buildings
    n_pipes = branch.n_pipes_per_side
    node_prefix = "" if is_root else f"{branch.label}:"
    key_prefix = "" if is_root else f"{branch.label}/"

    supply_nodes = [f"{node_prefix}S{i}" for i in range(1, n_pipes + 1)]
    return_nodes = [f"{node_prefix}R{i}" for i in range(1, n_pipes + 1)]

    previous = supply_in
    for i in range(1, n_pipes + 1):
        key = f"supply_{i}"
        spec = _pipe_spec_label(branch.pipe_attrs.get(key, {}) or {})
        edges.append(
            PressureEdge(
                f"{key_prefix}{key}", previous, supply_nodes[i - 1], spec is not None, spec or "free"
            )
        )
        previous = supply_nodes[i - 1]

    for i in range(1, n + 1):
        pr = branch.building_pr[i - 1]
        edges.append(
            PressureEdge(
                f"{key_prefix}building_{i}",
                supply_nodes[i - 1],
                return_nodes[i - 1],
                pr is not None,
                "pr" if pr is not None else "free",
            )
        )

    supply_end, return_end = supply_nodes[-1], return_nodes[-1]
    for terminal in branch.terminals:
        if terminal.child is not None:
            _emit_branch_edges(terminal.child, supply_end, return_end, edges, is_root=False)
        else:
            edges.append(
                PressureEdge(
                    f"{key_prefix}{terminal.key}",
                    supply_end,
                    return_end,
                    terminal.specified,
                    terminal.spec if terminal.specified else "free",
                )
            )

    for i in range(1, n_pipes + 1):
        key = f"return_{i}"
        spec = _pipe_spec_label(branch.pipe_attrs.get(key, {}) or {})
        downstream = return_nodes[i - 2] if i > 1 else return_out
        edges.append(
            PressureEdge(
                f"{key_prefix}{key}", return_nodes[i - 1], downstream, spec is not None, spec or "free"
            )
        )


def build_pressure_edges(
    n_buildings: int,
    building_pr: Sequence[Optional[float]],
    pipe_attrs: Mapping[str, Mapping[str, Any]],
    bypass_fixed: bool = True,
) -> List[PressureEdge]:
    """Describe every pressure-carrying element of a single ladder branch."""
    return build_network_pressure_edges(
        single_branch_topology(n_buildings, building_pr, pipe_attrs, bypass_fixed)
    )


def single_branch_topology(
    n_buildings: int,
    building_pr: Sequence[Optional[float]],
    pipe_attrs: Mapping[str, Mapping[str, Any]],
    bypass_fixed: bool = True,
) -> BranchTopology:
    """Build the topology description of one non-branching ladder."""
    if n_buildings < 1:
        raise ValueError("A branch needs at least one building.")
    if len(building_pr) != n_buildings:
        raise ValueError("building_pr must have one entry per building.")
    return BranchTopology(
        label="branch",
        building_pr=list(building_pr),
        pipe_attrs={str(k): dict(v or {}) for k, v in (pipe_attrs or {}).items()},
        terminals=[
            TerminalTopology(
                key="bypass",
                kind="bypass",
                specified=bool(bypass_fixed),
                spec="dp=0" if bypass_fixed else "free",
            )
        ],
    )


@dataclass
class PressureTopologyReport:
    """Outcome of the structural check."""

    n_buildings: int
    n_nodes: int
    n_edges: int
    n_loops: int
    n_specified: int
    n_required: int
    redundant: List[Tuple[str, List[str]]]  # (edge key, the already-connected path it closes)
    unanchored_nodes: List[str]
    edges: List[PressureEdge]
    n_branches: int = 1
    #: Branches that end in more than one terminal. Each fork puts its terminals
    #: in parallel between one pair of nodes, which is the usual reason a
    #: specification that worked as a single street stops working.
    n_forks: int = 0

    @property
    def ok(self) -> bool:
        return not self.redundant and not self.unanchored_nodes

    def message(self) -> str:
        scope = (
            f"{self.n_branches} branches"
            if self.n_branches > 1
            else "branch"
        )
        if self.ok:
            return (
                f"Network pressure specification is consistent: {self.n_specified} specified "
                f"element(s) across {scope} for {self.n_nodes} unknown pressure node(s), "
                f"{self.n_loops} loop(s) left free."
            )
        lines: List[str] = []
        if self.redundant:
            lines.append(
                f"Over-determined hydraulics: {len(self.redundant)} redundant pressure "
                f"specification(s). The network ({scope}) has {self.n_loops} independent "
                f"hydraulic loop(s), so exactly {self.n_required} of the {self.n_edges} elements "
                f"may fix a pressure drop; {self.n_specified} do."
            )
            for key, cycle in self.redundant:
                spec = next(e.spec for e in self.edges if e.key == key)
                lines.append(
                    f"  - '{key}' ({spec}) closes a loop whose drop is already fixed by: "
                    + ", ".join(cycle)
                )
            lines.append(
                "  Fix: remove the pressure specification from one element in each loop listed "
                "above (set the building's 'pr' to null, drop 'pr'/'L,D,ks' from the pipe, or "
                "set a terminal's 'dp' to null so its balancing valve reports the drop). Its "
                "pressure drop then becomes a solved output."
            )
            if self.n_forks:
                lines.append(
                    f"  Note: this network forks {self.n_forks} time(s), and a fork puts its "
                    "terminals in parallel between the same two nodes. Parallel paths that are "
                    "already anchored by their pipes cannot also pin a terminal drop. Free "
                    "the bypass valves ('dp: null') and let them balance."
                )
        if self.unanchored_nodes:
            lines.append(
                "Under-determined hydraulics: no specified path reaches "
                + ", ".join(self.unanchored_nodes)
                + ". Add a pressure specification on an element leading to those nodes."
            )
        return "\n".join(lines)


def _analyse_edges(
    edges: Sequence[PressureEdge],
    n_buildings: int,
    n_branches: int,
    plant_closed_loop: bool,
    n_forks: int = 0,
) -> PressureTopologyReport:
    nodes: Set[str] = set()
    for edge in edges:
        nodes.add(edge.node_a)
        nodes.add(edge.node_b)

    anchored = {"S0", "R0"} if plant_closed_loop else {"S0"}
    unknown_nodes = sorted(nodes - anchored)
    # Merging S0 and R0 removes one node from the graph, hence one more loop.
    effective_nodes = len(nodes) - (1 if plant_closed_loop else 0)
    n_loops = len(edges) - effective_nodes + 1

    parent: Dict[str, str] = {node: node for node in nodes}
    if plant_closed_loop:
        parent["R0"] = "S0"
    # Track how each node was reached so a redundancy can be reported as a path.
    reached_via: Dict[str, Tuple[str, str]] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def specified_route(node: str) -> List[str]:
        """Names of the already-specified elements linking ``node`` back to its root."""
        route: List[str] = []
        seen = {node}
        while node in reached_via:
            edge_key, other = reached_via[node]
            route.append(edge_key)
            if other in seen:
                break
            seen.add(other)
            node = other
        return route

    redundant: List[Tuple[str, List[str]]] = []
    for edge in edges:
        if not edge.specified:
            continue
        ra, rb = find(edge.node_a), find(edge.node_b)
        if ra == rb:
            route_a = specified_route(edge.node_a)
            route_b = specified_route(edge.node_b)
            # The loop is everything on one route that is not shared with the other.
            shared = set(route_a) & set(route_b)
            loop = [k for k in route_a + route_b if k not in shared]
            redundant.append((edge.key, loop or sorted(shared)))
            continue
        parent[ra] = rb
        if edge.node_b not in reached_via and edge.node_b not in anchored:
            reached_via[edge.node_b] = (edge.key, edge.node_a)
        elif edge.node_a not in reached_via and edge.node_a not in anchored:
            reached_via[edge.node_a] = (edge.key, edge.node_b)

    root_s0 = find("S0")
    unanchored = [node for node in unknown_nodes if find(node) != root_s0]

    return PressureTopologyReport(
        n_buildings=n_buildings,
        n_nodes=len(unknown_nodes),
        n_edges=len(edges),
        n_loops=n_loops,
        n_specified=sum(1 for e in edges if e.specified),
        n_required=len(unknown_nodes),
        redundant=redundant,
        unanchored_nodes=unanchored,
        edges=list(edges),
        n_branches=n_branches,
        n_forks=n_forks,
    )


def analyse_network_topology(
    root: BranchTopology,
    plant_closed_loop: bool = True,
) -> PressureTopologyReport:
    """Check a whole branching network's pressure specification without TESPy."""
    branches = list(root.walk())
    edges = build_network_pressure_edges(root)
    n_buildings = sum(b.n_buildings for b in branches)
    n_forks = sum(1 for b in branches if len(b.terminals) > 1)
    return _analyse_edges(
        edges, n_buildings, len(branches), plant_closed_loop, n_forks=n_forks
    )


def analyse_pressure_topology(
    n_buildings: int,
    building_pr: Sequence[Optional[float]],
    pipe_attrs: Mapping[str, Mapping[str, Any]],
    bypass_fixed: bool = True,
    plant_closed_loop: bool = True,
) -> PressureTopologyReport:
    """Check a single ladder branch's pressure specification without touching TESPy.

    ``plant_closed_loop`` (the default) accounts for the chilled-water loop being
    closed through the cycle closer and the chiller evaporator, which anchors
    the branch outlet pressure to the branch inlet pressure. Set it False only
    for an open-ended branch whose outlet pressure is genuinely free.
    """
    topology = single_branch_topology(n_buildings, building_pr, pipe_attrs, bypass_fixed)
    return analyse_network_topology(topology, plant_closed_loop=plant_closed_loop)


def validate_network_topology(
    root: BranchTopology,
    strict: bool = True,
    plant_closed_loop: bool = True,
) -> PressureTopologyReport:
    """Raise (or warn) with an actionable message when the network is ill-posed."""
    report = analyse_network_topology(root, plant_closed_loop=plant_closed_loop)
    if not report.ok:
        if strict:
            raise ValueError(report.message())
        import warnings

        warnings.warn(report.message(), stacklevel=2)
    return report


def validate_pressure_topology(
    n_buildings: int,
    building_pr: Sequence[Optional[float]],
    pipe_attrs: Mapping[str, Mapping[str, Any]],
    bypass_fixed: bool = True,
    strict: bool = True,
) -> PressureTopologyReport:
    """Raise (or warn) with an actionable message when the branch is ill-posed."""
    report = analyse_pressure_topology(n_buildings, building_pr, pipe_attrs, bypass_fixed)
    if not report.ok:
        if strict:
            raise ValueError(report.message())
        import warnings

        warnings.warn(report.message(), stacklevel=2)
    return report


def suggest_pressure_specification(n_buildings: int, bypass_fixed: bool = True) -> Dict[str, Any]:
    """Return one valid, physically sensible pressure specification for a ladder branch.

    The recommended convention is **"pipes fix the hydraulics, buildings absorb
    the residual"**: every pipe whose geometry is known fixes its own drop, and
    each building leg is left free so its solved pressure drop is exactly what a
    real balancing/control valve at that building would have to provide. With a
    closed plant loop this needs ``2n`` fixed elements out of ``3n + 1``, leaving
    ``n + 1`` free.

    When ``bypass_fixed`` (the ``EndBypass`` default of ``dp = 0``), ``supply_1``
    is left free so the pump-outlet pressure sets the header. When the bypass is
    free instead, every pipe including ``supply_1`` can be fixed from geometry
    and the bypass valve reports the balancing drop, which is the arrangement
    closest to a real end-of-line bypass, and the only one that carries over to
    a branching network, where the terminals of a fork sit in parallel between
    the same two nodes.
    """
    required = 2 * n_buildings
    if bypass_fixed:
        fixed = (
            [f"supply_{i}" for i in range(2, n_buildings + 1)]
            + [f"return_{i}" for i in range(1, n_buildings + 1)]
            + ["bypass"]
        )
        free = ["supply_1"] + [f"building_{i}" for i in range(1, n_buildings + 1)]
    else:
        fixed = [f"supply_{i}" for i in range(1, n_buildings + 1)] + [
            f"return_{i}" for i in range(1, n_buildings + 1)
        ]
        free = ["bypass"] + [f"building_{i}" for i in range(1, n_buildings + 1)]
    return {
        "n_required": required,
        "fixed": fixed,
        "free": free,
        "note": (
            "Fix the listed elements (pipe 'pr' or 'L,D,ks'); leave the listed elements' "
            "pressure drop as a solved output (building 'pr: null')."
        ),
    }


def resolve_network_pressure_specification(
    root: BranchTopology,
    plant_closed_loop: bool = True,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Free the minimum number of elements needed to make the network well-posed.

    Mutates ``root`` in place and returns ``(actions, notes)``. ``actions`` names
    what was released so the caller can apply the same change to the real
    objects: each entry is ``{"branch": label, "target": "building", "index": i}``
    or ``{"branch": label, "target": "terminal", "key": k}``.

    Only building legs and bypass valves get relaxed, never pipe geometry. A
    balancing valve sets the drop on both in reality, so dropping the
    specification costs no physics. Over-specified pipes are reported rather
    than quietly repaired, because conflicting pipe geometry means the scenario
    itself is wrong.
    """
    actions: List[Dict[str, Any]] = []
    notes: List[str] = []

    if analyse_network_topology(root, plant_closed_loop).unanchored_nodes:
        # Freeing more elements can only make an under-specified network worse.
        return actions, notes

    branches = {b.label: b for b in root.walk()}
    root_label = root.label

    def locate(edge_key: str) -> Optional[Tuple[BranchTopology, str, Any]]:
        """Map an edge key back to the branch element it came from."""
        branch_label, sep, local = edge_key.rpartition("/")
        if not sep:
            branch, local = root, edge_key
        else:
            branch = branches.get(branch_label)
            if branch is None:
                return None
        if local.startswith("building_"):
            index = int(local.split("_")[1]) - 1
            if 0 <= index < branch.n_buildings and branch.building_pr[index] is not None:
                return branch, "building", index
            return None
        for terminal in branch.terminals:
            if terminal.key == local and terminal.specified and terminal.releasable:
                return branch, "terminal", terminal
        return None

    def release(branch: BranchTopology, target: str, handle: Any, reason: str) -> None:
        where = "" if branch.label == root_label else f" on branch '{branch.label}'"
        if target == "building":
            old = branch.building_pr[handle]
            branch.building_pr[handle] = None
            notes.append(
                f"Released building_{handle + 1} pressure ratio (was {old}){where} to resolve "
                f"{reason}; its pressure drop is now a solved output."
            )
            actions.append({"branch": branch.label, "target": "building", "index": handle})
        else:
            handle.specified = False
            handle.spec = "free"
            notes.append(
                f"Released terminal '{handle.key}' pressure specification{where} to resolve "
                f"{reason}; its balancing valve now reports the drop the loop needs."
            )
            actions.append({"branch": branch.label, "target": "terminal", "key": handle.key})

    total_elements = sum(b.n_buildings + len(b.terminals) + 2 * b.n_pipes_per_side
                         for b in root.walk())
    for _ in range(total_elements + 1):
        report = analyse_network_topology(root, plant_closed_loop)
        if not report.redundant:
            break
        redundant_keys = [key for key, _ in report.redundant]
        loop_keys = [k for _, cycle in report.redundant for k in cycle]

        def pick(pool, prefix):
            for key in pool:
                local = key.rpartition("/")[2]
                if prefix is not None and not local.startswith(prefix):
                    continue
                found = locate(key)
                if found is not None:
                    return found
            return None

        # Prefer a building leg (a balancing valve nobody sizes by hand), then a
        # bypass valve, and look at the redundant element itself before reaching
        # into the rest of the loop it closes.
        candidate = None
        for pool in (redundant_keys, loop_keys):
            for prefix in ("building_", None):
                candidate = pick(pool, prefix)
                if candidate is not None:
                    break
            if candidate is not None:
                break
        if candidate is None:
            for branch in root.walk():
                for index in range(branch.n_buildings - 1, -1, -1):
                    if branch.building_pr[index] is not None:
                        candidate = (branch, "building", index)
                        break
                if candidate is not None:
                    break
        if candidate is None:
            break
        release(*candidate, reason="an over-determined hydraulic loop")

    return actions, notes


def resolve_pressure_specification(
    n_buildings: int,
    building_pr: Sequence[Optional[float]],
    pipe_attrs: Mapping[str, Mapping[str, Any]],
    bypass_fixed: bool = True,
    plant_closed_loop: bool = True,
) -> Tuple[List[Optional[float]], List[str]]:
    """Free the minimum number of building legs needed to make a ladder well-posed.

    Returns ``(building_pr, notes)``. Only building pressure ratios are relaxed,
    never pipe geometry or the bypass: a building leg's drop is set by a
    balancing valve in reality, so dropping it is the one relaxation that costs
    no physics.
    """
    topology = single_branch_topology(n_buildings, building_pr, pipe_attrs, bypass_fixed)
    # The legacy behaviour never touched the bypass, only building legs.
    for terminal in topology.terminals:
        terminal.releasable = False
    _, notes = resolve_network_pressure_specification(topology, plant_closed_loop)
    return topology.building_pr, notes
