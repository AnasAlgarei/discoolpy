# Network topology

How a DisCoolPy network is put together: branches, terminals, forks, satellite plants, and the degree-of-freedom rules that decide whether a scenario can be solved at all.

If you only read one section, read [The three rules](#the-three-rules).

---

## A branch is a hydraulic ladder

One branch is one street. Chilled water enters at the branch inlet and runs along the supply header; at each **tap** a splitter draws off the flow for one building, which passes through its substation heat exchanger and rejoins the return header at a **junction**. What is left at the end of the supply header goes to the branch's **terminals**.

```
             supply_1        supply_2        supply_3
  inlet ────────────► S1 ────────►  S2 ────────► S3 ──► terminals
                      │             │            │
                  building_1    building_2   building_3
                      │             │            │
  outlet ◄──────────  R1 ◄──────── R2 ◄───────── R3 ◄── terminals
             return_1        return_2        return_3
```

`S1…Sn` are the splitter nodes and `R1…Rn` the merge nodes. A TESPy `Splitter` and `Merge` force all their ports to one pressure, so each is a single node in the pressure graph.

Buildings are taken **in the order the branch lists them**: the first name in `branch.buildings` is the first tap after the inlet, and the last is nearest the end of the line. That ordering is not cosmetic, the first building sees the coldest supply water and the highest pressure, the last sees whatever is left after the pipe gain and friction of everything upstream.

A branch may also carry **no buildings at all**. It is then a trunk link with exactly one supply and one return pipe (`supply_1`, `return_1`), useful when a main runs some distance before its first junction.

---

## Terminals

The end of a branch has to send its flow somewhere. A branch ends in a list of **terminals**, each of which spans the same two nodes, the end-of-line supply node `Sn` and the end-of-line return node `Rn`.

| Type | What it is | Hydraulically |
|---|---|---|
| `bypass` | An end-of-line valve returning flow to the return header. Every dead-end street needs one to maintain minimum flow. | A valve between `Sn` and `Rn`. |
| `plant` | A **satellite plant**: a chiller with its own cooling tower and optionally its own store, taking a slipstream, cooling it, and delivering it into the return header. | A balancing valve, an evaporator, and optionally a store, in series between `Sn` and `Rn`. |
| `branch` | Another branch, fed from the end of this one. Two or more is a street that forks. | The whole subtree, entering at `Sn` and leaving at `Rn`. |

When a branch has more than one terminal, DisCoolPy inserts a TESPy `Splitter`/`Merge` pair at the fork. Neither drops pressure, so the fork adds no *node* to the pressure graph, only loops. That is the single most important fact about branching, and the next section is about what follows from it.

A scenario that declares no `terminals` gets one bypass valve carrying `design.bypass_mass_flow_kg_s`, exactly the pre-branching topology, unchanged.

---

## Degrees of freedom

### Pressure: a loop can fix all but one

Any closed loop of pressure-carrying elements can have the drop of every element but one specified. The remaining one is whatever the loop closure says it is. Fix them all and the problem is over-determined; fix too few and part of the network floats.

For a single branch of *n* buildings closed through the plant there are **n + 1** loops (the *n* building legs plus the bypass, all closing through the plant), **3n + 1** elements, and **2n** of them may fix a drop.

**A fork changes this.** Its terminals sit in parallel between one pair of nodes, so *k* terminals add *k − 1* loops. And once each parallel path is reachable through its own supply and return pipes, *both* of its end nodes are already anchored, so the terminal spanning them closes a loop, and pinning its drop is one specification too many.

This is why a scenario that solves happily as a single street with `bypass_dp: 0.0` stops solving the moment it forks with both bypasses pinned. TESPy's complaint about it is a dump of fifteen variable names and eighteen equation names.

### The convention that always works

**Pipes fix the hydraulics; valves absorb the residual.**

- Give every pipe its geometry (`L`, `D`, `ks`) or its `pr`.
- Leave every building leg free (`pr: null`).
- Leave every terminal free (`dp: null`).

Each freed element's solved drop is then exactly what a real balancing valve at that point would have to provide, which is useful output, not an input you had to guess. This is the only convention that generalises to a branching network, and it is what `config_branching_grid.yaml` uses.

### Checking it, before TESPy sees anything

```python
from discoolpy import analyse_network_topology, network_topology

report = analyse_network_topology(network_topology(config))
print(report.message())
print(report.n_branches, report.n_forks, report.n_loops, report.n_specified, report.n_required)
```

`network_topology` projects the scenario onto its pressure graph without building a single component; `analyse_network_topology` counts loops and finds redundant specifications with union-find. It runs in milliseconds.

When something is wrong the message names the offending element and the loop it closes:

```
Over-determined hydraulics: 2 redundant pressure specification(s). The network
(4 branches) has 9 independent hydraulic loop(s), so exactly 12 of the 21
elements may fix a pressure drop; 14 do.
  - 'east spur/return_1' (darcy_group (L+D+ks)) closes a loop whose drop is
    already fixed by: south leg/return_1, south leg/south dead end, ...
  Fix: remove the pressure specification from one element in each loop listed
  above ...
  Note: this network forks 2 time(s), and a fork puts its terminals in parallel
  between the same two nodes. Parallel paths that are already anchored by their
  pipes cannot also pin a terminal drop. Free the bypass valves ('dp: null')
  and let them balance.
```

Setting `branch.auto_relax_pressure: true` lets DisCoolPy repair it instead. It frees the minimum number of *balancing valves*, building legs first, then terminals, and records what it released in `system.notes`. It never touches pipe geometry, because conflicting geometry means the scenario itself is wrong and quietly rewriting it would hide that.

### Energy: exactly one free variable

Every pipe in the whole network needs exactly one energy specification, and the closed chilled-water loop needs exactly one *un*specified energy variable to absorb the balance.

Under `plant_control: supply_temperature` that free variable is the **central** chiller's duty, so every pipe must be specified (`heat_model: adiabatic` sets `Q = 0` and counts). A satellite plant's duty stays asserted and therefore adds no free variable, releasing a second duty would leave the loop under-determined.

Under the legacy `evaporator_duty` the central duty is asserted, so exactly one pipe must be left free instead. That pipe is a slack variable: any inconsistency between the asserted duty and the real loads is silently dumped into it as a fictitious pipe heat duty. Use `supply_temperature`.

### Mass flow: only leaves need a number

Fix the mass flow at every building inlet and at every **bypass** and **satellite plant**. A sub-branch never names one, it carries whatever its subtree consumes, computed bottom-up.

That is also exactly the set of mass flows TESPy needs specified, which is not a coincidence: both counts come from the same tree. Inspect it with:

```python
from discoolpy import network_design_mass_flows
flows = network_design_mass_flows(config)
flows["north spur"]["inlet"]                  # kg/s entering that branch
flows["north spur"]["pipe"]["supply_1"]       # kg/s in one pipe
flows[""]["inlet"]                            # the network total
```

---

## The three rules

1. **Every building belongs to exactly one branch**, and that branch's own list sets the tap order. A building on no branch, or on two, is refused by name.
2. **Only leaves name a mass flow.** A bypass and a satellite plant need `m_kg_s`; a sub-branch does not.
3. **Free the balancing valves.** `pr: null` on building legs, `dp: null` on terminals. Let the pipes fix the hydraulics.

---

## Satellite plants

A satellite plant takes a slipstream from the end of a line, cools it, and delivers it into the return header. It carries part of the district duty locally, so the central plant sees a colder return and a smaller evaporator load.

```yaml
- type: plant
  label: airport satellite
  m_kg_s: 14.0              # slipstream
  Q_evap_kW: 180.0          # design and maximum duty
  dispatch: proportional    # what it *delivers* follows the district's load
  min_load_fraction: 0.35
  dp: null                  # balancing valve free
  chiller:
    T_evap_degC: 1.0
    T_cond_degC: 46.0
    eta_s: 0.72
  cooling_tower:
    approach_temperature_K: -6.0
  storage:
    enabled: true
    capacity_kWh: 900.0
    coupling: hydraulic
    target_chiller_load_kW: 150.0
```

**Its duty is asserted, not solved.** The chilled-water loop has exactly one free energy variable and that is the central machine. That is not a limitation dressed up as a feature: how hard a distributed plant runs *is* a dispatch decision, not a physical consequence.

**Sizing the slipstream.** The temperature drop across the satellite is `Q / (m · cp)`. With 180 kW over 14 kg/s that is 3.1 K, so 6.5 °C supply water leaves at 3.4 °C. Too small a slipstream drives the water below freezing and the solve fails inside CoolProp with a property-range error rather than anything useful. Check the arithmetic before you run.

**`dispatch: proportional`** makes what the plant *delivers* follow the district's load, floored at `min_load_fraction`. With a store in series the plant's own chiller then runs level while its output follows demand, which is the reason to put a store at a satellite in the first place. `dispatch: fixed` holds the design duty regardless.

**Reading the result.** A satellite is reported separately from the central machine:

```python
system.branch.satellite_report()
# {'per_plant': {'airport satellite': {'Q_evap_W': ..., 'compressor_power_W': ...,
#                                      'cop': ..., 'storage_Q_W': ..., 'delivered_Q_W': ...,
#                                      'storage_soc': ...}},
#  'total_Q_evap_W': ..., 'total_compressor_power_W': ...,
#  'total_storage_Q_W': ..., 'total_delivered_Q_W': ...}
```

and in the result frame as `satellite_Q_evap_W`, `satellite_delivered_Q_W`, `total_compressor_power_W` and `fleet_cop`.

**What a satellite buys, honestly.** In `config_branching_grid.yaml` a 180 kW satellite takes about **10 % off the central machine's peak duty and peak power**, which is what defers a plant expansion. But the **fleet** peak barely moves and fleet energy goes up about 3 %, because the satellite is a smaller, less efficient machine than the central one.

A satellite plant moves duty; it does not create it. It earns its place when it defers central capacity, when it avoids pumping and pipe gain on a long index run, or when its condenser sits somewhere cooler, not automatically. The model is built so that comparison is a measurement rather than an assumption: swap the plant for a bypass carrying the same slipstream and subtract.

---

## A worked topology

`configs/config_branching_grid.yaml`:

```
central plant
     |
[downtown trunk]  city hall -> conference centre
     |
     +--(north)--> [north spur]  north tower -> teaching block --> bypass
     |
     +--(east) --> [east spur]   airport hotel
                        |
                        +--(south)--> [south leg]  exhibition hall --> bypass
                        |
                        +-----------> satellite plant (chiller + cold store)
```

Six buildings, four branches, two forks, and all three terminal types. Its degrees of freedom:

- **12 unknown pressure nodes**: 2 + 2 + 1 + 1 taps, supply and return.
- **12 fixed elements**: every pipe, from its Darcy geometry.
- **Free**: both end bypasses, the satellite's balancing valve, and all six building legs. Nine loops left free.
- **Energy**: all 12 pipes carry a `ua` heat model; the satellite duty is asserted; the central plant duty is the single free variable.

```python
system = build_system(config)
[b.label for b in system.branches]
# ['downtown trunk', 'north spur', 'east spur', 'south leg']

{b.label: [x.label for x in b.buildings] for b in system.branches}
# {'downtown trunk': ['city hall', 'conference centre'],
#  'north spur': ['north tower', 'teaching block'],
#  'east spur': ['airport hotel'],
#  'south leg': ['exhibition hall']}
```

Walk it yourself with `system.branch.walk()`, `Branch.sub_branches()`, `Branch.all_buildings()`, `Branch.satellite_plants()` and `Branch.tree_pipe_items()`.

---

## Reporting across a tree

Aggregate methods on the root `Branch` cover the whole subtree:

| Method | Scope |
|---|---|
| `heat_gain_report()` | Every pipe in the tree. Root keys are bare (`supply_1`), descendants namespaced (`north spur/supply_1`). |
| `satellite_report()` | Every satellite plant. |
| `pressure_feasibility()` | Every building leg and terminal. |
| `apply_ambient(...)` | Every pipe, each following its own `ambient_source`. |
| `apply_snapshot(...)` | Every satellite's condenser-water loop. |
| `set_design(...)` | Recurses into children. |
| `add_to_network(nw)` | Adds the whole tree, plus every satellite chiller subsystem. |

Per-branch numbers are easy to recover, which is worth doing, aggregating gain over the whole network hides where it comes from:

```python
per_pipe = system.branch.heat_gain_report()["per_pipe_W"]
for branch in system.branches:
    prefix = "" if branch is system.branch else f"{branch.label}/"
    gain = sum(per_pipe[f"{prefix}{k}"] for k, _ in branch.pipe_items())
    length = sum((branch.pipe_lengths or {}).get(k, 0.0) for k, _ in branch.pipe_items())
    print(f"{branch.label:<16} {gain/1e3:6.2f} kW over {length:6.0f} m "
          f"= {gain/length:5.2f} W/m")
```

```
downtown trunk    12.44 kW over   1720 m =  7.23 W/m
north spur         5.58 kW over   1120 m =  4.98 W/m
east spur          5.52 kW over    820 m =  6.73 W/m
south leg          4.92 kW over    560 m =  8.78 W/m
```

The south leg is the shortest route in the network and picks up the most heat per metre, because it crosses a service bridge in 42 °C air instead of being buried in 31 °C soil. Nothing but a per-branch breakdown would show that.

---

## Building the tree in Python

Everything the YAML does is available directly, which is what the tests use:

```python
from discoolpy import Branch, Building, EndBypass, SubBranch

spur = Branch(
    "north spur",
    buildings=[Building("north tower", Q_design=340e3)],
    terminals=[EndBypass(label="north dead end", m=0.08, dp=None)],
    pump_placement=None,
    pipe_attrs={"supply_1": {"pr": 0.999}, "return_1": {"pr": 0.999}},
    pipe_lengths={"supply_1": 300.0, "return_1": 300.0},
    pipe_headings={"supply_1": 90.0},
)

trunk = Branch(
    "downtown trunk",
    buildings=[Building("city hall", Q_design=310e3)],
    terminals=[
        SubBranch(branch=spur),
        EndBypass(label="east dead end", m=0.05, dp=None),
    ],
    pipe_attrs={"supply_1": {"pr": 0.999}, "return_1": {"pr": 0.999}},
)
```

Then `trunk.connect_between(source, "out1", sink, "in1")` and `trunk.add_to_network(nw)`.

---

## See also

- [Configuration reference](configuration.md#branchterminals), every terminal key.
- [Pipe parameter guide](pipe_parameter_guide.md), choosing lengths, diameters and bearings.
- [Network layout plots](layout.md), drawing the tree.
- `examples/branching_networks.ipynb`: all of the above, executed.
