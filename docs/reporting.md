# Printed and plotted results

`plot_network` draws what a scenario *is*. `discoolpy.reporting` covers what it
*did*: a printed report and a figure for each component, plus a whole-system
pair, for any district cooling system built with DisCoolPy.

It is the fourth of the four steps: define, check, run, report.

```python
from discoolpy import run_scenario

result = run_scenario("configs/config_riyadh_heat_gains.yaml")
result.print_report()      # a section per component
paths = result.plot_all()  # a figure per component, plus the dashboard
```

`ScenarioResult` carries the system that produced it, so neither call solves
anything a second time. `plot_all()` defaults to a `figures` directory beside
the scenario's other output; pass a path to put it elsewhere.

From the shell, which runs the scenario and documents it in one step:

```bash
discoolpy report my_scenario.yaml
discoolpy report my_scenario.yaml -o figures/        # somewhere else
discoolpy report my_scenario.yaml --results run.csv  # redraw a finished run
discoolpy run    my_scenario.yaml --report           # run and report in one
```

---

## What you can hand it

Every function takes the same `source` argument and works out what it is:

| You have | Pass it | What you get |
|---|---|---|
| `ScenarioResult` from `run_scenario` | `print_report(result)` | the time series, and the flexibility report where there is one |
| `CheckResult` from `check_scenario` | `print_report(check)` | the design point and the component objects |
| `DistrictCoolingSystem` from `build_system` | `print_report(system)` | the design point: conductances, design duties, geometry |
| A results `DataFrame` | `print_report(frame)` | the time series |
| A path to a results CSV | `print_report("results.csv")` | the time series |
| Both | `print_report(system, frame)` | everything |

A system carries the design point and the components. A frame carries the time
series. Neither is a subset of the other, so pass both when you have both.

## What the report says depends on what the scenario contains

This is deliberate. These functions are meant to be pointed at an arbitrary
network without first being told what is in it, so a scenario with no store
still produces a storage figure — and the figure says there is no store rather
than failing or drawing an empty box. The same holds for buildings with no
envelope model, networks with no satellite plants, and adiabatic pipes.

The report also comments on what it finds, but only where a number means
something:

```
-- Plant energy balance --
  building demand served             24541 kWh
  distribution pipe gain             973 kWh
    share of demand                  3.97 %
  pump heat                          613 kWh
    share of demand                  2.50 %
  tank ambient gain                  145 kWh
    share of demand                  0.59 %
  plant cooling produced             26608 kWh
    as a share of demand             108.42 %
  worst closure residual             0.0000 W
  * The plant produces 8.4% more cooling than the buildings consume. That excess
    is the network, and a model that treats pipes as adiabatic and the store as
    lossless reports none of it.
```

Notes appear for a store that hit a limit, a comfort band that was violated, a
delta-T that collapsed, a condenser loop that does not close, and a round-trip
efficiency quoted over a non-cyclic run. Each one is a result that is easy to
quote without noticing the caveat.

---

## The figures

Each is four panels, except the dashboard, which is six. Each returns a
matplotlib `Figure` and takes `title`, `axes`, `save_path` and `dpi`.

| Function | Panels |
|---|---|
| `plot_system` | weather, cooling, parasitic load, electricity, chilled water, store |
| `plot_balance` | what the plant removes, parasitic shares, electricity, loop closure |
| `plot_pipes` | design gain and `UA` per pipe, gain over the run, energy per pipe, temperature along the index run |
| `plot_buildings` | duty per building, envelope split, indoor temperature against the comfort band, energy per building |
| `plot_chiller` | cooling produced, electrical power, COP against ambient, power duration curve |
| `plot_cooling_tower` | heat rejection, condenser temperatures, rejection against ambient, condenser closure |
| `plot_storage` | charge and discharge, state of charge, tank gain against ambient, thermocline |

`plot_all(source, directory)` writes all seven, plus the plan view when a system
was supplied and the printed report as a text file, and returns the paths.

### Reading them

A few panels are there to answer one question each.

**Temperature along the index run** (`plot_pipes`) is the panel the heat-gain
model exists to produce. Water leaves the plant at setpoint and arrives warmer;
the further out a building sits, the less of the design delta-T is left for it.
With adiabatic pipes this panel is two flat lines.

**Gain over the run** (`plot_pipes`) should be nearly flat for a buried network
and should swing for an exposed one. Soil at 1.2 m barely moves over a day,
which is why `ambient_source: ground` matters.

**Tank gain against ambient** (`plot_storage`) is a straight line with a slope,
not a horizontal one. That slope is the whole argument against a flat daily loss
fraction: the loss peaks at the hour the stored cooling is worth most.

**Loop closure** (`plot_balance`) is the panel that makes the others mean
anything. If the residual is not numerically zero, something is adding or
removing heat that is not being reported, and the shares above it do not add up.

**Power duration curve** (`plot_chiller`) answers the sizing question a time
series does not: how many hours the plant spends near its peak.

### Colours

Fixed per quantity across every figure, so supply is the same blue in the pipe
figure and in the dashboard. The palette is a published colourblind-safe
categorical order, used in that order and never cycled: past eight series the
rest fold into one "other" line rather than repeating a hue. Nothing is a
dual-axis chart, and no panel relies on a red/green contrast.

---

## Building your own

`resolve_source` is public, so reporting built on top of this module does not
have to repeat the type-sorting:

```python
from discoolpy.reporting import resolve_source

src = resolve_source(result)
gain = src.col("pipe_heat_gain_W")            # None if the run has no such column
kWh = src.energy_kWh("compressor_power_W")    # integrated at the run's resolution
```

The `report_*` functions return a `ComponentReport` whose `data` holds floats
rather than formatted strings, so a notebook or a test should read that rather
than parse `rows`:

```python
from discoolpy import report_balance

share = report_balance(result).data["produced_share_pct"]
```

---

## Cross-checking the pipe heat models

`compare_pipe_heat_models` solves one pipe twice in a three-component TESPy
network, once with DisCoolPy's closed-form conductance and once with TESPy's
native buried or surface group, and returns both:

```python
from discoolpy import compare_pipe_heat_models

result = compare_pipe_heat_models(inner_diameter_m=0.2, length_m=800.0,
                                  insulation_thickness_m=0.05)
result["ratio"]               # 1.131
result["native_available"]    # True
```

`native_available` is `False`, with a `reason`, for the cases TESPy's groups
cannot take: ground outside its four named media, insulation thinner than 1 mm,
or still air on a surface pipe. See [Heat gains](heat_gains.md) for what the
numbers mean.

---

## API

| Function | Returns |
|---|---|
| `report_system(source, results=None)` | `SystemReport` of every section |
| `print_report(source, results=None)` | prints it, and returns it |
| `report_balance`, `report_pipes`, `report_buildings`, `report_chiller`, `report_cooling_tower`, `report_storage`, `report_satellites` | one `ComponentReport` each |
| `plot_system`, `plot_balance`, `plot_pipes`, `plot_buildings`, `plot_chiller`, `plot_cooling_tower`, `plot_storage` | a matplotlib `Figure` |
| `plot_all(source, output_dir, ...)` | `dict` of figure name to `Path` |
| `compare_pipe_heat_models(...)` | `dict` of the two conductances and their ratio |
| `resolve_source(source, results=None)` | the internal record the rest reads from |

`FIGURES` maps figure name to plotting function, in the order `plot_all` writes
them.
