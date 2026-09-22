# Command line

Installing the package puts a `discoolpy` command on your path. It is a thin wrapper over [`run_scenario` and `check_scenario`](api_reference.md), so anything the CLI does is available from Python and vice versa.

```
discoolpy <command> [scenario.yaml] [options]
```

The workflow is four steps, and each one ends by naming the next:

```bash
discoolpy new    my_scenario.yaml   # 1. define
discoolpy check  my_scenario.yaml   # 2. check
discoolpy run    my_scenario.yaml   # 3. run
discoolpy report my_scenario.yaml   # 4. report
```

`plot` and `list` sit outside the sequence. `discoolpy run --report` collapses the last two steps
into one.

| Command | What it does |
|---|---|
| [`new`](#new) | Write a scenario template to fill in |
| [`check`](#check) | Solve the design point and report on the scenario |
| [`plot`](#plot) | Draw the plan view, without solving |
| [`run`](#run) | Run the time series and write the results |
| [`report`](#report) | Run it, then write every result figure and the printed report |
| [`list`](#list) | List the scenarios shipped with the package |

`discoolpy --version` prints the installed version. Every command takes `-h`.

## `new`

```bash
discoolpy new my_scenario.yaml            # the annotated template
discoolpy new my_scenario.yaml --minimal  # the short version
discoolpy new my_scenario.yaml --force    # overwrite an existing file
```

Both templates run as they stand, so you can check and run one before editing anything. The annotated one covers every section with commented-out blocks for storage, thermal mass, terminals and pipe geometry; the minimal one is about twenty lines.

Without `--force`, writing over an existing file is refused rather than done quietly.

The annotated template is also in the repository as `examples/blank_scenario.yaml`, paired with `examples/blank_scenario.ipynb`, if you would rather edit a file that is already there.

## `check`

```bash
discoolpy check my_scenario.yaml
discoolpy check --all                      # every shipped scenario
discoolpy check --all --layout             # and draw each one
discoolpy check my_scenario.yaml --layout --layout-dir figures/
discoolpy check my_scenario.yaml --strict   # structural warnings are failures
```

Solves the design point only, which takes seconds, and reports in the order a scenario usually goes wrong:

0. **The structure of the file.** Before any network is built: misspelled keys, sections written as the wrong shape, buildings with no label or no load. A key close to a real one stops the check, naming both; a key close to nothing is a warning. `--strict` makes every warning a failure.
1. **The branch tree.** Which street carries which buildings, in what order, and how each one ends.
2. **Pressure degrees of freedom.** Whether the specification is consistent, and if not, which element is redundant and what to do about it.
3. **Pipe heat models.** Every conductance, per pipe and as a network total, before anything is solved.
4. **The design solve**, and whether it converged.
5. **The plant energy balance.** Building demand, pipe gain split into supply and return, pump heat, satellite duty, and the residual, which has to be about zero.
6. **The operating point.** Compressor power, COP, fleet COP if there are satellites, supply and return temperatures, ΔT, mass flow.
7. **Hydraulic feasibility.** Whether the pump can actually serve the far end of the network.
8. **Solved pressures and duties**, branch by branch.
9. **Storage**, if there is any: coupling, loss model, tank UA.

It ends with a verdict, whichever stage it stopped at, and names the next command when it passed:

```
-- Verdict --
  my_scenario is usable at its design point.
  Next: discoolpy run my_scenario.yaml -n 48     (a short look first; the file asks for 336)
        discoolpy report my_scenario.yaml   (the results, as figures)
```

Exit status is 0 when every scenario checked is usable and 1 when one is not, so it drops into CI unchanged.

| Option | Meaning |
|---|---|
| `--all` | Every shipped scenario |
| `--layout` | Also draw the plan view |
| `--layout-dir` | Where `--layout` writes |
| `--strict` | Treat structural warnings as failures |

Run this before `run`. A scenario that fails at design will fail once per snapshot for the whole horizon, and the first failure says everything the last one would.

## `plot`

```bash
discoolpy plot my_scenario.yaml
discoolpy plot my_scenario.yaml -o figures/network.png
discoolpy plot my_scenario.yaml --plain     # no pipe annotations
discoolpy plot --all -o figures/
```

Draws the network as a plan view from the pipe lengths and bearings in the scenario. No solve, so it works on a scenario that does not converge yet, which is exactly when you want it: a mislabelled branch or a spur pointing the wrong way is cheap to fix now.

Chillers are squares, cooling towers octagons, cold storage circles, buildings triangles, pumps diamonds, and splits and merges points. Distances are in metres on equal-aspect axes. A scenario with no `L` or `heading_deg` still draws, schematically, and the title says so. See [Network layout plots](layout.md).

## `run`

```bash
discoolpy run my_scenario.yaml
discoolpy run my_scenario.yaml -n 48        # shorter run
discoolpy run my_scenario.yaml --quiet      # no per-snapshot progress
discoolpy run my_scenario.yaml --no-plot    # skip the comparison figure
discoolpy run my_scenario.yaml --no-compare # the stored case alone
```

Generates the profile, solves the design point, steps through every snapshot, and writes the results into the scenario's `outputs.output_dir`.

Where the scenario enables a store, this is a **paired** run by default: the same network over the same weather, once without the store and once with, followed by a flexibility assessment of the difference. That comparison is what a storage study is after, and it is easy to get wrong by hand. `--no-compare` runs the stored case on its own.

`-n/--periods` overrides `profiles.periods`. Use it for a first look. A day tells you whether the scenario behaves; the week tells you what it costs.

| Option | Meaning |
|---|---|
| `-n`, `--periods` | Override `profiles.periods` |
| `--no-compare` | Run the stored case alone rather than pairing it |
| `--no-plot` | Skip the comparison figure |
| `--report` | Also write every result figure, as `discoolpy report` would |
| `-q`, `--quiet` | No per-snapshot progress |
| `--all` | Every shipped scenario. Slow. |

## `report`

```bash
discoolpy report my_scenario.yaml
discoolpy report my_scenario.yaml -o figures/          # where to write
discoolpy report my_scenario.yaml -n 48                # shorter run
discoolpy report my_scenario.yaml --results run.csv    # a finished run
```

Solves the design point, runs the time series, and writes a figure for every
component and one for the system as a whole, plus the plan view and the printed
report. Defaults to `outputs/reports/<scenario>/`.

`--results` skips the run and reads a results CSV you already have, while still
building the system for the design-point panels and the layout. That is the fast
way to redraw a long run you do not want to repeat.

What comes out:

```
  system         figures/my_scenario_system.png          the whole district, one page
  balance        figures/my_scenario_balance.png         where the cooling goes
  pipes          figures/my_scenario_pipes.png           gain per pipe, and along the run
  buildings      figures/my_scenario_buildings.png       duty, envelope split, indoor
  chiller        figures/my_scenario_chiller.png         duty, power, COP, duration
  cooling_tower  figures/my_scenario_cooling_tower.png   rejection and closure
  storage        figures/my_scenario_storage.png         dispatch, charge, thermocline
  layout         figures/my_scenario_layout.png          the plan view
  report         figures/my_scenario_report.txt          the printed report
```

A scenario without a store still gets a storage figure; the figure says there is
no store rather than failing. See [Printed and plotted results](reporting.md).

| Option | Meaning |
|---|---|
| `-o`, `--output` | Directory to write into |
| `-n`, `--periods` | Override `profiles.periods` |
| `--results` | Use a finished results CSV instead of running the scenario |
| `--no-compare` | Run the stored case alone rather than pairing it |
| `--dpi` | Figure resolution, default 160 |
| `-q`, `--quiet` | No per-snapshot progress |
| `--all` | Every shipped scenario. Slow. |

## `list`

```bash
discoolpy list
```

Names the scenarios in `configs/` with the `metadata.description` of each. Each shipped scenario is built around one question; see [Examples and scenarios](examples.md) for what they demonstrate.

This needs the source checkout. A wheel install has the templates but not the example configs.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Everything asked for succeeded |
| 1 | A scenario failed its check, or `new` refused to overwrite a file |
| 2 | Bad arguments, or a scenario file that does not exist |

## Equivalents in Python

| Command | Python |
|---|---|
| `discoolpy check s.yaml` | `check_scenario("s.yaml")` |
| `discoolpy check s.yaml --layout` | `check_scenario("s.yaml", layout_path="layout.png")` |
| `discoolpy run s.yaml` | `run_scenario("s.yaml")` |
| `discoolpy run s.yaml -n 48` | `run_scenario("s.yaml", periods=48)` |
| `discoolpy run s.yaml --no-compare` | `run_scenario("s.yaml", compare_storage=False)` |
| `discoolpy plot s.yaml -o p.png` | `plot_network(build_system(load_scenario("s.yaml")), save_path="p.png")` |

The Python forms also accept a config dictionary in place of a path, which is how you sweep a parameter without writing a file per case.
