# Changelog

## 1.0.0: define, check, run, report

The first stable release. The physics and the structural checking were finished
two releases ago; what this one adds is the way through them, and a way to read
what comes out. A scenario is something you define, check, run and report on, in
four commands that each name the next, and a mistake in the file is now a
sentence rather than a traceback.

The API is stable from here. Anything in `discoolpy.__all__` keeps working
through 1.x, and anything that has to change will be deprecated first.

### Added: the four-step workflow

- **`discoolpy report <scenario>`**, the fourth step. Solves, runs, and writes
  a printed report and a figure for every component plus a whole-system
  dashboard. `--results` redraws a finished run without repeating it.
  `discoolpy run --report` collapses the last two steps into one.
- **Each step names the next.** `new` ends by printing the check command,
  `check` ends with a verdict and the run command, `run` ends with the report
  command. On a scenario long enough to be worth sampling first, `check`
  suggests `-n 48` rather than the 336 the file asks for.
- **A verdict on every check**, whichever stage it stopped at, so whether a
  scenario passed is never something to infer from a wall of output.
- **`ScenarioResult` carries the system that produced it**, with
  `print_report()` and `plot_all()` on it. The report step costs no second
  solve, because it reuses the network the run already built.
- **`ScenarioResult.plot_all()` defaults its own destination**, to `figures/`
  beside the scenario's other output, so a run and its figures stay together
  without being asked where.

### Added: a scenario is checked as a file before it is checked as a network

- **`validate_scenario`**, run by every entry point. YAML has no schema, so a
  misspelled key is not an error, it is a key nobody reads: `buidlings:`
  produced a scenario with no buildings, and the traceback named the line that
  tripped over the absence rather than the line that caused it.
- **A key close to a real one stops the run**, naming both: *unknown key
  'buidlings' -> did you mean 'buildings'?*. A single-character slip and a case
  slip are each tested for directly, because a similarity ratio is a poor judge
  of short keys like `D`.
- **A key close to nothing is a warning**, not an error. Scenarios carry notes
  and private annotations, and refusing those would make the check a nuisance
  rather than a help. `--strict` turns every warning into a failure, for
  continuous integration.
- **Requirements are checked with the fix attached**: at least one building,
  unique labels, a positive design load. A building with no load names the two
  keys that would give it one.
- **`SCENARIO_KEYS`** is the map of what a scenario may contain, and every
  shipped scenario and template is tested against it, so it cannot drift far
  from the code without a test going red.

### Added: `Q_design_kW`

A building's design load may be written in kilowatts, which is the unit it
arrives in on a schedule. `Q_design_W` still works; giving both is refused. A
`Q_design_W` under 10 kW is warned about, because a district substation that
small does not exist and the likely cause is kilowatts typed into a watts key.

### Added: `ScenarioError`

Everything DisCoolPy raises on its own account inherits from `DisCoolPyError`,
and a problem in a scenario file is a `ScenarioError` carrying the file, the key
path and the fix separately. The command line prints it and exits 1; a notebook
catches it; `check_scenario` returns it on a falsy `CheckResult` rather than
raising, because reporting problems is what that step is for.

### Added: printed and plotted results

- **`discoolpy/reporting.py`**. A printed report and a four-panel figure for
  every component, plus a six-panel dashboard for the whole system, for any
  district cooling network built with the tool. `plot_pipes`, `plot_buildings`,
  `plot_chiller`, `plot_cooling_tower`, `plot_storage`, `plot_balance`,
  `plot_system`, and `plot_all` to write the lot to a directory.
- **One `source` argument everywhere.** A `ScenarioResult`, a `CheckResult`, a
  `DistrictCoolingSystem`, a results frame, a path to a results CSV, or a
  `(system, frame)` pair. A system carries the design point and the components;
  a frame carries the time series; pass both and every report uses both.
- **Scenario-shaped output.** What a report can say depends on what the
  scenario contains, and the functions work that out rather than being told. A
  network with no store still produces a storage figure, and the figure says
  there is no store. Same for buildings with no envelope, networks with no
  satellite plant, and adiabatic pipes, whose solved duties are dust around
  zero and are named rather than plotted.
- **`report_*` functions returning a `ComponentReport`** whose `data` holds
  floats rather than formatted strings, so a notebook or a test reads numbers
  instead of parsing text.
- **Notes on the numbers that are easy to quote without the caveat**: a store
  that hit a power or state-of-charge limit, a comfort band that was violated,
  a distribution temperature difference that collapsed, a condenser loop that
  does not close, a round-trip efficiency measured over a run that did not
  return to its starting state.

### Answered: how pipe heat loss is actually calculated

It was already TESPy-native, and the documentation did not say so clearly.
Under `heat_model: ua` DisCoolPy computes one number, the conductance, and hands
it with an ambient temperature to TESPy's own `UA_group`, whose residual goes
into the same Jacobian as everything else. The duty is solved simultaneously
with the water temperatures. Nothing in the solve path evaluates a heat flow:
`PipeThermal.heat_gain_W` is a pre-solve sanity check called by nothing that
runs during a solve.

The conductance is derived here rather than by TESPy for four reasons, now
measured rather than asserted:

- **The two formulations agree.** `compare_pipe_heat_models` solves the same
  pipe both ways in a real TESPy network. Across DN125 to DN500, 30 to 80 mm of
  PUR and 1.2 to 2.5 m of cover, the closed form runs 7 to 19 % above TESPy's
  buried group, and that gap is entirely TESPy's Wallentén implementation
  counting the steel wall thickness as insulation. Remove the wall and the two
  agree to within 2 %. The soil models are interchangeable; keeping a separate
  steel resistance is the defensible half of the difference.
- **A shared trench has no native term.** TESPy's buried group is a single-pipe
  model. `twin_spacing_m` lowers the gain by 3.6 % in moist soil and 11.4 % in
  dry soil for a DN200 pair at 0.7 m centres.
- **Three ordinary cases the native groups refuse**: ground outside TESPy's four
  named media, insulation thinner than 1 mm, and still air on a surface pipe. On
  an exposed chilled line at a 43 °C ambient, radiation is 38 % of the external
  film, and TESPy's forced-convection group omits it.
- **A conductance you can see before the solve** is one an implausible
  insulation value gets caught in.

No behaviour changed. The native models stay supported, and are now better
supported than they were.

### Changed

- **`Branch.heat_gain_report()` carries per-pipe detail**: the solved duty,
  conductance, log-mean driving difference, both terminal temperatures, length,
  heat model, ambient source and owning branch. All of it read back off the
  solved TESPy component, including for the native groups, which back-report
  `UA` and `lmtd` as results whenever `Tamb` is set. The existing keys are
  unchanged.
- **Results rows carry `pipe_network_UA_W_K`**, the conductance the network
  balanced on at that snapshot.
- **`DistrictCoolingSystem` keeps its scenario**, as `.config`, and can say
  which one it is through `.name`. Figures title themselves from it.
- **`run_configured_case(..., return_system=True)`** hands back the assembled
  system alongside the frame, so the reporting layer does not solve twice.
- **Packaging moved to `pyproject.toml`**, with `setup.py` reduced to a shim for
  pip versions predating PEP 660. One source of truth for the metadata, and the
  classifier is now `5 - Production/Stable`.
- **The test suite sets a headless matplotlib backend in `conftest.py`**, so
  every test file passes on its own rather than only when whichever module
  happened to set the backend was collected first.
- **459 tests**, up from 346.

### Fixed

- **The native pipe groups now fail in the scenario, not in the solver.** A
  ground material outside TESPy's four, an insulation thickness below its 1 mm
  floor, or a zero wind speed on a surface pipe each used to surface as a
  non-convergence message naming neither the pipe nor the value: a `KeyError`
  or a `ZeroDivisionError` raised inside the residual evaluation. Each is now
  refused at configuration time, naming the pipe, the value, and the
  alternative.

---

## 1.0.0a1: one call to run a scenario, and a command line

The physics has been complete for two releases. What was missing was a way in.
Running a scenario meant knowing that `make_weather_and_load_profiles` comes
before `run_configured_case`, which comes before `write_storage_comparison_summary`
and `make_storage_comparison_plot`, and writing a 150-line YAML file before any
of that would solve. Neither is hard, but both are things a first-time user has
to get right before seeing a single number.

This release adds a short path to the same results, and shortens the scenario
file a long way. Nothing underneath moved: every existing scenario, script and
notebook runs unchanged, and the 310 tests that passed before still pass.

### Added: the short way in

- **`discoolpy/scenario.py`**. `run_scenario(config)` does the whole pipeline
  in one call: read the YAML, generate the profile, solve the design point,
  step through every snapshot, write the results. Where the scenario enables a
  store, it is a paired run by default, once without and once with, followed
  by a flexibility assessment of the difference. That is the comparison a
  storage study is actually after, and it takes four coordinated calls to do
  by hand.
- **`check_scenario(config)`**, the cheap counterpart. Solves the design point
  only and reports the topology, the degrees of freedom, the heat-gain
  conductances, the plant energy balance, the operating point, hydraulic
  feasibility, and the solved pressures branch by branch. Run it first: a
  scenario that fails at design fails once per snapshot afterwards.
- **`ScenarioResult`** and **`CheckResult`**. Result frames by case, the
  flexibility report, the paths of every file written, and a `summary()`.
  `CheckResult` is truthy when the scenario is usable, so it drops into an
  `assert` or a CI step.
- **`discoolpy/cli.py`** and a `discoolpy` console script:
  `new`, `check`, `plot`, `run`, `list`. `discoolpy check` exits non-zero on a
  scenario that is not usable, so `discoolpy check --all` works as a smoke test.
- **Scenario templates**, in `discoolpy/templates/`. `discoolpy new` writes an
  annotated one covering every section, or `--minimal` for the twenty-line
  version. Both run unmodified.

### Added: shorter scenarios

- **`branch.autofill: true`** works out the pressure and energy specification
  from the number of buildings. It applies `suggest_pressure_specification`,
  which already knew the answer for a ladder of *n* buildings, and gives every
  adiabatic pipe the `Q = 0` that TESPy needs written down, leaving one free
  where `plant_control: evaporator_duty` requires it. It declines on a branch
  that forks, on a trunk link with no buildings, and on any branch that already
  states a pressure of its own: the first is a modelling decision, and the
  third is a scenario that means what it says.
- **`DESIGN_DEFAULTS`**. Every key in the `design` section now has a fallback,
  so a scenario states only what it has an opinion about. The values are
  ordinary chilled-water district numbers: 7/12 °C distribution, a condenser
  loop 5 K wide above a 35 °C design ambient, 3 bar anchors. Anything a
  scenario states still wins.
- **A derived pump power.** `design.pump_power_W` used to default to zero,
  which is not usable: a pump doing no work has no isentropic efficiency, and
  TESPy divides by the enthalpy rise to compute one. `default_pump_power_W`
  sizes the pump from the design flow against a nominal 1 bar head instead.
  Every shipped scenario states its own pump power, so none of them change.

The three together mean a scenario can be six lines and still solve:

```yaml
branch:
  autofill: true

buildings:
  - {label: building_1, Q_design_W: 150000.0}
  - {label: building_2, Q_design_W: 150000.0}
```

### Added: a fill-in example and hosted documentation

- **`examples/blank_scenario.yaml`** and **`examples/blank_scenario.ipynb`**, a
  pair meant to be edited. The scenario is the annotated template, kept in the
  repository so it can be edited in place; the notebook walks through it cell
  by cell, from a schematic first solve to real pipe geometry with buried-pipe
  heat gain. Both run unmodified.
- **Sphinx and Read the Docs.** `docs/conf.py`, `.readthedocs.yaml` and a
  `[docs]` extra. The pages stay Markdown, parsed by MyST, so they read the
  same on GitHub and on the site. Two new pages: `quickstart.md` and `cli.md`.

### Changed

- **`examples/validate_scenario.py`** is now a thin wrapper over
  `check_scenario`. Same output, same flags; the implementation moved into the
  package so the CLI and the tests share it.
- **Prose throughout.** Docstrings, comments, documentation and notebook
  markdown were rewritten for readability. No behaviour changed.
- **Version 1.0.0a1**, classifier `3 - Alpha`. The feature set is complete
  across branching, storage, heat gains, flexibility and layout; the alpha
  marks the API hardening before 1.0.

### Tests

346, up from 310. The 36 new ones cover the design defaults, the derived pump
power, both halves of autofill including the three cases where it declines,
`run_scenario` against the hand-driven pipeline, `check_scenario`, every CLI
command, and the shipped blank example.

---

## 0.4.0b1: stratified cold storage

A `ColdStorage` has always been a single number: an amount of cooling in kWh.
That is the right resolution for most district planning, but it cannot answer
the question a chilled-water tank actually raises in operation, **at what
temperature does the cooling come out, and for how long?**

A real chilled-water store has no membrane. Cold water sits at the bottom
because it is denser, warm return floats on top, and only a thermocline separates
them. As the tank cycles that thermocline thickens, so the water leaving the cold
port creeps upward in temperature long before the tank is empty. This release
adds an optional 1-D layered tank that resolves it.

Nothing changes for an existing scenario. `storage.stratified` defaults off, and
every scalar store behaves exactly as before.

### Added

- **`discoolpy/stratified.py`**: `StratifiedTank` and `TankStepResult`. The
  tank is divided into equal-volume nodes, indexed bottom-first; each carries
  advection from its upstream neighbour, ambient gain through its own share of
  the shell, and vertical conduction to its neighbours. The update is explicit
  Euler, sub-stepped on a Courant condition, with a buoyancy pass after every
  sub-step. No TESPy dependency, so the physics is testable in closed form.
- **Modelled on mosaik-heatpump's hot water tank**, inverted for district
  cooling: charging pushes cold water in at the *bottom* and displaces warm water
  out of the *top*. One deliberate difference, an unstable pair is **mixed** to
  its mean rather than swapped. Both conserve the same energy, but an unstable
  pair physically overturns and blends, and mixing converges monotonically
  instead of being able to oscillate.
- **`storage.stratified` YAML section**, with geometry given as a volume, as a
  volume plus a height-to-diameter ratio, or as explicit height and diameter.
- **The delivery temperature is now a model output.** New result columns:
  `storage_tank_outlet_temperature_degC`, `storage_cold_port_degC`,
  `storage_warm_port_degC`, `storage_thermocline_m`,
  `storage_tank_mass_flow_kg_s` and the full profile as `storage_tank_T0_degC`
  onwards.
- **`examples/stratified_storage.ipynb`**: the tank on its own, the thermocline
  travelling, the outlet degrading, standing losses, the tank in the campus
  network, and a like-for-like comparison against a scalar store.
- **`check_stratified_temperatures`**, run inside `build_system`: a tank asked to
  charge colder than the plant supplies can never fill, and one asked to
  discharge warmer than the district returns can never empty. Either way it
  drifts isothermal and quietly stops storing anything, so it is now called out
  as a warning and recorded in `system.notes`.

### Changed

- **Dispatch converts power to flow.** The controller still asks for a power, so
  every mode, guard and precedence rule is unchanged; but for a stratified tank
  that power is converted into the mass flow that would deliver it *against the
  temperature difference the tank can offer right now*, clipped to
  `max_tank_flow_kg_s`, and the power that comes back is whatever the layer
  physics produced. As the thermocline reaches the outlet the available
  difference collapses and the delivered power falls away, the shortfall
  appears in `storage_curtailed_request_W` like any other curtailment.
- **`ColdStorage.energy_kWh` is now a property**, backed by the layer profile
  when a tank is present and by a scalar otherwise, so the two representations
  cannot drift apart.
- **Capacity, efficiencies and the standing loss are no longer inputs for a
  stratified tank.** Capacity follows from geometry and the temperature span;
  the round trip is emergent from mixing, conduction and shell gain. Combining a
  tank with `loss_model: fraction` or `both` is refused rather than
  double-counted.
- **`run_configured_case` feeds the tank the network's solved supply and return
  temperatures**, using the previous snapshot as a one-step predictor seeded from
  the design solve, the same device already used for the parasitic-gain
  estimate, and for the same reason.
- **`configs/config_campus_five_buildings.yaml` now carries a stratified tank.**
  Doing so exposed that its scalar store declared 3200 kWh alongside a 2800 m3
  volume, figures that disagree by a factor of eight, and that its store
  temperatures (5 / 13 degC) did not match what the network circulates
  (7.0 / 13.5 degC). Both are fixed; the tank is 425 m3 in 14 layers.
- **`examples/storage_comparison_example.py`** reports the tank when the scenario
  has one: delivered energy, how much of it came back within half a kelvin of the
  setpoint, the outlet range, the thermocline span, and the final profile.
- **`examples/validate_length_pipe_design.py` is now `examples/validate_scenario.py`.**
  The old name came from the length-derived pipe pressure drop it was written to
  check; it has been the generic design-point validator for two releases:
  `--config` any scenario, `--all` for every shipped one, `--layout` to draw
  them. The flags and the output are unchanged. There is no shim at the old
  name, so update any script that called it.

### Documentation

- **[Cold storage](docs/cold_storage.md)** gains a full stratified-tank section:
  the model and how it differs from mosaik-heatpump, why capacity is not an
  input, why the temperatures must match the network, how dispatch changes, the
  extra result columns, sizing rules, and an explicit account of the limitations.
- Configuration reference, examples page, API reference, index and README
  updated.

### Tests

- `tests/test_stratified.py` (44), closed-form conservation (energy in equals
  energy stored to nine significant figures; standing loss equals shell UA times
  the driving difference), buoyant stability, the outlet degrading, and the
  network integration. The headline behavioural claim, that a worn thermocline
  costs usable capacity, is checked at 8, 12 and 20 layers so it cannot be an
  artefact of the discretisation.
- 307 tests in total, up from 266.

---

## 0.3.0b1: branching networks, satellite plants, and layout plots

The theme of this release: **real district cooling networks follow street grids, and
street grids fork.** A DisCoolPy network is now a tree of branches rather than a single
street, and because every pipe carries a length and a bearing, the tree can be drawn.

Nothing in this release changes what an existing scenario means. A `branch:` section
with no `terminals: ` key describes exactly the topology it always did, one street
ending in one bypass valve, and produces identical numbers.

### Added: branching

- **`Branch` is now a tree.** A branch ends in a list of **terminals**, each of which is
  an `EndBypass` (a dead-end valve), a `SatellitePlant` (a distributed chiller), or a
  `SubBranch` (another `Branch`). Two or more of the last is a street that forks;
  branches nest to any depth. Declared in YAML under `branch.terminals`.
- **Buildings are assigned to branches, in order.** Each branch lists its own
  `buildings:` by label, and the first name is the first tap after the branch inlet.
  A building on no branch, or on two, is refused by name. When no branch lists any, the
  root takes them all in scenario order, the previous behaviour.
- **Branches with no buildings** are legal: a trunk link carrying one supply and one
  return pipe from its parent to its own fork.
- **Children inherit** `pipe_model`, `heat_model`, `thermal_defaults`, `hydraulic`,
  `native_offdesign` and `auto_relax_pressure` from their parent, so a network of
  mostly-identical mains only needs its exceptions spelled out.
- **Design mass flows are computed bottom-up over the tree.** Only leaves, bypasses and
  satellite plants, need an `m_kg_s`; a sub-branch carries whatever its subtree
  consumes. `network_design_mass_flows()` exposes the whole calculation.

### Added: satellite plants

- **`SatellitePlant`**: a distributed chiller with its own `CoolingTower` and optionally
  its own `ColdStorage`, taking a slipstream from the end of a line, cooling it, and
  delivering it into the return header. It occupies the same hydraulic slot as a bypass,
  so it also provides the minimum-flow path.
- **Its duty is asserted, not solved.** The chilled-water loop has exactly one free
  energy variable and that is the central machine. `Q_evap_kW` is therefore required,
  which is also the honest model, because how hard a distributed plant runs is a
  dispatch decision.
- **`dispatch: proportional`** makes what the plant *delivers* follow the district's
  load, while a store in series absorbs the difference from the flat duty it aims its
  chiller at. The machine runs level while its output follows demand, which is the
  reason to put a store at a satellite at all.
- **A balancing valve** on the plant leg, free by default, so a fork carrying a
  satellite is not automatically over-determined.
- Reported through `Branch.satellite_report()` and the result columns
  `satellite_Q_evap_W`, `satellite_delivered_Q_W`, `satellite_storage_Q_W`,
  `total_compressor_power_W`, `total_cooling_produced_W` and `fleet_cop`.

### Added: network layout plots

- **`discoolpy.layout`**, with `plot_network(system)` and `compute_layout(system)`.
  Chillers are squares, cooling towers octagons, cold storage circles, buildings
  triangles, splits and merges points, pumps diamonds, and pipes lines, supply and
  return drawn as a parallel pair.
- **Plan geometry from the scenario.** Pipes take a `heading_deg` (a bearing
  counter-clockwise from east) alongside the `L` the solver already needs, and branches
  take an `origin_m`. The drawing is to scale, the axes are in metres, and it needs no
  solved network, the layout is a property of the scenario.
- **Scenarios with no geometry still draw.** Unknown lengths fall back to a nominal
  segment and terminals fan out around their parent's heading;
  `NetworkLayout.geometry_is_complete` and the plot title both say so.
- `validate_length_pipe_design.py --layout` writes one drawing per scenario.

### Changed: structural checking

- **`hydraulics.py` works on trees.** `BranchTopology`/`TerminalTopology` describe a
  whole network with no TESPy involved; `analyse_network_topology()` counts loops and
  finds redundancies across it. `analyse_pressure_topology()` is now a thin wrapper for
  the single-branch case and behaves exactly as before.
- **Forks are diagnosed explicitly.** A fork puts its terminals in parallel between one
  pair of nodes, so `k` terminals add `k − 1` loops, which is why a scenario that solves
  as a single street with `bypass_dp: 0.0` stops solving the moment it forks with both
  bypasses pinned. The report carries `n_forks` and the message names the trap and its
  fix.
- **`auto_relax_pressure` now frees terminals as well as building legs**, preferring
  building legs, and records what it released in `system.notes`. It still never touches
  pipe geometry.
- **`validate_thermal_degrees_of_freedom` counts pipes across the whole tree.**
- **New public helper `network_topology(config)`** returns a scenario's pressure graph
  so it can be checked in milliseconds before any solve.

### Changed: reporting

- `Branch.heat_gain_report()`, `pressure_feasibility()`, `apply_ambient()`,
  `set_design()` and `add_to_network()` now cover the whole subtree. Root-branch pipe
  keys stay bare (`supply_1`); descendants are namespaced (`north spur/supply_1`), so
  single-branch scenarios keep their exact result columns.
- The chilled-water closure identity now includes distributed plants:
  `Q_central + Q_satellite − Q_satellite_store = Q_buildings + Q_pipes + P_pump +
  Q_central_store`. `chw_energy_residual_W` still closes to under 1 W.
- `DistrictCoolingSystem` gained `.branches`, `.satellite_plants`, `.chillers` and
  `.branch_spec`. It is still iterable as the original six-tuple.

### Added: scenario, example, notebook

- **`configs/config_branching_grid.yaml`**: six buildings, four branches, two forks and
  all three terminal types, with full plan geometry.
- **`examples/branching_network_example.py`**: runs it, draws it, and compares it
  against the same network with a plain bypass in the satellite's place.
- **`examples/branching_networks.ipynb`**: reads the tree, breaks its degrees of
  freedom on purpose, draws it, solves it, and reports heat gain branch by branch.

### Documentation

Rewritten and extended so it covers the whole tool rather than the most recent change:

- New: **[Configuration reference](docs/configuration.md)** (every YAML key),
  **[Network topology](docs/network_topology.md)**, **[Network layout plots](docs/layout.md)**,
  **[Examples and scenarios](docs/examples.md)**.
- Rewritten: index, installation, getting started, API reference.
- Updated: pipe parameter guide (bearings, branch-local pipe keys, what a fork changes),
  heat gains (inheritance and per-branch reporting), README.

### Tests

- `tests/test_branching.py` (38), fork arithmetic, scenario parsing and its rejections,
  design mass flows over a tree, assembly, the energy balance with a satellite in the
  loop, and a measured check that a satellite takes 180 kW off the central plant.
- `tests/test_layout.py` (19), the marker convention, geometry in metres, the fallback
  when a scenario has none, and that every shipped scenario draws.
- 266 tests in total, up from 209.

---

## 0.2.0b1: heat gains and flexibility

The theme of this release: **a district-cooling network is colder than everything
around it, so it gains heat everywhere**, and until that is accounted for you cannot
honestly assess how much flexibility the system has.

### Fixed

- **`configs/config_length_pipes.yaml` and `configs/config_length_derived_pr.yaml` could
  not be solved.** Both over-specified the branch pressure drops (7 fixed elements where
  the topology allows 6), and `config_length_pipes.yaml` was simultaneously
  under-specified, `return_1` and `return_2` had no pressure specification at all, so
  the whole ladder floated. TESPy rejected both before the first Newton iteration, on
  the pinned 0.9.16 as well as on 0.11. This meant **the flagship storage-comparison
  example could not be run by anyone who cloned the repository.**
- **`examples/storage_comparison_example.py` pointed at
  `../configs/riyadh_three_building_length_derived_pr.yaml`**, which is not in the
  repository. The default path was also relative to the caller's working directory.
- **`ensure_output_dir` defaulted to `/home/ubuntu/yaml_example_outputs`.** Relative
  output directories now resolve against the *scenario file's* directory, so a scenario
  writes to the same place regardless of where it is launched from.
- **`chiller.native_offdesign.condenser_ttd_u` was accepted from YAML and silently
  discarded** (the docstring admitted it was "retained for API documentation").
  It now takes effect, as an *alternative* to fixing the design condensing temperature,
  specifying both over-determines the cycle.
- **`evaporator_ttd_l: null` was forwarded as `set_attr(ttd_l=None)`.** Now guarded.
- **The tutorial notebook did `sys.path.insert(0, "../src")`**, a directory that has
  never existed, and used the `kA` name that TESPy 0.11 deprecates.
- **Version claims.** README and docs advertised TESPy ≥ 0.7; the code has always needed
  ≥ 0.9.16 (`Network(iterinfo=…)`, `nw.units.set_defaults`, `Valve.dp`). Now tested on
  both 0.9.16 and 0.11.0.
- **`make_riyadh_weather_and_load_profiles` raised unless the scenario had exactly three
  buildings.** It now works for any number and is aliased from
  `make_weather_and_load_profiles`.

### Added: heat gains

- **`thermal.py`**: heat-transfer physics with no TESPy dependency, so it is directly
  unit-testable: buried and exposed pipe conductances from geometry and insulation, tank
  UA, building envelope gains, LMTD-to-ambient, and a Kusuda soil-temperature harmonic.
- **Pipe heat models.** Per-pipe `heat_model`: `adiabatic` (the old behaviour),
  `fixed`, `ua` (recommended, DisCoolPy derives `UA` from geometry and TESPy computes
  the gain from the actual water temperature each snapshot), or delegation to TESPy's
  native buried/surface pipe groups. Buried pipes track **soil** temperature, exposed
  pipes track air; a network can mix both.
- **Building envelopes and thermal mass.** `load_model: envelope` adds
  `UA·(T_air − T_indoor) + solar + infiltration`; `load_model: thermal_mass` adds a
  first-order zone model with a comfort band, integrated exactly. Control modes
  `track` / `precool` / `coast` / `recover` / `manual`. Comfort is a hard constraint, and
  breaches are reported (`band_violation_K`, `capacity_limited`) rather than absorbed
  silently.
- **Ambient-driven storage losses.** `loss_model: ua` replaces the constant
  `standby_loss_fraction_per_day` with `UA·(T_ambient − T_store)`. For a typical ice tank
  in Riyadh this is more than twice the flat assumption at the afternoon peak, and the
  loss is largest exactly when the stored cooling is worth most. Sensible chilled-water
  stores can warm as they discharge (`temperature_varies_with_soc`).

### Changed: how the plant duty is determined

Once anything else adds heat to the chilled-water loop, the loop energy balance already
determines the chiller duty, so asserting it as well over-determines the network.
DisCoolPy now switches automatically to `plant_control: supply_temperature`, releases the
evaporator duty and solves it. Every result row carries `chw_energy_residual_W`, which
must be numerically zero.

This replaces a hidden slack variable. The original scenarios left one pipe without an
energy specification, commented "Q not needed for the last return pipe". In
`config_length_pipes.yaml` the scenario asserted a 650 W pump while the pump actually
drew 1168 W, and the 518 W difference was silently reported as **−518 W of pipe
*cooling***, with nothing in the output to flag it, because pipe duties were never
reported at all.

### Changed: how storage couples to the network

`coupling: hydraulic` (recommended) makes the store a real `SimpleHeatExchanger` in the
plant section of the chilled-water loop, carrying `Q = charge − discharge`. Charging then
forces the plant to produce water *below* the distribution setpoint, which depresses the
evaporating temperature and costs COP, the penalty that makes ice storage interesting in
the first place.

`coupling: supervisory` (the previous behaviour) is retained but documented as an
approximation: it kept the network balanced by **rescaling every building's heat duty**,
so the building heat exchangers were no longer serving their real demand and the reported
chilled-water return temperature was not the return temperature of the real system.

### Added: structural checking

- **`hydraulics.py`.** A branch with `n` buildings has `3n + 1` pressure-carrying
  elements, `2n` unknown nodes and `n + 1` loops, so exactly `2n` may fix a drop. The
  checker counts this, names the redundant element, and suggests a valid specification,
  instead of TESPy's fifteen-variable "circular dependency" dump. It also checks energy
  degrees of freedom, and `Branch.pressure_feasibility()` flags any element whose solved
  pressure *rises* along the flow, which means the pump cannot serve the far end at
  design flow.
- `branch.auto_relax_pressure: true` frees the minimum number of building legs needed to
  make an over-determined branch well-posed. It never touches pipe geometry.

### Added: flexibility assessment

**`flexibility.py`** separates peak reduction, energy penalty, thermal round-trip and
**electric** round-trip efficiency, plus cost and carbon against a tariff. Round trips
are corrected for any net change in state of charge over the horizon, and the electric
figure is withheld when the horizon is not reasonably cyclic. `flexibility_envelope()`
reports the deliverable up/down cooling power right now, respecting comfort bands, SOC
and power ratings.

### Added: scenarios, examples, tests

- `configs/config_riyadh_heat_gains.yaml`: buried mains plus a hydraulically coupled ice
  store, one week of Riyadh July.
- `configs/config_precooling_flexibility.yaml`: building envelopes and thermal mass;
  demand-side flexibility with no store.
- `configs/config_campus_five_buildings.yaml`: five buildings, mixed buried and
  above-ground pipes, sensible chilled-water store.
- `examples/precooling_flexibility_example.py`, and three new notebooks:
  `heat_gains_and_flexibility.ipynb`, `precooling_flexibility.ipynb`,
  `building_your_own_scenario.ipynb`.
- `examples/validate_length_pipe_design.py` rewritten as a full design-point check
  (degrees of freedom, conductances, energy balance, hydraulic feasibility) with `--all`.
- **`tests/`: 209 tests, where there were none.** Closed-form physics checks, exact RC
  integration and comfort-band enforcement, storage SOC conservation, flexibility metric
  arithmetic, and an end-to-end check that every shipped scenario converges with an
  energy balance closing to under 1 W. The regression tests reproduce the exact
  specifications that shipped broken.

### Backward compatibility

A scenario that does not mention heat gains behaves exactly as it did before: every new
section defaults off, `build_standard_branch_system()` still returns the original
six-tuple, and `config_tutorial.yaml` reproduces its previous design point (300.30 kW,
COP 4.70: the third decimal moves only because `condenser_ttd_u` is now honoured).
