# DisCoolPy

> A modular district-cooling modelling tool built on [TESPy](https://tespy.readthedocs.io/).

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![TESPy](https://img.shields.io/badge/TESPy-0.9.16%2B-green)](https://tespy.readthedocs.io/)
[![Version](https://img.shields.io/badge/version-1.0.0-brightgreen)](CHANGELOG.md)
[![Docs](https://img.shields.io/badge/docs-readthedocs-blue)](https://discoolpy.readthedocs.io/)

---

## Overview

**DisCoolPy** is an open-source Python tool for modelling and simulating district cooling
networks. You supply street lengths, building load profiles and chiller specifications, and the
tool assembles a thermodynamically consistent TESPy network that solves at design point and steps
through time-varying load profiles.

### Key capabilities

| Feature | Description |
|---|---|
| **Branching networks** | Trees of branches following a street grid. Terminals may be end bypasses, satellite plants, or further branches, nested to any depth |
| **Network layout plots** | Plan-view drawing from pipe lengths and bearings. Chillers are squares, towers octagons, storage circles, buildings triangles, forks points |
| **Result reports and figures** | A printed report and a four-panel figure for every component, plus a whole-system dashboard, from a solved system or a results CSV |
| **Modular components** | Independent wrappers for chillers, cooling towers, branches, buildings, and cold/ice storage. Any number of buildings |
| **YAML configuration** | All inputs in a single scenario file; no code changes to switch scenarios |
| **Planned pipe lengths** | Street lengths, diameters and bearings; the tool derives hydraulics and heat-transfer conductances |
| **Heat gains everywhere** | Buried/exposed pipe gain from geometry and insulation, weather-driven building envelopes, ambient-driven storage losses |
| **Building thermal mass** | RC zone model with a comfort band, so pre-cooling and load shedding are representable |
| **Thermal storage** | Chilled-water, PCM or ice, coupled hydraulically into the chilled-water loop so charging costs COP |
| **Stratified tanks** | Optional 1-D layered chilled-water store: the thermocline is resolved, so delivery temperature degrades as the tank empties |
| **Flexibility assessment** | Peak reduction, energy penalty, thermal *and* electric round-trip efficiency, cost and carbon |
| **Structural checking** | Pressure and energy degrees of freedom validated across the whole tree before TESPy sees the network |
| **Time-varying simulation** | Nonlinear offdesign snapshots over hourly or sub-hourly profiles |

---

## Quick start

```bash
git clone https://github.com/AnasAlgarei/discoolpy.git
cd discoolpy

conda env create -f environment.yml
conda activate discoolpy
pip install -e .
```

That puts a `discoolpy` command on your path. The workflow is four steps: define a scenario, check
it, run it, report it.

```bash
discoolpy new    my_scenario.yaml   # 1. define: a commented template to fill in
discoolpy check  my_scenario.yaml   # 2. check:  design point, a few seconds
discoolpy run    my_scenario.yaml   # 3. run:    the time series
discoolpy report my_scenario.yaml   # 4. report: the results, as figures
```

Each step ends by naming the next, so the sequence is not something to remember. The template runs
as it stands, so you can walk all four before changing anything. `discoolpy plot my_scenario.yaml`
draws the plan view at any point without solving, and `discoolpy run --report` collapses the last
two steps into one.

A misspelled key in a YAML file is not an error, it is a key nobody reads, so the structure of the
file is checked before the network is:

```
Scenario error in my_scenario.yaml
  buidlings: unknown key 'buidlings'
  -> did you mean 'buildings'?
```

Check every shipped scenario solves, and draw each one:

```bash
discoolpy check --all --layout
```

### A scenario this short solves

```yaml
branch:
  autofill: true

buildings:
  - {label: building_1, Q_design_W: 150000.0}
  - {label: building_2, Q_design_W: 150000.0}
```

`autofill` works out the pipe pressure ratios and the adiabatic pipe duties from the number of
buildings, which is the degree-of-freedom arithmetic that stops most people writing their first
scenario. Everything in the `design` section falls back to ordinary chilled-water values, and
anything you state overrides its fallback.

### From Python

```python
from discoolpy import check_scenario, run_scenario

check_scenario("configs/config_riyadh_heat_gains.yaml")   # design point
result = run_scenario("configs/config_riyadh_heat_gains.yaml", periods=48)

print(result.summary())
print(result.report.to_markdown())    # the store's flexibility, on a paired run
```

Everything underneath stays public, for a study that needs its own control logic between
snapshots:

```python
from discoolpy import build_system, load_yaml_config, plot_network

system = build_system(load_yaml_config("configs/config_branching_grid.yaml"))

plot_network(system, save_path="layout.png")     # no solve needed
system.network.solve(mode="design", max_iter=400)

print(f"central plant   {system.chiller.solved_Q_evap_W / 1e3:8.1f} kW")
print(f"satellites      {system.branch.satellite_report()['total_Q_evap_W'] / 1e3:8.1f} kW")
print(f"pipe gain       {system.branch.heat_gain_report()['total_heat_gain_W'] / 1e3:8.1f} kW")
```

Results come back printed and plotted, per component and for the whole system, for
any network built with the tool:

```python
from discoolpy import run_scenario, print_report, plot_all

result = run_scenario("configs/config_riyadh_heat_gains.yaml")
print_report(result)                      # a section per component
plot_all(result, "outputs/figures")       # a figure per component, plus the dashboard
```

Or open `examples/blank_scenario.ipynb`, a fill-in notebook paired with a fill-in scenario file.

---

## Documentation

Full documentation at [discoolpy.readthedocs.io](https://discoolpy.readthedocs.io/).

| Document | Description |
|---|---|
| [Introduction](docs/index.md) | Tool overview, architecture, and design philosophy |
| [Installation](docs/installation.md) | Environment setup, dependencies, verification, troubleshooting |
| [**Quickstart**](docs/quickstart.md) | From nothing to a solved network in about five minutes |
| [Getting Started](docs/getting_started.md) | Your first simulation, end to end |
| [**Configuration reference**](docs/configuration.md) | Every YAML section and key, with defaults |
| [**Command line**](docs/cli.md) | `discoolpy new`, `check`, `plot`, `run`, `report`, `list` |
| [**Network topology**](docs/network_topology.md) | Branches, terminals, forks, satellite plants, degrees of freedom |
| [Pipe Parameter Guide](docs/pipe_parameter_guide.md) | Lengths, diameters, roughness, bearings, pressure specification |
| [Heat gains and flexibility](docs/heat_gains.md) | Pipe, building and storage heat gains; how flexibility is assessed |
| [**Cold storage**](docs/cold_storage.md) | How the storage component works: dispatch, losses, coupling, stratified tanks, results |
| [**Network layout plots**](docs/layout.md) | Drawing the network as a plan |
| [**Printed and plotted results**](docs/reporting.md) | A report and a figure per component, and for the whole system |
| [Examples and scenarios](docs/examples.md) | What every shipped config, script and notebook demonstrates |
| [API Reference](docs/api_reference.md) | Full component and function reference |

**Requirements:** Python ≥ 3.9, TESPy ≥ 0.9.16 (tested on 0.9.16 and 0.11.0), CoolProp, PyYAML,
pandas, matplotlib, numpy, scipy.

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull
request.

## License

MIT, see [LICENSE](LICENSE).

