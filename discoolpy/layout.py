"""Plan-view layout of a district cooling network.

A DisCoolPy network is a tree of streets, and a tree of streets has a shape.
This module recovers that shape from geometry the scenario already carries,
each pipe's length and its ``heading_deg`` bearing, and draws it:

===================  ==========================
Chiller / plant      square
Cooling tower        octagon
Cold storage         circle
Building             triangle
Split / merge        point
Pump                 diamond
Pipe                 line (supply and return drawn as a parallel pair)
===================  ==========================

The aim is a drawing you can hold against a site plan, rather than a graph
drawing. Distances are in metres on equal-aspect axes, so a 600 m main really
is twice the length of a 300 m spur, and a spur that turns north turns north.

Hand it the system and it draws::

    from discoolpy import build_system, load_yaml_config, plot_network

    system = build_system(load_yaml_config("configs/config_branching_grid.yaml"))
    plot_network(system, save_path="layout.png")

Nothing here needs a solved network. The layout belongs to the scenario, so
draw it before the first solve, while a mislabelled branch or a spur pointing
the wrong way is still cheap to fix.

Missing geometry
----------------
A scenario that never mentions ``L`` or ``heading_deg`` still draws. Unknown
lengths fall back to a nominal segment, and terminals with no bearing of their
own fan out evenly around their parent's heading, so a fork with two children
sends one left and one right. That gives you a schematic rather than a survey.
:attr:`NetworkLayout.geometry_is_complete` tells you which one you have.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .branch import Branch, EndBypass, SatellitePlant, SubBranch, Terminal

__all__ = [
    "LayoutNode",
    "LayoutEdge",
    "NetworkLayout",
    "NODE_MARKERS",
    "compute_layout",
    "plot_network",
]

#: Marker for each kind of node, as required by the layout convention.
NODE_MARKERS: Dict[str, str] = {
    "chiller": "s",          # square
    "cooling_tower": "8",    # octagon
    "storage": "o",          # circle
    "building": "^",         # triangle
    "junction": ".",         # point
    "pump": "D",             # diamond
}

_NODE_STYLE: Dict[str, Dict[str, Any]] = {
    "chiller": {"size": 190, "color": "#1f4e79", "edge": "#0d2b45", "z": 5},
    "cooling_tower": {"size": 170, "color": "#4e8ab8", "edge": "#0d2b45", "z": 5},
    "storage": {"size": 150, "color": "#7fbfd4", "edge": "#0d2b45", "z": 5},
    "building": {"size": 130, "color": "#d98d3f", "edge": "#7a4a12", "z": 4},
    "junction": {"size": 45, "color": "#444444", "edge": "#444444", "z": 6},
    "pump": {"size": 70, "color": "#6a51a3", "edge": "#3f2f63", "z": 5},
}

_EDGE_STYLE: Dict[str, Dict[str, Any]] = {
    "supply": {"color": "#2d6ca2", "width": 2.0, "style": "-"},
    "return": {"color": "#c0562a", "width": 2.0, "style": "-"},
    "service": {"color": "#8a8a8a", "width": 1.2, "style": "--"},
}

#: Label offset (dx, dy in label-offset units) and alignment, per node kind.
_LABEL_PLACEMENT: Dict[str, Tuple[float, float, str, str]] = {
    "chiller": (-0.7, -0.7, "right", "top"),
    "cooling_tower": (0.0, 1.0, "center", "bottom"),
    "storage": (0.0, -1.0, "center", "top"),
    "building": (0.0, 1.0, "center", "bottom"),
    "pump": (0.0, -1.0, "center", "top"),
    "junction": (0.0, 1.0, "center", "bottom"),
}

#: Used for any pipe whose scenario does not give a length.
DEFAULT_SEGMENT_M = 150.0


@dataclass
class LayoutNode:
    """One drawable component at a plan-view position."""

    key: str
    kind: str
    label: str
    x: float
    y: float
    branch: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def marker(self) -> str:
        return NODE_MARKERS.get(self.kind, "o")


@dataclass
class LayoutEdge:
    """One drawable connection between two plan-view positions."""

    key: str
    kind: str                 # "supply" | "return" | "service"
    x0: float
    y0: float
    x1: float
    y1: float
    label: str = ""
    branch: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def length_m(self) -> float:
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)


@dataclass
class NetworkLayout:
    """Everything needed to draw the network, with no matplotlib involved."""

    nodes: List[LayoutNode] = field(default_factory=list)
    edges: List[LayoutEdge] = field(default_factory=list)
    #: Pipe keys the scenario gave no length for, so the drawing used a nominal one.
    assumed_lengths: List[str] = field(default_factory=list)
    #: Pipe keys the scenario gave no bearing for.
    assumed_headings: List[str] = field(default_factory=list)

    @property
    def geometry_is_complete(self) -> bool:
        """True when every pipe drawn came from a real length and bearing."""
        return not self.assumed_lengths and not self.assumed_headings

    def nodes_of(self, kind: str) -> List[LayoutNode]:
        return [n for n in self.nodes if n.kind == kind]

    def bounds(self) -> Tuple[float, float, float, float]:
        """``(xmin, ymin, xmax, ymax)`` over every node and edge endpoint."""
        xs = [n.x for n in self.nodes] + [e.x0 for e in self.edges] + [e.x1 for e in self.edges]
        ys = [n.y for n in self.nodes] + [e.y0 for e in self.edges] + [e.y1 for e in self.edges]
        if not xs:
            return (0.0, 0.0, 1.0, 1.0)
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def extent_m(self) -> float:
        xmin, ymin, xmax, ymax = self.bounds()
        return max(xmax - xmin, ymax - ymin, 1.0)

    def to_records(self) -> Dict[str, List[Dict[str, Any]]]:
        """Plain dictionaries, for a dataframe or for writing to disk."""
        return {
            "nodes": [
                {
                    "key": n.key, "kind": n.kind, "label": n.label,
                    "x_m": n.x, "y_m": n.y, "branch": n.branch, **n.meta,
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "key": e.key, "kind": e.kind, "label": e.label,
                    "x0_m": e.x0, "y0_m": e.y0, "x1_m": e.x1, "y1_m": e.y1,
                    "length_m": e.length_m, "branch": e.branch, **e.meta,
                }
                for e in self.edges
            ],
        }


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _unit(heading_deg: float) -> Tuple[float, float]:
    radians = math.radians(heading_deg)
    return math.cos(radians), math.sin(radians)


def _normal(heading_deg: float) -> Tuple[float, float]:
    """Left-hand normal of a bearing, used to offset the return pipe."""
    radians = math.radians(heading_deg + 90.0)
    return math.cos(radians), math.sin(radians)


def _fan_headings(count: int, parent_heading: float) -> List[float]:
    """Bearings for ``count`` terminals that name none of their own.

    One terminal carries straight on. Two send one left and one right, which is
    what a street junction looks like. More than two spread evenly across the
    same half-turn.
    """
    if count <= 1:
        return [parent_heading]
    if count == 2:
        return [parent_heading + 90.0, parent_heading - 90.0]
    if count == 3:
        return [parent_heading + 90.0, parent_heading, parent_heading - 90.0]
    step = 180.0 / (count - 1)
    return [parent_heading + 90.0 - i * step for i in range(count)]


def _resolve_root(system_or_branch: Any) -> Branch:
    branch = getattr(system_or_branch, "branch", system_or_branch)
    if not isinstance(branch, Branch):
        raise TypeError(
            "plot_network needs a DistrictCoolingSystem or a Branch; got "
            f"{type(system_or_branch).__name__}."
        )
    return branch


class _LayoutBuilder:
    def __init__(
        self,
        default_segment_m: float,
        building_offset_m: Optional[float],
        pipe_offset_m: Optional[float],
        stub_m: Optional[float],
    ) -> None:
        self.layout = NetworkLayout()
        self.default_segment = float(default_segment_m)
        self._building_offset = building_offset_m
        self._pipe_offset = pipe_offset_m
        self._stub = stub_m

    # Scale.
    def calibrate(self, root: Branch) -> None:
        """Pick offsets from the size of the network, unless told otherwise."""
        lengths = [
            length
            for branch in root.walk()
            for length in (branch.pipe_lengths or {}).values()
            if length
        ]
        typical = (sum(lengths) / len(lengths)) if lengths else self.default_segment
        if self._building_offset is None:
            self._building_offset = 0.26 * typical
        if self._pipe_offset is None:
            self._pipe_offset = 0.035 * typical
        if self._stub is None:
            self._stub = 0.30 * typical

    # Drawing primitives.
    def node(self, key, kind, label, x, y, branch=None, **meta) -> LayoutNode:
        node = LayoutNode(key, kind, label, float(x), float(y), branch, dict(meta))
        self.layout.nodes.append(node)
        return node

    def edge(self, key, kind, a, b, label="", branch=None, **meta) -> LayoutEdge:
        edge = LayoutEdge(key, kind, float(a[0]), float(a[1]), float(b[0]), float(b[1]),
                          label, branch, dict(meta))
        self.layout.edges.append(edge)
        return edge

    def pipe_pair(self, key_prefix, a, b, heading, branch_label, **meta) -> None:
        """Draw a supply/return pair as two lines either side of the street axis."""
        nx, ny = _normal(heading)
        off = self._pipe_offset
        self.edge(
            f"{key_prefix}/supply", "supply",
            (a[0] + nx * off, a[1] + ny * off), (b[0] + nx * off, b[1] + ny * off),
            label=f"{key_prefix} supply", branch=branch_label, **meta,
        )
        self.edge(
            f"{key_prefix}/return", "return",
            (b[0] - nx * off, b[1] - ny * off), (a[0] - nx * off, a[1] - ny * off),
            label=f"{key_prefix} return", branch=branch_label, **meta,
        )

    # Walk the tree and draw as we go.
    def walk_branch(self, branch: Branch, start: Tuple[float, float], heading: float) -> None:
        position = branch.origin or start
        current = heading
        # A pump sits at its branch inlet, whether it is the main one or a
        # booster on a spur.
        if branch.pump is not None:
            self.node(
                f"pump/{branch.label}", "pump", getattr(branch.pump, "label", "pump"),
                position[0], position[1], branch.label,
                role="distribution pump" if branch.pump_placement == "supply_inlet"
                else "return pump",
            )
        lengths = branch.pipe_lengths or {}
        headings = branch.pipe_headings or {}
        attrs = branch.pipe_attrs or {}

        for index in range(1, branch.n_pipes_per_side + 1):
            key = f"supply_{index}"
            qualified = f"{branch.label}/{key}"
            if key in headings:
                current = float(headings[key])
            else:
                self.layout.assumed_headings.append(qualified)
            length = lengths.get(key)
            if length is None:
                self.layout.assumed_lengths.append(qualified)
                length = self.default_segment
            dx, dy = _unit(current)
            end = (position[0] + dx * length, position[1] + dy * length)
            self.pipe_pair(
                f"{branch.label}/{index}", position, end, current, branch.label,
                length_m=float(length), diameter_m=attrs.get(key, {}).get("D"),
            )

            if index <= branch.n_buildings:
                building = branch.buildings[index - 1]
                self.node(
                    f"{branch.label}/tap_{index}", "junction",
                    f"{branch.label} tap {index}", end[0], end[1], branch.label,
                    role="building tap",
                )
                # Alternate sides so consecutive buildings on one street do not
                # sit on top of each other.
                side = 1.0 if index % 2 else -1.0
                nx, ny = _normal(current)
                self.node(
                    f"building/{building.label}", "building", building.label,
                    end[0] + nx * self._building_offset * side,
                    end[1] + ny * self._building_offset * side,
                    branch.label,
                    order=index,
                    Q_design_W=getattr(building, "Q_design", None),
                )
                self.edge(
                    f"{branch.label}/service_{index}", "service", end,
                    (end[0] + nx * self._building_offset * side,
                     end[1] + ny * self._building_offset * side),
                    label=f"{building.label} service", branch=branch.label,
                )
            position = end

        self.walk_terminals(branch, position, current)

    def walk_terminals(
        self, branch: Branch, position: Tuple[float, float], heading: float
    ) -> None:
        terminals: Sequence[Terminal] = branch.terminals
        if len(terminals) > 1:
            self.node(
                f"{branch.label}/fork", "junction", f"{branch.label} fork",
                position[0], position[1], branch.label,
                role="split/merge", ways=len(terminals),
            )
        else:
            self.node(
                f"{branch.label}/end", "junction", f"{branch.label} end of line",
                position[0], position[1], branch.label, role="end of line",
            )

        defaults = _fan_headings(len(terminals), heading)
        for terminal, default_heading in zip(terminals, defaults):
            bearing = (
                float(terminal.heading_deg)
                if terminal.heading_deg is not None
                else default_heading
            )
            if isinstance(terminal, SubBranch):
                self.walk_branch(terminal.branch, position, bearing)
                continue

            dx, dy = _unit(bearing)
            stub = (position[0] + dx * self._stub, position[1] + dy * self._stub)
            self.edge(
                f"{branch.label}/{terminal.label}/leg", "service", position, stub,
                label=terminal.label, branch=branch.label,
            )
            if isinstance(terminal, EndBypass):
                self.node(
                    f"{branch.label}/{terminal.label}", "junction", terminal.label,
                    stub[0], stub[1], branch.label,
                    role="end bypass", m_kg_s=terminal.m,
                )
            elif isinstance(terminal, SatellitePlant):
                self.place_plant(
                    key=f"{branch.label}/{terminal.label}",
                    label=terminal.label,
                    position=stub,
                    heading=bearing,
                    chiller=terminal.chiller,
                    cooling_tower=terminal.cooling_tower,
                    storage=terminal.storage,
                    branch_label=branch.label,
                    role="satellite plant",
                )

    def place_plant(
        self,
        key: str,
        label: str,
        position: Tuple[float, float],
        heading: float,
        chiller: Any,
        cooling_tower: Any,
        storage: Any,
        branch_label: Optional[str],
        role: str,
    ) -> None:
        """Draw a plant block: chiller, its tower, and its store if it has one."""
        nx, ny = _normal(heading)
        gap = max(self._stub * 0.62, 1e-6)
        self.node(
            key, "chiller", label, position[0], position[1], branch_label,
            role=role, Q_evap_W=getattr(chiller, "_Q_evap", None),
        )
        if cooling_tower is not None:
            tower = (position[0] + nx * gap, position[1] + ny * gap)
            self.node(
                f"{key}/tower", "cooling_tower", getattr(cooling_tower, "label", "cooling tower"),
                tower[0], tower[1], branch_label, role="condenser water",
            )
            self.edge(f"{key}/tower_leg", "service", position, tower,
                      label="condenser water", branch=branch_label)
        if storage is not None:
            store = (position[0] - nx * gap, position[1] - ny * gap)
            self.node(
                f"{key}/storage", "storage", getattr(storage, "label", "storage"),
                store[0], store[1], branch_label,
                role="cold storage", capacity_kWh=getattr(storage, "capacity_kWh", None),
            )
            self.edge(f"{key}/storage_leg", "service", position, store,
                      label="cold storage", branch=branch_label)


def compute_layout(
    system_or_branch: Any,
    storage: Any = None,
    cooling_tower: Any = None,
    chiller: Any = None,
    default_segment_m: float = DEFAULT_SEGMENT_M,
    building_offset_m: Optional[float] = None,
    pipe_offset_m: Optional[float] = None,
    stub_m: Optional[float] = None,
) -> NetworkLayout:
    """Compute the plan-view layout of a network without drawing it.

    Pass a :class:`~discoolpy.utils.DistrictCoolingSystem` and the central
    plant, its tower, its store and the whole branch tree are all read off it. Pass a bare :class:`~discoolpy.branch.Branch` to draw the distribution
    network alone, optionally naming the plant components yourself.

    The offsets default to a fraction of the network's own typical pipe length,
    so the same call produces a readable drawing for a 300 m campus and a 5 km
    district without being told the scale.
    """
    root = _resolve_root(system_or_branch)
    if chiller is None:
        chiller = getattr(system_or_branch, "chiller", None)
    if cooling_tower is None:
        cooling_tower = getattr(system_or_branch, "cooling_tower", None)
    if storage is None:
        storage = getattr(system_or_branch, "storage", None)

    builder = _LayoutBuilder(default_segment_m, building_offset_m, pipe_offset_m, stub_m)
    builder.calibrate(root)

    origin = root.origin or (0.0, 0.0)
    heading = float(root.heading_deg or 0.0)
    dx, dy = _unit(heading)
    plant = (origin[0] - dx * builder._stub * 1.4, origin[1] - dy * builder._stub * 1.4)

    if chiller is not None:
        builder.place_plant(
            key="central plant",
            label=getattr(chiller, "label", "central plant"),
            position=plant,
            heading=heading,
            chiller=chiller,
            cooling_tower=cooling_tower,
            # A supervisory store is an accounting device, not a pipe fitting;
            # only a hydraulically coupled one sits in the network.
            storage=storage if getattr(storage, "coupling", None) == "hydraulic" else None,
            branch_label=None,
            role="central plant",
        )
        builder.edge("central plant/feed", "service", plant, origin,
                     label="plant to distribution")

    builder.walk_branch(root, origin, heading)
    return builder.layout


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def plot_network(
    system_or_branch: Any,
    ax: Any = None,
    title: Optional[str] = None,
    annotate: bool = True,
    annotate_pipes: bool = False,
    show_pumps: bool = True,
    legend: bool = True,
    scale_bar: bool = True,
    save_path: Optional[str] = None,
    dpi: int = 160,
    layout: Optional[NetworkLayout] = None,
    **layout_kwargs: Any,
) -> Any:
    """Draw the network layout and return the matplotlib axes.

    Parameters
    ----------
    system_or_branch:
        A :class:`~discoolpy.utils.DistrictCoolingSystem` (the usual case) or a
        bare :class:`~discoolpy.branch.Branch`.
    annotate:
        Label buildings, plants and stores. Turn it off for a dense network.
    annotate_pipes:
        Also label each pipe with its length and diameter.
    show_pumps:
        Draw pumps. They are not part of the required marker set, so this is the
        one thing you may want off for a strictly schematic drawing.
    save_path:
        When given, the figure is written there as well as returned.
    **layout_kwargs:
        Forwarded to :func:`compute_layout` (``default_segment_m``,
        ``building_offset_m``, ``pipe_offset_m``, ``stub_m``).

    The network does not need to have been solved; the layout is a property of
    the scenario, not of a solution.
    """
    import matplotlib.pyplot as plt

    if layout is None:
        layout = compute_layout(system_or_branch, **layout_kwargs)

    if ax is None:
        extent = layout.extent_m
        xmin, ymin, xmax, ymax = layout.bounds()
        aspect = (ymax - ymin + 0.2 * extent) / (xmax - xmin + 0.2 * extent)
        width = 11.0
        _, ax = plt.subplots(figsize=(width, max(4.0, min(11.0, width * aspect))))

    for edge in layout.edges:
        style = _EDGE_STYLE.get(edge.kind, _EDGE_STYLE["service"])
        ax.plot(
            [edge.x0, edge.x1], [edge.y0, edge.y1],
            color=style["color"], linewidth=style["width"], linestyle=style["style"],
            solid_capstyle="round", zorder=2,
        )
        if annotate_pipes and edge.kind == "supply" and edge.meta.get("length_m"):
            diameter = edge.meta.get("diameter_m")
            text = f"{edge.meta['length_m']:.0f} m"
            if diameter:
                text += f" DN{diameter * 1000:.0f}"
            ax.annotate(
                text, ((edge.x0 + edge.x1) / 2, (edge.y0 + edge.y1) / 2),
                fontsize=6.5, color=_EDGE_STYLE["supply"]["color"],
                ha="center", va="bottom", zorder=7,
            )

    drawn_kinds: List[str] = []
    for kind in ("junction", "building", "storage", "cooling_tower", "chiller", "pump"):
        if kind == "pump" and not show_pumps:
            continue
        nodes = layout.nodes_of(kind)
        if not nodes:
            continue
        drawn_kinds.append(kind)
        style = _NODE_STYLE[kind]
        ax.scatter(
            [n.x for n in nodes], [n.y for n in nodes],
            marker=NODE_MARKERS[kind], s=style["size"],
            facecolor=style["color"], edgecolor=style["edge"],
            linewidths=0.9, zorder=style["z"], label=kind.replace("_", " "),
        )

    if annotate:
        offset = 0.022 * layout.extent_m
        for node in layout.nodes:
            # Taps and forks are too dense to label; a bypass is worth naming.
            if node.kind == "junction" and node.meta.get("role") != "end bypass":
                continue
            if node.kind == "pump" and not show_pumps:
                continue
            # A plant block stacks three markers within a few tens of metres, so
            # send their labels in three different directions rather than
            # letting them pile up.
            dx, dy, ha, va = _LABEL_PLACEMENT.get(node.kind, (0.0, 1.0, "center", "bottom"))
            ax.annotate(
                node.label, (node.x + dx * offset, node.y + dy * offset),
                fontsize=7.5, ha=ha, va=va, zorder=8, color="#222222",
            )

    if scale_bar:
        _draw_scale_bar(ax, layout)

    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("east (m)")
    ax.set_ylabel("north (m)")
    ax.set_title(title or _default_title(system_or_branch, layout))
    ax.grid(True, linestyle=":", linewidth=0.5, alpha=0.5)
    ax.margins(0.12)
    if legend and drawn_kinds:
        handles, labels = ax.get_legend_handles_labels()
        supply = ax.plot([], [], color=_EDGE_STYLE["supply"]["color"], lw=2.0)[0]
        ret = ax.plot([], [], color=_EDGE_STYLE["return"]["color"], lw=2.0)[0]
        ax.legend(
            handles + [supply, ret], labels + ["supply pipe", "return pipe"],
            loc="best", fontsize=8, framealpha=0.9,
        )

    if save_path:
        ax.figure.tight_layout()
        ax.figure.savefig(save_path, dpi=dpi, bbox_inches="tight")
    return ax


def _default_title(system_or_branch: Any, layout: NetworkLayout) -> str:
    root = _resolve_root(system_or_branch)
    branches = list(root.walk())
    buildings = sum(b.n_buildings for b in branches)
    plants = 1 + len(root.satellite_plants())
    parts = [
        f"{len(branches)} branch{'es' if len(branches) != 1 else ''}",
        f"{buildings} building{'s' if buildings != 1 else ''}",
        f"{plants} plant{'s' if plants != 1 else ''}",
    ]
    title = f"{root.label}: " + ", ".join(parts)
    if not layout.geometry_is_complete:
        title += "  (schematic: some lengths/bearings assumed)"
    return title


def _draw_scale_bar(ax: Any, layout: NetworkLayout) -> None:
    """A metric scale bar, because the axes are in metres and that is the point."""
    xmin, ymin, xmax, ymax = layout.bounds()
    span = max(xmax - xmin, 1.0)
    raw = span / 5.0
    magnitude = 10 ** math.floor(math.log10(raw))
    bar = min([1, 2, 5, 10], key=lambda m: abs(m * magnitude - raw)) * magnitude
    pad = 0.04 * layout.extent_m
    x0, y0 = xmin, ymin - pad
    ax.plot([x0, x0 + bar], [y0, y0], color="#333333", linewidth=2.5,
            solid_capstyle="butt", zorder=9)
    ax.annotate(
        f"{bar:.0f} m", (x0 + bar / 2, y0 - 0.012 * layout.extent_m),
        fontsize=8, ha="center", va="top", color="#333333", zorder=9,
    )
