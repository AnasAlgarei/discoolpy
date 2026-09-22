# Quickstart

From nothing to a solved network. Assumes DisCoolPy is installed; if it is not, see [Installation](installation.md).

## Four steps

Define a scenario, check it, run it, report it.

```bash
discoolpy new    my_scenario.yaml   # 1. define: a commented template to fill in
discoolpy check  my_scenario.yaml   # 2. check:  design point, a few seconds
discoolpy run    my_scenario.yaml   # 3. run:    the time series
discoolpy report my_scenario.yaml   # 4. report: the results, as figures
```

Each step ends by naming the next, so the sequence is not something to remember. The template runs as it stands, so you can walk all four before changing anything and see what each produces. Then start replacing the numbers.

`discoolpy plot my_scenario.yaml` draws the plan view at any point, without solving anything, which is useful while a scenario still does not converge. `discoolpy run --report` collapses the last two steps into one.

The same four steps from Python:

```python
from discoolpy import check_scenario, run_scenario

check_scenario("my_scenario.yaml")
result = run_scenario("my_scenario.yaml", periods=48)
result.print_report()
result.plot_all()
```

## When the file has a mistake in it

YAML has no schema, so a misspelled key is not an error: it is a key nobody reads. `buidlings:` gives you a scenario with no buildings. DisCoolPy checks the structure of the file before it checks the network, and says what it found:

```
Scenario error in my_scenario.yaml
  buidlings: unknown key 'buidlings'
  -> did you mean 'buildings'?
```

A key that is close to a real one stops the run, because the scenario it produces is not the one you wrote. A key that is close to nothing is a warning and the run continues, because scenarios carry notes and annotations of their own. `discoolpy check --strict` turns every warning into a failure, which is what you want in continuous integration.

## The smallest scenario that works

```yaml
branch:
  autofill: true

buildings:
  - {label: building_1, Q_design_W: 150000.0}
  - {label: building_2, Q_design_W: 150000.0}
```

Six lines, and `discoolpy check` on it reports a converged design point with a closed energy balance. Two things make that possible.

**`autofill: true`** works out the pressure and energy specification for you. A branch of *n* buildings has `3n + 1` elements that could fix a pressure drop and room for exactly `2n` of them, and getting that count wrong is the single most common way a first scenario fails. Autofill applies the standard answer: fix the pipes the arithmetic asks for, leave the building legs and the bypass to be solved. It also gives every adiabatic pipe the `Q = 0` that TESPy needs written down, leaving one pipe free where the plant control mode requires it.

It declines to guess in two cases. On a branch that forks, because which parallel path carries the free element is a modelling decision. And on a branch that already states a pressure anywhere, because a scenario that says something about pressure is assumed to mean it.

**Design defaults.** Everything in the `design` section falls back to ordinary chilled-water values: 7 °C supply, 12 °C return, 35 °C ambient, a condenser loop 5 K wide, 3 bar anchors. State only what you have an opinion about. Anything you do state wins.

The one value with no honest default is pump power, and leaving it out is fine: DisCoolPy sizes the pump from the design flow against a nominal 1 bar head. That converges and lands in the right order of magnitude, but it is not a sizing. Put your real number in `design.pump_power_W` once you have one.

## Growing the scenario

Add things in roughly this order, checking after each.

**Real pipe geometry.** Delete `autofill` and give the pipes lengths, diameters and roughness. With `pipe_model: darcy` the geometry fixes the pressure drop; with `heat_model: ua` the same three numbers also fix the heat gain.

```yaml
branch:
  pipe_model: darcy
  heat_model: ua
  fix_pump_power: false
  pump_pressure_ratio: 1.25
  thermal_defaults:
    placement: buried
    insulation: pur
    insulation_thickness_m: 0.05
    burial_depth_m: 1.2
    ground: moist soil
    twin_spacing_m: 0.7
    ambient_source: ground
  pipes:
    supply_1: {L: 400.0, D: 0.15, ks: 0.00005, heading_deg: 0}
    supply_2: {L: 250.0, D: 0.125, ks: 0.00005, heading_deg: -30}
    return_2: {L: 250.0, D: 0.125, ks: 0.00005}
    return_1: {L: 400.0, D: 0.15, ks: 0.00005}
```

The plant duty now exceeds the sum of the building loads, by the pipe gain plus the pump heat. That gap is cooling the plant makes and nobody uses. See [Heat gains](heat_gains.md) and the [Pipe parameter guide](pipe_parameter_guide.md).

**A store.** Set `storage.enabled: true`, give it a capacity and a `target_chiller_load_kW` near the mean plant duty. `discoolpy run` then runs the scenario twice, once without the store and once with, and reports what the store bought. See [Cold storage](cold_storage.md).

**Thermal mass.** Give a building `load_model: thermal_mass`, an envelope and a comfort band, and it can be pre-cooled and left to coast. Check `<building>_band_violation_K` in the results afterwards: flexibility taken out of occupant comfort is not flexibility.

**A fork.** Add a `terminals` list with a `type: branch` entry, and the branch becomes the root of a tree. Leave every terminal's `dp` free once you do, because a fork puts its terminals in parallel between the same two nodes. See [Network topology](network_topology.md).

## From Python instead

Everything above has a Python equivalent, and the two mix freely. `load_scenario` gives you the YAML as an ordinary dictionary, and every function takes a dictionary as happily as a path.

```python
from discoolpy import check_scenario, load_scenario, run_scenario

config = load_scenario("my_scenario.yaml")
config["buildings"].append({"label": "building_3", "Q_design_W": 200_000.0})

check_scenario(config)                       # design point
result = run_scenario(config, periods=48)    # one day at 30 min

print(result.summary())
result.results.head()
```

`run_scenario` returns a `ScenarioResult`:

| Attribute | What it holds |
|---|---|
| `results` | The result frame you most likely want: the stored case if there is one |
| `cases` | Every frame by name: `base`, or `without_storage` and `with_storage` |
| `report` | A `FlexibilityReport` on a paired run, else `None` |
| `profile` | The weather and demand profile the run used |
| `files` | Path of every file written |
| `summary()` | A few lines of plain text |

`check_scenario` returns a `CheckResult`, which is truthy when the scenario is usable, stringifies to the whole report, and keeps the solved system on `.system`.

## Start short

`periods=48` is one day at half-hourly resolution and takes under a minute. The week a scenario usually asks for takes seven times that. Get the design point right, run a day, read the numbers, then commit to the week.

`discoolpy check` exists for the same reason. A scenario that fails at design will fail once per snapshot, 336 times in a row, and the first failure tells you everything the last one would.

## Next

- [Getting started](getting_started.md) walks the same ground more slowly, with the reasoning.
- `examples/blank_scenario.ipynb` is a notebook version of this page, with a scenario file next to it you can edit in place.
- [Configuration reference](configuration.md) is the full key list.
- `discoolpy list` names the shipped scenarios, each of which is built around one question.
