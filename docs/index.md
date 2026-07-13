DisCoolPy — Documentation

> **Version:** 1.1 | **License:** MIT | **Python:** ≥ 3.9 | **TESPy:** ≥ 0.7

---

## Introduction

**DisCoolPy** is an open-source Python tool for modelling and simulating district cooling networks. It is designed for **planned-system analysis**: you supply street/pipe lengths, building peak loads, and chiller specifications, and the tool assembles a thermodynamically consistent [TESPy](https://tespy.readthedocs.io/) network that can be solved at design point and stepped through time-varying load profiles.

The tool is intended for engineers and researchers who need to:

- Evaluate the performance of a proposed district cooling network before it is built.
- Quantify the impact of pipe sizing, chiller selection, and thermal storage on annual energy consumption and COP.
- Generate high-fidelity electrical demand profiles for use in broader energy-system models (e.g. oemof, mosaik).

---

## Architecture

The tool is structured as a set of **modular component wrappers** around TESPy primitives. Each wrapper encapsulates the TESPy component assembly, connection labelling, and design/offdesign parameter management for one physical subsystem.

```
┌─────────────────────────────────────────────────────────────┐
│                    User configuration                        │
│              (Python dict  or  YAML file)                   │
└────────────────────────┬────────────────────────────────────┘
                         │  load_yaml_config()
                         ▼
┌─────────────────────────────────────────────────────────────┐
│               build_standard_branch_system()                │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────────────┐ │
│  │ Chiller  │  │ CoolingTower │  │       Branch          │ │
│  │ wrapper  │  │   wrapper    │  │  (pump + pipes +      │ │
│  └──────────┘  └──────────────┘  │   buildings +         │ │
│                                  │   bypass)             │ │
│                                  └───────────────────────┘ │
│                         ┌──────────────────────────────┐   │
│                         │   ColdStorage  (optional)    │   │
│                         └──────────────────────────────┘   │
│                                                             │
│                    TESPy Network object                     │
└─────────────────────────────────────────────────────────────┘
                         │  nw.solve(mode='design')
                         │  nw.solve(mode='offdesign', ...)
                         ▼
                  Time-series results
              (COP, Q_evap, W_comp, …)
```

---

## Component Summary

| Component | Module | Description |
|---|---|---|
| `Chiller` | `chiller.py` | Vapour-compression refrigeration cycle (evaporator, compressor, condenser, expansion valve). Supports native TESPy offdesign with characteristic-line extrapolation. |
| `CoolingTower` | `cooling_tower.py` | Condenser heat-rejection tower. Models approach temperature and condenser-water mass flow. |
| `Branch` | `branch.py` | Distribution branch: supply/return pipes, pump, Splitter/Merger junctions, bypass valve, and building substations. Supports `pressure_ratio` and `length_derived_pr` pipe models. |
| `Building` | `building.py` | Building substation heat exchanger. Accepts time-varying cooling loads and mass flows. |
| `ColdStorage` | `cold_storage.py` | Supervisory thermal storage model. Implements charge/discharge state-of-charge tracking and load-leveling dispatch for chilled-water, PCM, or ice storage. |
| `TimeSnapshot` | `time_snapshot.py` | Data container for one time step: building loads, ambient temperature, and storage state. |

---

## Documentation Pages

| Page | Contents |
|---|---|
| **[Installation](installation.md)** | Environment setup, dependencies, verification |
| **[Getting Started](getting_started.md)** | First simulation in 10 minutes |
| **[Pipe Parameter Guide](pipe_parameter_guide.md)** | Choosing lengths, diameters, roughness, and pressure anchors |
| **[API Reference](api_reference.md)** | Full component and utility function reference |

---

## How the Tool Models District Cooling

The tool couples a physical representation of the chilled-water network with a detailed thermodynamic model of the central plant. Generation is handled by the `Chiller` wrapper, which explicitly models the refrigerant cycle and captures the nonlinear effects of part-load operation and changing condensing temperatures. Demand is represented by `Building` substation heat exchangers that accept time-varying cooling loads. The network is built using `Branch` wrappers that support either simple pressure-ratio models or Darcy-Weisbach pipe equations derived from planned street lengths.

The optional `ColdStorage` component implements a supervisory charge/discharge model that shifts the effective cooling load seen by the chiller, enabling load-leveling or peak-shaving dispatch strategies. The storage state of charge is tracked across time steps using a first-order energy balance.

---

## Integration with Energy System Models

By accurately resolving the electrical power required by the chiller compressor and pumps under varying conditions, the tool provides high-fidelity electrical demand profiles that can be fed into larger grid or microgrid optimisation models. The modular output format (CSV time-series of power and cooling) is designed to be consumed by frameworks such as [oemof](https://oemof.org/) and [mosaik](https://mosaik.readthedocs.io/).

---

## References

1. TESPy Developers. *TESPy: Thermal Engineering Systems in Python*. <https://tespy.readthedocs.io/>
2. ASHRAE. *Thermal Energy Storage Design and Operation Resources*. <https://www.ashrae.org/technical-resources>
3. oemof Developers. *Open Energy Modelling Framework*. <https://oemof.org/>
4. mosaik Developers. *mosaik: A flexible smart-grid co-simulation framework*. <https://mosaik.readthedocs.io/>
5. Bell, I. H. et al. (2014). *Pure and Pseudo-pure Fluid Thermophysical Property Evaluation and the Open-Source Thermophysical Property Library CoolProp*. Industrial & Engineering Chemistry Research, 53(6), 2498–2508.
