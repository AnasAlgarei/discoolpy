# Getting Started

Your first district cooling simulation, end to end. By the end of this page you will have solved a two-building network at its design point, stepped it through a day of weather, drawn its layout, and know what to change next.

Assumes you have finished [Installation](installation.md). If you would rather see the whole thing in five minutes and read the reasoning afterwards, start with the [Quickstart](quickstart.md).

---

## Step 1. Run something that already works

Before writing a scenario, run one:

```bash
discoolpy check configs/config_tutorial.yaml
```

You get the branch tree, the hydraulic degree-of-freedom report, the pipe heat models, the design solve, and the plant energy balance with its closure residual. If that prints without error, the whole stack works.

Then run the time series over it, and report on what came out:

```bash
discoolpy run    configs/config_tutorial.yaml -n 48
discoolpy report configs/config_tutorial.yaml -n 48
```

Define, check, run, report. Those four are the whole workflow, and each one ends by naming the next, so the sequence is not something to remember. The rest of this page is what they do underneath, and how to reach into it.

---

## Step 2. Build and solve from Python

The one-call form first, because most of the time it is what you want:

```python
from discoolpy import check_scenario, run_scenario

check_scenario("configs/config_tutorial.yaml")          # design point
result = run_scenario("configs/config_tutorial.yaml", periods=48)
print(result.summary())
```

`check_scenario` prints the same report the CLI does and returns a `CheckResult` that is truthy when the scenario is usable. `run_scenario` generates the profile, solves the design point, steps through the snapshots, writes the results, and hands back a `ScenarioResult`. Where the scenario has a store, it runs the case twice, without and with, and assesses the difference.

Underneath, both go through `build_system`, which is where you start when you need control between snapshots:

```python
from discoolpy import build_system, load_yaml_config

config = load_yaml_config("configs/config_tutorial.yaml")
system = build_system(config)

for note in system.notes:
    print("[note]", note)

system.network.solve(mode="design", max_iter=200)
assert system.network.converged

print(f"plant duty      {system.chiller.solved_Q_evap_W / 1e3:8.2f} kW")
print(f"compressor      {system.chiller.compressor.P.val / 1e3:8.2f} kW")
print(f"COP             {system.chiller.solved_Q_evap_W / system.chiller.compressor.P.val:8.3f}")
```

`build_system` returns a `DistrictCoolingSystem` holding everything the scenario produced:

| Attribute | What it is |
|---|---|
| `.network` | The TESPy `Network` |
| `.chiller` | The central plant `Chiller` |
| `.cooling_tower` | Its condenser-water loop |
| `.buildings` | Every `Building`, in the order the scenario defines them |
| `.branch` | The **root** `Branch`; the rest of the tree hangs off it |
| `.branches` | Every branch, root first |
| `.satellite_plants` | Every distributed plant |
| `.chillers` | Central plant first, then every satellite's |
| `.storage` | The central `ColdStorage`, or `None` |
| `.plant_control` | `"supply_temperature"` or `"evaporator_duty"` |
| `.notes` | Decisions DisCoolPy made and thinks you should know about |

It is also iterable as the six-tuple older code expects:

```python
nw, chiller, buildings, branch, tower, conn = build_system(config)
```

---

## Step 3. Look at what you built

```python
from discoolpy import plot_network

plot_network(system, save_path="layout.png")
```

Squares are chillers, octagons cooling towers, circles cold storage, triangles buildings, and points splits and merges. Pipes are lines, with supply and return drawn as a parallel pair. The axes are in metres.

This does not need a solved network. The layout is a property of the scenario, so it is worth drawing *before* the first solve, when a spur pointing the wrong way is still cheap to fix. See [Network layout plots](layout.md).

---

## Step 4. Step through time

```python
from discoolpy import make_weather_and_load_profiles, run_configured_case

config["profiles"] = {
    "start": "2026-07-14 00:00:00",
    "periods": 48,          # one day at 30-minute resolution
    "freq": "30min",
    "daily_high_degC": [41.0],
    "daily_low_degC": [28.0],
}
config["outputs"] = {"output_dir": "my_outputs"}

profile = make_weather_and_load_profiles(config)
results = run_configured_case(config, profile, case="base", use_storage=False)

print(results[["timestamp", "chiller_Q_evap_W", "compressor_power_W", "cop"]].head())
print("closure residual (W):", results["chw_energy_residual_W"].abs().max())
```

`run_configured_case` solves the design point, saves the design state, then solves one offdesign snapshot per row of the profile. It returns a `DataFrame` with one row per snapshot, see [Configuration reference](configuration.md#result-columns) for what the columns mean.

`make_weather_and_load_profiles` synthesises a diurnal weather and load profile from the `profiles:` section for any number of buildings, using each building's `archetype` to shape its load. Bring your own profile instead by building the DataFrame yourself: it needs a `timestamp` column, weather columns, and one `<building label>_Q_W` column per building.

---

## Step 5. Write your own scenario

```bash
discoolpy new my_scenario.yaml
```

writes a commented template covering every section, with the optional parts commented out. It runs as it stands, so you can check it, run it, and then start replacing numbers one at a time. `--minimal` gives you a twenty-line version instead.

The shortest scenario that solves is shorter than you would expect:

```yaml
branch:
  autofill: true

buildings:
  - {label: building_1, Q_design_W: 150000.0}
  - {label: building_2, Q_design_W: 150000.0}
```

`autofill: true` works out the pipe pressure ratios and the adiabatic pipe duties from the number of buildings, which is the arithmetic covered in Step 6. Everything in the `design` section falls back to ordinary chilled-water values, and anything you do state overrides its fallback.

Write it out in full once you want control over the values. The same network, spelled out:

```yaml
design:
  supply_temperature_degC: 7.0
  building_return_temperature_degC: 12.0
  ambient_temperature_degC: 35.0
  condenser_inlet_temperature_degC: 30.0
  condenser_outlet_temperature_degC: 35.0
  chilled_water_pressure_bar: 3.0
  condenser_pressure_bar: 3.0
  pump_power_W: 300.0
  pump_efficiency: 0.70
  bypass_mass_flow_kg_s: 0.05
  cp_water_J_kgK: 4180.0

chiller:
  label: my_chiller
  T_evap_degC: 2.0
  T_cond_degC: 40.0
  eta_s: 0.80
  refrigerant: R134a

cooling_tower:
  label: my_cooling_tower
  approach_temperature_K: -5.0

buildings:
  - label: building_1
    Q_design_W: 150000.0
    pr: 0.995
  - label: building_2
    Q_design_W: 150000.0
    pr: null

branch:
  label: my_branch
  pipe_model: pressure_ratio
  pipes:
    supply_1: {Q: 0.0}
    supply_2: {pr: 0.997, Q: 0.0}
    return_2: {Q: 0.0}
    return_1: {pr: 0.999}

solver:
  max_iter: 200

outputs:
  output_dir: my_outputs
```

Every key, section by section, is in the [Configuration reference](configuration.md).

---

## Step 6. The two rules that catch everyone

Almost every failed first scenario is one of these two, and DisCoolPy checks both **before** TESPy sees anything, so the message is a sentence rather than a matrix dump.

### Pressure: count the loops

A branch is a hydraulic ladder, and a ladder contains loops. In a loop you may fix the pressure drop of every element but one; the last one's drop is whatever the loop closure says it is. For a single branch of *n* buildings closed through the plant there are *n + 1* loops, so exactly **2n** of the **3n + 1** elements may fix a drop.

The convention that always works: **pipes fix the hydraulics, valves absorb the residual.** Give every pipe its geometry or its `pr`, and leave every building leg (`pr: null`) and every bypass (`dp: null`) free. Their solved drop is then exactly what a real balancing valve at that point would have to provide, which is useful information rather than an input you had to guess.

```python
from discoolpy import analyse_network_topology, network_topology

print(analyse_network_topology(network_topology(config)).message())
```

If it is over-determined, the message names the redundant element and the loop it closes. Two settings help. `branch.auto_relax_pressure: true` frees the minimum number of balancing valves needed to make an over-determined network well posed, and reports what it released. `branch.autofill: true` goes the other way and fills in a consistent specification from scratch, for a branch that does not fork and does not already state a pressure of its own.

### Energy: exactly one free variable

Every pipe needs exactly one energy specification, and the closed chilled-water loop needs exactly one *un*specified energy variable to absorb the balance.

Under `plant_control: supply_temperature`, the default whenever any heat gain is active, that free variable is the central chiller's duty, so **every pipe must be specified**. `heat_model: adiabatic` sets `Q = 0` and counts as specified. Under the legacy `evaporator_duty` the plant duty is asserted, so exactly one pipe must be left free instead.

Full treatment in [Pipe parameter guide](pipe_parameter_guide.md).

---

## Step 7. Where to go next

| You want to… | Go to |
|---|---|
| Understand every YAML key | [Configuration reference](configuration.md) |
| Build a network that forks along streets | [Network topology](network_topology.md), `examples/branching_networks.ipynb` |
| Choose pipe lengths, diameters and bearings | [Pipe parameter guide](pipe_parameter_guide.md) |
| Model buried mains and their heat gain | [Heat gains and flexibility](heat_gains.md) |
| Add thermal storage and assess flexibility | [Heat gains and flexibility](heat_gains.md), `examples/storage_comparison_example.py` |
| Model pre-cooling and demand response | `examples/precooling_flexibility.ipynb` |
| Draw the network | [Network layout plots](layout.md) |
| See what each shipped example does | [Examples and scenarios](examples.md) |
| Read the results, per component and whole-system | [Printed and plotted results](reporting.md) |
| Look up a class or function | [API reference](api_reference.md) |
| Check a scenario before a long run | `discoolpy check --all --layout` |
| Start from a blank file you can edit and run | `examples/blank_scenario.yaml` and `examples/blank_scenario.ipynb` |
| See every command | [Command line](cli.md) |
