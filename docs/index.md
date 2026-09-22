# DisCoolPy

**Version 1.0.0** | MIT | Python ≥ 3.9 | TESPy ≥ 0.9.16

DisCoolPy is an open-source Python tool for modelling and simulating district cooling networks. You supply street lengths, building peak loads and chiller specifications, and it assembles a thermodynamically consistent [TESPy](https://tespy.readthedocs.io/) network that solves at design point and steps through time-varying load profiles.

It is aimed at planned-system analysis: work out how a network will behave before it is built.

```{toctree}
:maxdepth: 2
:caption: Getting started

installation
quickstart
getting_started
```

```{toctree}
:maxdepth: 2
:caption: Reference

configuration
cli
network_topology
pipe_parameter_guide
heat_gains
cold_storage
layout
reporting
api_reference
```

```{toctree}
:maxdepth: 1
:caption: Worked examples

examples
```

## What it is for

Engineers and researchers use it to:

- Evaluate a proposed district cooling network before it is built.
- Lay out a branching network that follows a real street grid: trunks that fork into spurs, dead-end bypasses, and distributed satellite plants.
- Quantify what pipe sizing, insulation, chiller selection and thermal storage do to annual energy and COP.
- Account for the heat a chilled network gains from its surroundings, through buried mains, building envelopes and storage tank walls. All of it is load the plant has to remove again.
- Assess how much operational flexibility a system can actually deliver, and what that costs.
- Generate electrical demand profiles for wider energy-system models such as [oemof](https://oemof.org/) and [mosaik](https://mosaik.readthedocs.io/).

## The shortest useful example

```bash
pip install -e .
discoolpy new    my_scenario.yaml   # 1. define: a commented template to fill in
discoolpy check  my_scenario.yaml   # 2. check:  design point, a few seconds
discoolpy run    my_scenario.yaml   # 3. run:    the time series
discoolpy report my_scenario.yaml   # 4. report: the results, as figures
```

Each step ends by naming the next. The same four from Python:

```python
from discoolpy import check_scenario, run_scenario

check_scenario("my_scenario.yaml")
result = run_scenario("my_scenario.yaml", periods=48)
print(result.summary())
result.print_report()      # a section per component
result.plot_all()          # a figure per component, plus the dashboard
```

A scenario can be as short as this and still solve:

```yaml
branch:
  autofill: true

buildings:
  - {label: building_1, Q_design_W: 150000.0}
  - {label: building_2, Q_design_W: 150000.0}
```

`autofill` works out the pipe pressure ratios and the adiabatic pipe duties from the number of buildings, and everything in the `design` section falls back to ordinary chilled-water values. See [Quickstart](quickstart.md).

## Documentation map

Read these in order the first time. Afterwards, use the table as an index.

| Page | Contents |
|---|---|
| [Installation](installation.md) | Environment setup, dependencies, verification, troubleshooting |
| [Quickstart](quickstart.md) | From nothing to a solved network in about five minutes |
| [Getting started](getting_started.md) | Your first simulation, end to end, with the reasoning |
| [Configuration reference](configuration.md) | Every YAML section and key, with defaults |
| [Command line](cli.md) | `discoolpy new`, `check`, `plot`, `run`, `report`, `list` |
| [Network topology](network_topology.md) | Branches, terminals, forks, satellite plants, degrees of freedom |
| [Pipe parameter guide](pipe_parameter_guide.md) | Lengths, diameters, roughness, bearings, pressure specification |
| [Heat gains and flexibility](heat_gains.md) | Pipe, building and storage heat gains; how flexibility is assessed |
| [Cold storage](cold_storage.md) | Dispatch, losses, coupling, stratified tanks, results |
| [Network layout plots](layout.md) | Drawing the network as a plan, and reading the drawing |
| [Printed and plotted results](reporting.md) | A report and a figure per component, and for the whole system |
| [Examples and scenarios](examples.md) | What every shipped config, script and notebook demonstrates |
| [API reference](api_reference.md) | Full component and function reference |

## Architecture

DisCoolPy is a set of modular component wrappers around TESPy primitives. Each wrapper handles the TESPy component assembly, connection labelling, and design/offdesign parameter management for one physical subsystem.

```
┌──────────────────────────────────────────────────────────────────────┐
│                        User configuration                            │
│                   (YAML file  or  Python dict)                       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  load_yaml_config()
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       make_branch_specs()                            │
│    normalises the scenario into a TREE of branches, with no TESPy    │
│    involved, so it can be checked before anything is built           │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  analyse_network_topology()
                                │  validate_thermal_degrees_of_freedom()
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          build_system()                              │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────────────────┐  │
│  │ Chiller  │  │ CoolingTower │  │      Branch (root)             │  │
│  │ wrapper  │  │   wrapper    │  │  pump + pipes + buildings      │  │
│  └──────────┘  └──────────────┘  │  + terminals:                  │  │
│                                  │      EndBypass                 │  │
│  ┌────────────────────────────┐  │      SatellitePlant            │  │
│  │  ColdStorage  (optional)   │  │      SubBranch -> Branch ...   │  │
│  └────────────────────────────┘  └────────────────────────────────┘  │
│                                                                      │
│                        TESPy Network object                          │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  nw.solve(mode='design')
                                │  nw.solve(mode='offdesign', ...)
                                ▼
                        Time-series results          plot_network()
                     (COP, Q_evap, W_comp, …)     (plan-view layout)
                                │                      plot_all()
                                └────────────────►  (a figure per component,
                                                     and one for the system)
```

`run_scenario()` and `check_scenario()` drive that pipeline for you. Everything under them stays public, so a study that needs its own control logic between snapshots can call the pieces directly.

## Component summary

| Component | Module | Description |
|---|---|---|
| `Chiller` | `chiller.py` | Vapour-compression cycle: evaporator, compressor, condenser, expansion valve. Supports native TESPy offdesign with characteristic-line extrapolation. Used for the central plant and every satellite. |
| `CoolingTower` | `cooling_tower.py` | Condenser heat-rejection loop. Models approach temperature and condenser-water mass flow. |
| `Branch` | `branch.py` | One street: supply and return pipes, pump, Splitter/Merge taps, building substations, and one or more end terminals. Branches nest, so a network is a tree. |
| `Terminal` | `branch.py` | What closes the end of a branch: `EndBypass`, `SatellitePlant`, or `SubBranch`. |
| `Building` | `building.py` | Building substation heat exchanger. Takes time-varying loads, a weather-driven envelope, and an RC thermal-mass model with a comfort band for pre-cooling. |
| `ColdStorage` | `cold_storage.py` | Chilled-water, PCM or ice storage. Ambient-driven UA losses, and optional hydraulic coupling as a real element in the chilled-water loop so charging costs COP. |
| `StratifiedTank` | `stratified.py` | Optional 1-D layered chilled-water tank. Resolves the thermocline, so delivery temperature is an output that degrades as the store empties. |
| `TimeSnapshot` | `time_snapshot.py` | One time step: building loads, ambient and ground temperature, solar irradiance, metadata. |
| - | `scenario.py` | `run_scenario` and `check_scenario`: the whole pipeline in one call. |
| - | `cli.py` | The `discoolpy` command. |
| - | `thermal.py` | Heat-transfer physics: buried and exposed pipe conductances, tank UA, building envelopes, soil temperature. No TESPy dependency, so it is directly testable. |
| - | `hydraulics.py` | Structural checking of pressure and energy degrees of freedom across the whole tree, with actionable error messages. |
| - | `layout.py` | Plan-view layout and plotting from pipe lengths and bearings. |
| - | `reporting.py` | Printed reports and result figures, per component and for the whole system, from a solved system or a results frame. |
| - | `flexibility.py` | Peak, energy, round-trip, cost and carbon metrics, and the deliverable flexibility envelope. |
| - | `config_schema.py` | YAML to object translation, with every default and compatibility shim in one place. |
| - | `utils.py` | Orchestration: assembly, profile generation, running, post-processing. |

## How the tool models district cooling

**Generation** is the `Chiller` wrapper, which models the refrigerant cycle explicitly and so captures the nonlinear effects of part load and changing condensing temperature. **Demand** is `Building` substation heat exchangers taking time-varying cooling loads.

**Distribution** is a tree of `Branch` objects. A branch is a hydraulic ladder, a supply header with taps, one building leg per tap, and a return header, ending in one or more terminals. A terminal is a bypass valve, a satellite plant, or another branch, which is what makes a street grid representable. Pipes carry a simple pressure ratio, a design-point pressure ratio derived from length, or the full Darcy-Weisbach group.

**Heat gains** are modelled where they occur. Pipes carry a conductance derived from geometry and insulation, which is handed to TESPy's native `UA_group` so that the duty is solved simultaneously with the water temperatures rather than computed alongside them, so supply temperature rises along the street, the distribution ΔT degrades, and plant duty exceeds the sum of the building loads. Buildings can carry an envelope (`UA·ΔT` + solar + infiltration) and a first-order thermal-mass model, which makes their demand weather-dependent and makes pre-cooling representable. Storage gains heat at `UA·(T_ambient − T_store)` instead of losing a fixed fraction per day.

All of which means **the chiller duty cannot be asserted**: the chilled-water loop energy balance already determines it. DisCoolPy releases the central evaporator duty and solves it against the supply-temperature setpoint, which is also the more faithful control model. Every result row carries `chw_energy_residual_W` as a closure check.

The optional `ColdStorage` can be coupled hydraulically, as a real heat flow in the chilled-water loop, rather than as a rescaling of building loads. Charging then forces the plant to produce water below the distribution setpoint, so the COP penalty of making ice shows up in the results instead of being assumed away.

## Structural checking

Two classes of mistake sink a district-cooling model before physics gets a look in, and neither produces a readable error from a solver:

- **Over- or under-specified pressure.** A ladder contains loops, and in a loop you may fix the pressure drop of every element but one. A fork makes it worse: its terminals sit in parallel between one pair of nodes, so *k* terminals add *k − 1* loops.
- **The wrong number of free energy variables.** Every pipe needs exactly one energy specification, and the closed loop needs exactly one unspecified one to absorb the balance.

`hydraulics.py` models both as graphs and checks them before TESPy sees a single component. When something is wrong it names the element and says what to do. `branch.autofill: true` goes further and works the whole specification out for a non-forking branch. See [Network topology](network_topology.md).

## Integration with energy system models

By resolving the electrical power the chiller compressors and pumps need under varying conditions, DisCoolPy produces electrical demand profiles for larger grid and microgrid optimisation models. The output is CSV time series of power and cooling, meant to be read by frameworks such as [oemof](https://oemof.org/) and [mosaik](https://mosaik.readthedocs.io/).

## References

1. TESPy Developers. *TESPy: Thermal Engineering Systems in Python*. <https://tespy.readthedocs.io/>
2. ASHRAE. *Thermal Energy Storage Design and Operation Resources*. <https://www.ashrae.org/technical-resources>
3. oemof Developers. *Open Energy Modelling Framework*. <https://oemof.org/>
4. mosaik Developers. *mosaik: A flexible smart-grid co-simulation framework*. <https://mosaik.readthedocs.io/>
5. Bell, I. H. et al. (2014). *Pure and Pseudo-pure Fluid Thermophysical Property Evaluation and the Open-Source Thermophysical Property Library CoolProp*. Industrial & Engineering Chemistry Research, 53(6), 2498-2508.
