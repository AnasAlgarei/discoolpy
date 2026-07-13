# Guide to Pipe-Length Inputs and Hydraulic Parameters

This guide explains how to use planned street or trench lengths as inputs in DisCoolPy, how to choose pipe parameters that are likely to converge, and when to use each supported pipe model. The refactored workflow lets users move most scenario assumptions into YAML files, so planned systems can be represented with transparent, reviewable network data rather than hard-coded example scripts.

## Overview

The tool now supports three pipe-input styles through `branch.pipe_model` in the YAML scenario. The most robust option for planning examples is **`length_derived_pr`**. In this mode, the user supplies planned pipe length `L`, internal diameter `D`, and roughness `ks`; the utility layer estimates a Darcy-Weisbach pressure drop at the design mass flow and passes the equivalent TESPy pressure ratio `pr` to the pipe component. This preserves street-length realism while avoiding the high numerical sensitivity that can occur when a small conceptual network directly solves all Darcy pipe equations.

| YAML value | User-facing pipe inputs | TESPy pipe inputs passed to the model | Recommended use |
|---|---|---|---|
| `pressure_ratio` | `pr`, optional `Q` | Fixed pressure ratio and heat exchange | Fast conceptual studies or reproducing legacy examples. |
| `length_derived_pr` | `L`, `D`, `ks`, optional `Q` | Equivalent design `pr` derived from length and geometry | Planned-system examples where street lengths are known and robust time-series solving is important. |
| `darcy` | `L`, `D`, `ks`, optional `Q` | Native TESPy pipe geometry equations | Detailed hydraulic studies after pressure anchors, diameters, and initial values have been checked carefully. |

## Recommended YAML pattern

The recommended planned-network input method is to keep all design values, component labels, pipe parameters, storage parameters, profile settings, solver settings, and output paths in a YAML file. The example `configs/riyadh_three_building_length_derived_pr.yaml` follows this pattern.

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

The utility script interprets `supply_i` and `return_i` as the sequential supply and return segments of the branch. For the standard three-building example, `supply_2` and `return_2` carry the downstream flow to buildings 2 and 3 plus bypass flow, while `supply_3` and `return_3` carry the downstream flow to building 3 plus bypass flow. This inferred mass-flow logic is used only to calculate an equivalent design pressure ratio in `length_derived_pr` mode.

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

## Bypass valve and the last-building rule

The distribution branch includes a bypass valve that short-circuits the last building in the chain. This valve creates a hydraulic loop that imposes an implicit pressure constraint between the last supply splitter and the last return merger. As a result:

**The last building must have `pr: null` in the YAML configuration.** Setting a pressure ratio on the last building creates a circular dependency that makes TESPy's Jacobian singular. This is the most common source of `AssertionError: Design solve did not converge` for new users.

For an N-building network the correct pattern is:

| Component | `pr` setting | Reason |
|---|---|---|
| `building_1` through `building_(N-1)` | Set to a value close to 1, e.g. `0.995` | Represents pressure drop across the substation heat exchanger |
| `building_N` (last building) | `null` | Bypass valve already constrains this pressure island |
| `supply_2` through `supply_N` | Set to a value close to 1, e.g. `0.997` | Each bridges two pressure islands |
| `return_1` | Set to a value close to 1, e.g. `0.999` | Anchors the return-header pressure |
| `return_2` through `return_N` | Not set | Already constrained by Merge pressure-equality equations |

---

## Pressure anchors and equation balance

TESPy networks require a well-determined set of pressure equations. Supplying a pressure ratio on every possible branch component can overdetermine pressure cycles, while removing too many pressure specifications underdetermines the hydraulic system. The validated three-building YAML file deliberately uses a mixed pattern: one building pressure ratio, several length-derived pipe ratios, and simplified adiabatic anchors on selected segments.

| Symptom | Likely cause | Recommended correction |
|---|---|---|
| `not provided enough parameters` | Too few pressure or pump specifications. | Add one pipe `pr`, one derived length pipe, or a pump pressure/power anchor. |
| `circular dependency` | Too many pressure ratios in the same closed hydraulic loop. | Remove one `pr` or geometry-derived pipe equation from that loop. |
| Negative building `dp` warning | A building with no fixed `pr` is being used as a pressure balancing element. | For planning examples this can be non-fatal if the network converges; for hydraulic studies, revise pipe pressure anchors or switch to a more detailed topology. |
| Non-convergence at high load | Pipe diameters too small, pressure ratios too severe, or initial pressure steps too aggressive. | Increase diameters, relax `min_pr`, improve starting pressures, or test design mode before full time series. |

## Recommended validation sequence

Before running a long 336-snapshot time series, validate the design point. The repository includes `validate_length_pipe_design.py`, which loads the planned-length YAML file and solves only the design case. If that converges, run the full paired comparison with `python3 storage_comparison_example.py`. The refactored utility layer now raises an explicit error if a design or offdesign solve does not converge, which prevents misleading post-processing from partially solved snapshots.

| Step | Command | Expected outcome |
|---|---|---|
| Syntax check | `python3 -m py_compile src/discoolpy/utils.py` | No Python syntax errors. |
| Design-only check | `cd examples && python3 validate_length_pipe_design.py` | TESPy reports convergence for the design case. |
| Full comparison | `cd examples && python3 storage_comparison_example.py` | Two 336-snapshot examples are solved and summary files are written. |

## When to use native Darcy mode

Use `branch.pipe_model: darcy` only when pipe hydraulics themselves are the subject of the study and the network has enough reliable specifications to solve the nonlinear pressure-flow system. Native Darcy mode is more physically explicit, but it is also more sensitive to equation count, initial values, diameter choices, and pump specifications. A recommended workflow is to start with `length_derived_pr`, inspect the equivalent pressure losses, and then graduate to native `darcy` mode after the topology, diameters, and pressure anchors are well understood.

## Validated example note

The validated planned-length example uses `length_derived_pr`, solves 336 half-hour snapshots without storage and 336 half-hour snapshots with storage, and writes results to `yaml_length_pipe_storage_outputs/`. The storage case reduces peak chiller evaporator load from **700.0 kW** to **555.0 kW**, reduces peak compressor power by **36.8 kW**, and increases mean COP from **3.442** to **3.504** in the validated run. TESPy may still print non-fatal diagnostics for a building substation used as a pressure-balancing element; the guide above explains how to interpret and eliminate those messages in project-specific hydraulic layouts.

## References

[1]: https://tespy.readthedocs.io/ "TESPy Documentation"
[2]: https://en.wikipedia.org/wiki/Darcy%E2%80%93Weisbach_equation "Darcy-Weisbach equation"
[3]: https://tespy.readthedocs.io/en/latest/tutorials/ "TESPy Tutorials and Offdesign Calculation Documentation"
