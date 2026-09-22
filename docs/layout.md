# Network layout plots

A district cooling network is a set of streets, and a set of streets has a shape. `discoolpy.layout` recovers that shape from geometry already in the scenario, each pipe's length and bearing, and draws it as a plan.

The point is a drawing you can hold against a site plan, not a graph drawing. Distances are in metres and the axes are equal-aspect, so a 600 m main really is twice as long as a 300 m spur, and a spur that turns north really turns north.

---

## The simplest use

```python
from discoolpy import build_system, load_yaml_config, plot_network

system = build_system(load_yaml_config("configs/config_branching_grid.yaml"))
plot_network(system, save_path="layout.png")
```

That is the whole interface. Hand it the system, with all its subsystems already in it, and it draws.

**The network does not need to have been solved.** The layout is a property of the scenario, so it is worth drawing *before* the first solve, when a mislabelled branch or a spur pointing the wrong way is still cheap to fix.

---

## The marker convention

| Component | Marker |
|---|---|
| Chiller / plant | **square** |
| Cooling tower | **octagon** |
| Cold storage | **circle** |
| Building | **triangle** |
| Split / merge | **point** |
| Pump | **diamond** |
| Pipe | **line**, supply and return drawn as a parallel pair |

Supply pipes are blue and return pipes orange, offset either side of the street axis so both are visible. Service connections are dashed grey: a building's lateral, a plant's link to its tower and store.

Only a **hydraulically coupled** store is drawn. A supervisory store is an accounting device that rescales the building duties; it is not a pipe fitting and drawing it as one would be a lie.

---

## Giving the network its geometry

Two keys, both pure metadata as far as the solver is concerned.

```yaml
branch:
  origin_m: [0.0, 0.0]     # plan-view position of the branch inlet, metres
  heading_deg: 0           # default bearing for this branch
  pipes:
    supply_1: {L: 520.0, D: 0.28, ks: 5.0e-5, heading_deg: 0}    # due east
    supply_2: {L: 340.0, D: 0.25, ks: 5.0e-5, heading_deg: 0}
    return_2: {L: 340.0, D: 0.25, ks: 5.0e-5}                    # inherits 0
    return_1: {L: 520.0, D: 0.28, ks: 5.0e-5}
  terminals:
    - type: branch
      label: north spur
      heading_deg: 90      # the street turns north
      ...
```

`heading_deg` is a bearing measured **counter-clockwise from east**: `0` is +x (east), `90` is +y (north), `-90` is south. A return pipe inherits its supply counterpart's bearing unless it names its own, because supply and return follow the same street.

`L` is doing double duty. The solver already needs it for the Darcy hydraulics and for the `ua` heat model, so adding a bearing is usually the only extra work a real layout costs.

---

## When the geometry is missing

A scenario that never mentions `L` or `heading_deg` still draws. Unknown lengths fall back to a nominal segment (`default_segment_m`, 150 m), and terminals with no bearing of their own fan out evenly around their parent's heading, so a fork with two children sends one left and one right rather than drawing them on top of each other.

The result is schematic rather than surveyed, and the title says so:

```
tutorial_branch: 1 branch, 2 buildings, 1 plant  (schematic: some lengths/bearings assumed)
```

`NetworkLayout.geometry_is_complete` tells you the same thing in code, and `assumed_lengths` / `assumed_headings` list exactly which pipes fell back.

---

## Options

```python
plot_network(
    system,
    ax=None,                  # draw into an existing axes
    title=None,               # default: "<root>: N branches, M buildings, K plants"
    annotate=True,            # label buildings, plants, stores, bypasses
    annotate_pipes=False,     # also label each pipe with its length and diameter
    show_pumps=True,          # pumps are not in the required marker set
    legend=True,
    scale_bar=True,
    save_path=None,           # write a PNG as well as returning the axes
    dpi=160,
    # forwarded to compute_layout:
    default_segment_m=150.0,
    building_offset_m=None,   # default: 0.26 × the network's typical pipe length
    pipe_offset_m=None,       # default: 0.035 ×
    stub_m=None,              # default: 0.30 ×
)
```

The three offsets default to a fraction of the network's own typical pipe length, so the same call produces a readable drawing for a 300 m campus and a 5 km district without being told the scale.

Returns the matplotlib `Axes`, so it composes:

```python
import matplotlib.pyplot as plt

fig, (left, right) = plt.subplots(1, 2, figsize=(20, 8))
plot_network(before, ax=left, title="as planned")
plot_network(after, ax=right, title="with the north spur added")
```

---

## The layout as data

`compute_layout` gives the same thing without matplotlib, which is what you want for a test, a report, or an export to GIS:

```python
from discoolpy import compute_layout

layout = compute_layout(system)
layout.geometry_is_complete          # True when nothing was assumed
layout.bounds()                      # (xmin, ymin, xmax, ymax) in metres
layout.extent_m
layout.nodes_of("building")          # [LayoutNode, ...]

records = layout.to_records()        # {"nodes": [...], "edges": [...]}
pd.DataFrame(records["nodes"])
```

`LayoutNode` carries `key`, `kind`, `label`, `x`, `y`, `branch`, and a `meta` dict (a building's `order` along its branch and its `Q_design_W`; a fork's `ways`; a plant's `Q_evap_W`; a store's `capacity_kWh`). `LayoutEdge` carries the two endpoints, `length_m`, and the pipe's diameter.

A drawn supply edge's `length_m` equals the specified `L` exactly, the parallel offset is perpendicular, so it does not change the length.

Pass a bare `Branch` instead of a system to draw the distribution network alone:

```python
compute_layout(system.branch)     # no central plant; satellites still drawn
```

---

## Reading the drawing

A few things worth checking every time:

- **Do the spurs leave the trunk where you expect?** A fork is drawn as a point at the end of its parent's supply header. If it is in the wrong place, a `heading_deg` or an `L` is wrong.
- **Are the buildings in the right order along each street?** They alternate sides so consecutive taps do not overlap, and `meta["order"]` is their position along the branch.
- **Is the satellite plant where you think it is?** It is drawn as a square with its tower and store either side of it, at the end of the leg it hangs off.
- **Does the scale bar match your site?** If a 600 m main draws as 150 m, that pipe has no `L`.

---

## Worked example

```python
from discoolpy import build_system, compute_layout, load_yaml_config, plot_network

system = build_system(load_yaml_config("configs/config_branching_grid.yaml"))
layout = compute_layout(system)

print("complete:", layout.geometry_is_complete)
for node in layout.nodes_of("building"):
    print(f"  {node.label:<20} branch={node.branch:<16} "
          f"({node.x:7.1f}, {node.y:7.1f}) m  tap {node.meta['order']}")

plot_network(system, annotate_pipes=True, save_path="branching_layout.png")
```

```
complete: True
  city hall            branch=downtown trunk   (  520.0,    91.4) m  tap 1
  conference centre    branch=downtown trunk   (  860.0,   -91.4) m  tap 2
  north tower          branch=north spur       (  768.6,   300.0) m  tap 1
  teaching block       branch=north spur       (  951.4,   560.0) m  tap 2
  airport hotel        branch=east spur        ( 1270.0,    91.4) m  tap 1
  exhibition hall      branch=south leg        ( 1361.4,  -280.0) m  tap 1
```

The trunk runs 520 m then 340 m due east; the north spur turns north at the fork at x = 860 and runs 300 m then 260 m; the east spur carries on east to x = 1270, where the south leg turns south for 280 m and the satellite plant sits a short stub further east.

To draw every shipped scenario at once:

```bash
discoolpy check --all --layout
```

which writes one PNG per scenario into `outputs/layouts/`.

---

## See also

- [Network topology](network_topology.md), what is being drawn.
- [Pipe parameter guide](pipe_parameter_guide.md#bearings-and-plan-geometry), choosing bearings.
- `examples/branching_networks.ipynb`: the plotter in use.
