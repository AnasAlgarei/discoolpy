# Guide to Pipe-Length Inputs and Hydraulic Parameters

This guide explains how to use planned street or trench lengths as inputs in DisCoolPy, how to choose pipe parameters that are likely to converge, when to use each supported pipe model, and how to give a network the plan geometry that lets it be drawn. Scenario assumptions live in YAML, so a planned system is transparent, reviewable network data rather than a hard-coded example script.

Everything here applies per branch. A network that forks has one `pipes:` mapping per branch, and the same key names repeat on each; see [Network topology](network_topology.md) for what a fork changes about the degrees of freedom.

## Overview

The tool supports three pipe-input styles through `branch.pipe_model` in the YAML scenario. The most robust option for a first pass is **`length_derived_pr`**; use **`darcy`** when part-load hydraulics matter, since `length_derived_pr` freezes the pressure drop at the design mass flow. In this mode, the user supplies planned pipe length `L`, internal diameter `D`, and roughness `ks`; the utility layer estimates a Darcy-Weisbach pressure drop at the design mass flow and passes the equivalent TESPy pressure ratio `pr` to the pipe component. This preserves street-length realism while avoiding the high numerical sensitivity that can occur when a small conceptual network directly solves all Darcy pipe equations.

| YAML value | User-facing pipe inputs | TESPy pipe inputs passed to the model | Recommended use |
|---|---|---|---|
| `pressure_ratio` | `pr`, optional `Q` | Fixed pressure ratio and heat exchange | Fast conceptual studies or reproducing legacy examples. |
| `length_derived_pr` | `L`, `D`, `ks`, optional `Q` | Equivalent design `pr` derived from length and geometry | Planned-system examples where street lengths are known and robust time-series solving is important. |
| `darcy` | `L`, `D`, `ks`, optional `Q` | Native TESPy pipe geometry equations | Detailed hydraulic studies, and any scenario where part-load pumping matters. Recomputes the drop at every snapshot. |

## Recommended YAML pattern

The recommended planned-network input method is to keep all design values, component labels, pipe parameters, storage parameters, profile settings, solver settings, and output paths in a YAML file. The example `configs/config_length_derived_pr.yaml` follows this pattern; `configs/config_riyadh_heat_gains.yaml` adds heat-transfer parameters to the same structure.

```yaml
branch:
  label: district
  pump_placement: supply_inlet
  native_offdesign: false
  pipe_model: length_derived_pr
  hydraulic:
    design_pressure_bar: 3.0
    fluid_density_kg_m3: 999.0
    dynamic_viscosity_Pa_s: 0.0013
    min_pr: 0.985
    max_pr: 0.9998
  pipes:
    supply_2:
      L: 180.0
      D: 0.30
      ks: 0.00005
      Q: 0.0
```

Pipe keys are **local to their branch**. `supply_i` runs from tap `i-1` to tap `i`, and `return_i` runs from junction `i` back towards the plant; both carry the same design flow, every building from tap `i` onwards, plus whatever leaves through the end of the line. A branch with `n` buildings has `supply_1 … supply_n` and `return_1 … return_n`; a branch with *no* buildings is a trunk link and has exactly `supply_1` and `return_1`. Naming a pipe that does not exist is refused with the list of keys that do.

In a branching network the same keys repeat on each branch, and each branch's `pipes:` mapping is its own. Reporting namespaces them: the root branch keeps bare keys (`supply_1`), descendants are prefixed with their branch label (`north spur/supply_1`).

Design mass flows are computed bottom-up over the whole tree and can be inspected directly:

```python
from discoolpy import network_design_mass_flows

flows = network_design_mass_flows(config)
flows["north spur"]["pipe"]["supply_1"]   # kg/s in that pipe
flows["north spur"]["inlet"]              # kg/s entering that branch
flows[""]["inlet"]                        # the network total
```

This inferred mass-flow logic is used to calculate the equivalent design pressure ratio in `length_derived_pr` mode, and is worth reading anyway when sizing diameters.

## Choosing pipe lengths

Use **actual planned trench, street, or route-centerline lengths**, not straight-line distances, whenever possible. District-cooling pipe pressure loss is proportional to length, so the length should include realistic routing around plots, road crossings, service corridors, and plant-room approaches. If only a GIS road segment length is known, use it as the first estimate and add a project contingency for service entries and bends.

| Input | Practical recommendation | Why it matters |
|---|---|---|
| `L` | Use route length in metres for the pipe segment between two branch nodes. | Longer pipes increase frictional pressure drop approximately linearly. |
| Segment definition | Keep each YAML pipe segment consistent with the branch topology, such as `supply_2` between the first and second building taps. | The utility infers design mass flow from the segment index. |
| Length contingency | Add a modest allowance when only street-centerline lengths are available. | Valve chambers, elbows, and building tie-ins add equivalent length. |

## Choosing diameter and roughness

The internal diameter `D` is the most influential hydraulic parameter because velocity and friction loss change strongly with diameter. As a practical starting point, size chilled-water distribution pipes so that design velocities remain moderate and pressure losses are not extreme. If the equivalent pressure ratio derived by the tool is clipped at `min_pr`, the segment is probably too small, too long, or carrying too much design flow for the chosen design pressure.

| Parameter | Typical starting approach | Diagnostic sign of a poor value |
|---|---|---|
| `D` | Increase diameter until the calculated pressure drop is small compared with the design pressure. | Very low derived `pr`, solver instability, or excessive pump duty. |
| `ks` | Use a small absolute roughness for smooth modern steel or plastic distribution pipe; increase only for rougher or aged materials. | Unusually large pressure drop despite reasonable velocity and length. |
| `design_pressure_bar` | Use the chilled-water design pressure anchor that represents the branch operating pressure. | Derived `pr` becomes too sensitive if the design pressure is unrealistically low. |
| `min_pr` and `max_pr` | Keep clipping bounds close to one for conceptual examples, for example `0.985` to `0.9998`. | Frequent clipping indicates that diameter or design pressure should be revisited. |

The `length_derived_pr` calculation uses the Darcy-Weisbach structure: velocity is estimated from design mass flow and internal diameter, a friction factor is estimated from Reynolds number and relative roughness, and pressure drop is converted to a TESPy pressure ratio. This is an engineering approximation intended to convert known route lengths into robust network inputs; it is not a substitute for final pipe sizing, valve-loss modelling, or calibrated pump selection.

## Bearings and plan geometry

A pipe may also carry a `heading_deg`: its plan-view bearing, measured **counter-clockwise from east**, so `0` is +x (east), `90` is +y (north), `-90` is south. Together with `L`, which the Darcy and `ua` models already need, this is enough to draw the network as the street grid it represents.

```yaml
branch:
  origin_m: [0.0, 0.0]      # where the branch inlet sits, in metres
  heading_deg: 0            # default bearing for this branch's pipes
  pipes:
    supply_1: {L: 520.0, D: 0.28, ks: 5.0e-5, heading_deg: 0}
    supply_2: {L: 340.0, D: 0.25, ks: 5.0e-5, heading_deg: 0}
    return_2: {L: 340.0, D: 0.25, ks: 5.0e-5}     # inherits the supply bearing
    return_1: {L: 520.0, D: 0.28, ks: 5.0e-5}
```

None of this changes anything the solver sees. It exists so `plot_network` can produce a drawing you can hold against a site plan, and, in practice, so that a spur pointing the wrong way is caught by eye before it is caught by a strange pressure profile.

A return pipe inherits its supply counterpart's bearing unless it names its own, because supply and return follow the same street. A branch inherits its parent's heading unless it names its own, and a terminal that names no bearing is fanned out from its parent's. Scenarios with no geometry at all still draw, schematically. See [Network layout plots](layout.md).

Practical advice: take bearings from the same GIS route you took `L` from, and round them to the street grid. Sub-degree precision buys nothing, the drawing exists to be checked, not surveyed from.

---

## Pressure degrees of freedom: the counting rule

This is the single most common source of failure, and it has an exact answer rather than a rule of thumb.

A branch is a hydraulic **ladder**. With `n` buildings there are `3n + 1` elements that could fix a pressure drop, `n` supply pipes, `n` building legs, `n` return pipes, and the end-of-line bypass. Because the chilled-water loop is closed through the cycle closer and the chiller evaporator, the branch inlet and outlet are effectively one anchored node, leaving `2n` unknown pressure nodes and `n + 1` independent loops. Therefore:

> **Exactly `2n` elements may fix a pressure drop. Exactly `n + 1` must be left free.**

Specify one too many and the problem is over-determined; TESPy rejects the network before the first iteration with a list of variable names that is impossible to act on. Specify one too few and the ladder floats.

`discoolpy.hydraulics` performs this count and names the offending element. It runs automatically inside `build_system()`, and you can run it yourself on any scenario, branching or not, in milliseconds:

```python
from discoolpy import analyse_network_topology, load_yaml_config, network_topology

cfg = load_yaml_config("configs/config_riyadh_heat_gains.yaml")
print(analyse_network_topology(network_topology(cfg)).message())
```

### What a fork changes

The `2n` rule above is the single-branch case. A branch may end in several **terminals**, and every terminal spans the same two nodes, its branch's end-of-line supply node and end-of-line return node. So a fork with `k` terminals puts `k` paths in parallel between one pair of nodes and adds `k - 1` loops.

The practical consequence is sharper than the arithmetic suggests. Once each parallel path is reachable through its own supply and return pipes, *both* of its end nodes are already anchored, so the terminal spanning them closes a loop, and pinning its drop is one specification too many. **In a network whose pipes all fix their own drop, no terminal anywhere may pin one.**

That is why a scenario which solves happily as a single street with `bypass_dp: 0.0` stops solving the moment it forks with both bypasses pinned. Set `dp: null` on every terminal and let the balancing valves report the drop the loops need. See [Network topology](network_topology.md).

### What counts as "fixing" a drop

`pr`, `dp`, `zeta`, or a complete `L` + `D` + `ks` triple (in `darcy` mode the geometry *is* the pressure specification). `L` and `D` without `ks` are a thermal specification only and do not count.

### Two valid conventions

**A. Bypass fixed (`bypass_dp: 0.0`, the default).** The bypass and the last building leg span the same two nodes, so fixing both is always redundant, this is the mistake that made two of the three originally shipped scenarios unrunnable.

| Component | Setting |
|---|---|
| `supply_2` … `supply_N` | fixed |
| `return_1` … `return_N` | fixed |
| bypass | fixed at `dp = 0` |
| `supply_1`, all building legs | **free** |

**B. Bypass free (`bypass_dp: null`), recommended for geometry-driven scenarios.** Every pipe fixes its own drop from its geometry; the bypass valve reports the balancing drop, which is what a real end-of-line bypass valve does.

| Component | Setting |
|---|---|
| `supply_1` … `supply_N`, `return_1` … `return_N` | fixed from geometry |
| bypass, all building legs | **free** |

In both cases each building's solved pressure drop is exactly what a balancing/control valve at that building would have to provide.

**Convention B is the one to use.** It is the only one that generalises to a branching network, and it produces the most useful output: each freed element's solved drop is exactly what a real balancing valve at that point would have to provide.

`suggest_pressure_specification(n, bypass_fixed)` returns a valid set for any single-branch `n`. `branch.auto_relax_pressure: true` will free the minimum number of *balancing valves* needed to make an over-determined network well-posed, building legs first, then terminals, and record what it released in `system.notes`. It never touches pipe geometry, because conflicting geometry means the scenario itself is wrong and quietly rewriting it would hide that.

---

## Energy degrees of freedom

The same counting logic applies to heat. Every pipe needs exactly one energy specification, and the closed chilled-water loop needs exactly one *un*specified energy variable to absorb the loop balance.

| `design.plant_control` | Free variable | Requirement |
|---|---|---|
| `supply_temperature` (default when heat gains are active) | chiller evaporator duty | **every** pipe must be specified |
| `evaporator_duty` (legacy) | one pipe's duty | exactly **one** pipe left unspecified |

The count covers every pipe in the tree, including those on sub-branches. A satellite plant's evaporator duty stays asserted and so adds no free variable of its own; release a second duty and the loop goes under-determined, which is why a satellite needs an explicit `Q_evap_kW`.

The legacy arrangement is a trap. The original scenarios left one pipe free with a comment reading "Q not needed for the last return pipe"; that pipe was a hidden slack variable. In `config_length_pipes.yaml` the scenario asserted a 650 W pump while the pump actually drew 1168 W, and the 518 W difference was silently reported as `−518 W` of pipe *cooling*. Nothing in the output flagged it, because pipe duties were never reported.

`validate_thermal_degrees_of_freedom()` now checks this, and every result row carries `chw_energy_residual_W` which must be numerically zero.

See [heat_gains.md](heat_gains.md) for the pipe heat models themselves.

---

## Hydraulic feasibility

Building mass flows are specified, so TESPy will return a **negative** pressure drop across a building leg or the bypass without complaint: the solver is not asked whether the pump could push that flow, only what pressures balance the equations. A negative drop means the round-trip pipe friction to that point exceeds the head available, and in reality the far building would simply be starved.

`branch.pressure_feasibility()` reports this explicitly, and `run_configured_case` warns:

```
Infeasible hydraulics: pressure RISES across building 3 (-0.328 bar). The pump is
short of head by about 0.328 bar; at design flow the far end of the branch cannot
actually be served. Raise the pump head (branch.pump_pressure_ratio or
design.pump_power_W), increase the pipe diameters, or shorten the index run.
```

Without this check the only symptom is a stream of TESPy "Invalid value for pr" warnings buried in the log.

---

## Troubleshooting

| Symptom | Likely cause | Correction |
|---|---|---|
| `Over-determined branch hydraulics` from DisCoolPy | More than `2n` elements fix a drop | Remove the specification the message names, usually the last building's `pr` |
| `Under-determined branch hydraulics` from DisCoolPy | A pipe with no `pr` and no complete geometry | Give it geometry or a `pr` |
| `circular dependency` from TESPy | The structural check was bypassed | Call `analyse_pressure_topology()` directly |
| `plant_control='evaporator_duty' ... over-determines` | Heat gains combined with an asserted duty | Use `supply_temperature` (or remove the heat models) |
| `Chilled-water loop is under-specified` | A pipe has no energy specification | Give it a `heat_model` (`adiabatic` sets `Q = 0`) |
| `Infeasible hydraulics: pressure RISES` | Pump head below round-trip friction | Raise `pump_pressure_ratio`, or increase diameters |
| Non-convergence at high load | Diameters too small, or aggressive initial pressure steps | Increase diameters, adjust `solver.initial_pressure_step_bar`, test design mode first |
| `Over-determined` only after adding a fork | Terminals of a fork are in parallel between one node pair | Set `dp: null` on every bypass and satellite leg |
| Satellite solve fails inside CoolProp | `Q / (m·cp)` drives the slipstream below freezing | Raise `m_kg_s` or lower `Q_evap_kW` |
| `is listed on both branch … and branch …` | A building placed twice | Each building belongs to exactly one branch's `buildings:` list |
| `defines pipe(s) … that do not exist` | A pipe key beyond the branch's building count | A branch with `n` buildings has `supply_1…n` and `return_1…n` |

## Recommended validation sequence

| Step | Command | Expected outcome |
|---|---|---|
| Design-point check, all scenarios | `discoolpy check --all` | Degrees of freedom consistent, design solves, energy residual ~0, hydraulics feasible |
| Layout check | `discoolpy check --all --layout` | One plan-view PNG per scenario in `outputs/layouts/` |
| Test suite | `pytest` | ~260 passing |
| Full comparison | `cd examples && python storage_comparison_example.py` | Two 336-snapshot runs plus a flexibility summary |

## When to use native Darcy mode

Use `branch.pipe_model: darcy` when part-load hydraulics matter. It recomputes pressure drop from geometry at every snapshot, whereas `length_derived_pr` freezes the drop at the **design** mass flow, real drop scales roughly with the square of flow, so at part load the frozen value overstates it. `length_derived_pr` remains the easier option for the solver and is fine for conceptual studies; start there, inspect the equivalent pressure losses, then graduate to `darcy` once diameters and pressure anchors are settled.

## Sizing check

Aim for **1-2 m/s** in the mains. An oversized pipe overstates heat gain (more surface) and understates pumping at the same time, so the error compounds in the direction that flatters the design.

```
v = m_dot / (rho * pi * D^2 / 4)
```

## References

[1]: https://tespy.readthedocs.io/ "TESPy Documentation"
[2]: https://en.wikipedia.org/wiki/Darcy%E2%80%93Weisbach_equation "Darcy-Weisbach equation"
[3]: https://tespy.readthedocs.io/en/latest/tutorials/ "TESPy Tutorials and Offdesign Calculation Documentation"
