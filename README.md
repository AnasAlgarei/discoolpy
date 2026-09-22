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

Two ideas run through it.

**Real networks follow street grids, and street grids fork.** A DisCoolPy network is a *tree* of
branches. Each branch is a street with its buildings in order, and it ends in one or more
terminals: a dead-end bypass, a distributed satellite plant with its own chiller and store, or
another branch. Because every pipe also carries a length and a bearing, the whole thing can be
drawn as a plan you can hold against a site drawing.

**A district-cooling network is colder than everything around it, so it gains heat everywhere**,
through buried mains, through building envelopes, and through the walls of a cold store.
DisCoolPy accounts for all three, because each one is a load the plant has to remove again, and
each one erodes the flexibility a thermal store or a pre-cooling scheme can actually deliver.

### Key capabilities

| Feature | Description |
|---|---|
| **Branching networks** | Trees of branches following a street grid. Terminals may be end bypasses, satellite plants, or further branches, nested to any depth |
| **Satellite plants** | Distributed chillers with their own cooling tower and cold store, dispatched to follow the district while their own machine runs level |
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

## Scenarios

| File | What it demonstrates |
|---|---|
| `configs/config_tutorial.yaml` | Minimal two-building network, adiabatic pipes |
| `configs/config_length_pipes.yaml` | Native Darcy pipe hydraulics from street lengths |
| `configs/config_length_derived_pr.yaml` | Design-point pressure ratios derived from lengths |
| `configs/config_riyadh_heat_gains.yaml` | **Buried mains + hydraulically coupled ice store**, one week of Riyadh July |
| `configs/config_precooling_flexibility.yaml` | Building envelopes and thermal mass; demand-side flexibility, no store |
| `configs/config_campus_five_buildings.yaml` | Five buildings, mixed buried/above-ground pipes, **stratified chilled-water tank** |
| `configs/config_branching_grid.yaml` | **A forking street grid**: four branches, two forks, a satellite plant with its own store |

`discoolpy list` prints the same table from the scenarios' own metadata.

## Examples and notebooks

| File | Contents |
|---|---|
| `examples/blank_scenario.yaml` | **A blank scenario to fill in.** Annotated section by section; runs unmodified |
| `examples/blank_scenario.ipynb` | **The same file, walked through cell by cell.** Start here to build your own |
| `examples/validate_scenario.py` | Design-point check: topology, degrees of freedom, conductances, energy balance, hydraulic feasibility, layout |
| `examples/branching_network_example.py` | A forking network, and what a satellite plant is actually worth |
| `examples/storage_comparison_example.py` | Paired with/without-storage run plus a full flexibility assessment |
| `examples/precooling_flexibility_example.py` | Pre-cool and coast within a comfort band |
| `examples/tutorial_dummy_data.ipynb` | 24-hour walkthrough for first-time users |
| `examples/branching_networks.ipynb` | Branching, degrees of freedom, the layout plotter, a satellite plant over a day |
| `examples/stratified_storage.ipynb` | The layered tank: thermocline, delivery temperature, and what stratification changes |
| `examples/heat_gains_and_flexibility.ipynb` | Where the conductances come from; a week with and without an ice store |
| `examples/precooling_flexibility.ipynb` | Demand-side flexibility from building thermal mass |
| `examples/building_your_own_scenario.ipynb` | Degrees-of-freedom counting and a five-building campus |

---

## Two representative results

### Heat gains and storage: `config_riyadh_heat_gains.yaml`, one week at 30 min

| | Without store | With ice store |
|---|---:|---:|
| Peak compressor power | 201.8 kW | 175.0 kW (−13.3 %) |
| Compressor energy | 26 116 kWh | 26 361 kWh (+0.9 %) |
| Load factor | 0.770 | 0.897 |
| Electricity cost | 3732 | 3454 (−7.4 %) |
| Emissions | 14 364 kg | 14 498 kg (+0.9 %) |
| Distribution pipe gain | 4.0 % of demand | 4.0 % of demand |
| Tank ambient gain | - | 0.6 % of demand |
| Thermal round trip | - | 0.809 |
| **Electric** round trip | - | 0.853 |

The plant produces **8.4 % more cooling than the buildings consume**. The store cuts peak power
and cost while *increasing* kWh and emissions. Whether that is a good trade depends entirely on
the tariff, which is the question a flexibility assessment should surface rather than hide.

### A distributed plant: `config_branching_grid.yaml`, one day at 30 min

| | Without satellite | With satellite |
|---|---:|---:|
| Central plant peak duty | 1707 kW | 1536 kW (−10.0 %) |
| Central compressor peak | 437.5 kW | 393.7 kW (−10.0 %) |
| **Fleet** compressor peak | 437.5 kW | 437.9 kW (+0.1 %) |
| **Fleet** compressor energy | 8128 kWh | 8346 kWh (+2.7 %) |
| Mean fleet COP | 3.67 | 3.64 (−0.8 %) |

A 180 kW satellite takes 10 % off the *central* machine, which is what defers a plant expansion,
but the *fleet* peak barely moves and fleet energy rises, because the satellite is a smaller, less
efficient machine. **A satellite plant moves duty; it does not create it.** It earns its place
when it defers central capacity, avoids pumping and pipe gain on a long index run, or sits
somewhere its condenser runs cooler. Not automatically.

---

## Testing

```bash
pip install -e ".[dev]"
pytest                      # 459 tests
pytest -m "not slow"        # unit tests only
```

The suite covers the heat-transfer physics in closed form, exact RC integration and comfort-band
enforcement, storage state-of-charge conservation, flexibility metric arithmetic, branching
topology and its degrees of freedom, layout geometry in metres, the scenario defaults and the
command line, the reporting module against scenarios with no store, no envelope and no time
series at all, a cross-check of the closed-form pipe conductance against TESPy's own buried and
surface groups, and an end-to-end check that every shipped scenario reaches a converged design
point with a chilled-water energy balance that closes to under 1 W.

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull
request.

## License

MIT, see [LICENSE](LICENSE).

## Citation

If you use DisCoolPy in academic work, please cite:

```bibtex
@article{Algarei2026DisCoolPy,
  title   = {DisCoolPy: A modular Python framework for district cooling network
             simulation with distributed heat gains and flexibility assessment},
  author  = {Algarei, Anas},
  journal = {SoftwareX},
  year    = {2026}
}
```
