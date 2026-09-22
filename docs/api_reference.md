# API Reference

Everything importable from `discoolpy`. Grouped by module; every name listed here is exported from the package root, so `from discoolpy import X` always works.

For the YAML side of the same objects see the [Configuration reference](configuration.md).

---

## Package layout

```
discoolpy/
├── branch.py          Branch, Terminal, EndBypass, SatellitePlant, SubBranch
├── building.py        Building, ThermalMass, ThermalMassState
├── chiller.py         Chiller
├── cooling_tower.py   CoolingTower
├── cold_storage.py    ColdStorage, StorageDispatchResult
├── stratified.py      StratifiedTank, TankStepResult (no TESPy dependency)
├── thermal.py         heat-transfer physics (no TESPy dependency)
├── hydraulics.py      pressure and energy degree-of-freedom checking
├── layout.py          plan-view layout and plotting
├── reporting.py       printed reports and result figures, per component
├── errors.py          DisCoolPyError, ScenarioError
├── flexibility.py     flexibility metrics and envelopes
├── time_snapshot.py   TimeSnapshot, SnapshotSchedule
├── config_schema.py   YAML -> objects
├── scenario.py        run_scenario, check_scenario: the pipeline in one call
├── cli.py             the `discoolpy` command
└── utils.py           orchestration: assembly, profiles, running, reporting
```

---

## The short way in (`scenario.py`)

Most work goes through these two. Both take a path or an already-loaded config dictionary.

### `run_scenario(config, periods=None, compare_storage=None, progress=True, plot=True, report=False, report_dir=None) -> ScenarioResult`

Run a scenario end to end: generate the profile, solve the design point, step through every snapshot, write the results into the scenario's `outputs.output_dir`.

| Argument | Meaning |
|---|---|
| `config` | Path to a YAML scenario, or a config mapping |
| `periods` | Override `profiles.periods`, for a shorter run |
| `compare_storage` | Run without and with the store, and assess the difference. Defaults to True when the scenario enables a store |
| `progress` | Print per-snapshot progress |
| `plot` | Write the comparison figure. Paired runs only |
| `report` | Also write the per-component result figures and the printed report |
| `report_dir` | Where `report` writes. Defaults to `figures/` beside the other output |

Comparing a scenario that has no store raises `ValueError`. Structural warnings from the scenario file are re-raised through `warnings.warn` and kept on the result.

### `ScenarioResult`

| Attribute | Type | Meaning |
|---|---|---|
| `name` | `str` | `metadata.name`, else the file stem |
| `config` | `dict` | The config as run, including any override |
| `profile` | `DataFrame` | The weather and demand profile |
| `cases` | `dict[str, DataFrame]` | `base`, or `without_storage` and `with_storage` |
| `results` | `DataFrame` | The case you most likely mean: the stored one if there is one |
| `paired` | `bool` | Whether both storage cases were run |
| `report` | `FlexibilityReport` or `None` | The assessment, on a paired run |
| `files` | `dict[str, Path]` | Every file written |
| `system` | `DistrictCoolingSystem` | The solved network behind `results`, kept so the report step costs no second solve |
| `warnings` | `list[str]` | Structural warnings raised while the scenario was read |
| `output_dir` | `Path` | Where this scenario writes |
| `summary()` | `str` | A few lines of plain text |
| `print_report()` | `SystemReport` | Print the per-component report and return it |
| `plot_all(output_dir=None, **kwargs)` | `dict[str, Path]` | Write every result figure. Defaults to `figures/` beside the other output |

### `check_scenario(config, layout_path=None, verbose=True, strict=False) -> CheckResult`

Solve the design point and report on the scenario without touching the time series. Covers, in the order a scenario usually goes wrong: the structure of the file itself, the branch tree, the pressure degrees of freedom, the pipe heat-gain conductances, the design solve, the plant energy balance, the operating point, hydraulic feasibility, and the solved pressures branch by branch. It ends with a verdict, whichever stage it stopped at, and names the next command when it passed.

A scenario that cannot be read at all comes back as a falsy `CheckResult` carrying the `ScenarioError`, rather than raising: reporting problems is what the step is for.

Pass `layout_path` to draw the plan view too, `strict` to treat structural warnings as failures, and `verbose=False` to collect the report without printing it.

Run this before `run_scenario`. A scenario that fails here fails once per snapshot afterwards.

### `CheckResult`

| Attribute | Type | Meaning |
|---|---|---|
| `name` | `str` | Scenario name |
| `ok` | `bool` | Whether the scenario is usable. The object is truthy on the same test |
| `lines` | `list[str]` | The report, one line per entry. `str(result)` joins them |
| `system` | `DistrictCoolingSystem` or `None` | The solved system, when it got that far |
| `layout_path` | `Path` or `None` | Where the plan view was written |
| `warnings` | `list[str]` | Structural warnings raised while the scenario was read |
| `error` | `ScenarioError` or `None` | What went wrong, when the file could not be read at all |

### `load_scenario(config, validate=True, strict=False) -> dict`

Accept a path or a mapping and return a mutable config dictionary. Use it to edit a scenario from Python rather than in the file. Validates the structure on the way in and attaches the warnings as `_warnings`; pass `validate=False` for a config assembled in code that deliberately carries keys of its own.

---

## Errors (`errors.py`)

Everything DisCoolPy raises on its own account inherits from `DisCoolPyError`, so a study driving many scenarios can tell "this scenario is wrong" from "this tool is broken" without matching on message text.

| Name | Meaning |
|---|---|
| `DisCoolPyError` | Base class |
| `ScenarioError` | A scenario file that cannot be used as written. Carries `problem`, `where` (the key path, as `buildings[0].Q_design_W`), `hint`, `path` and `extra`, and renders them as the command line prints them |

### `validate_scenario(config, strict=False) -> list[str]`

Check a scenario's structure before anything is built from it: misspelled keys, sections written as the wrong shape, buildings with no label or no load. Returns the warnings it did not consider fatal.

A key close to a real one raises `ScenarioError` naming both, because the scenario it produces is not the one that was written. A key close to nothing is a warning, because scenarios carry notes and private annotations. `strict=True` turns every warning into an error.

`SCENARIO_KEYS` is the map of what a scenario may contain. Every shipped scenario and template is tested against it.

### `building_design_load_W(item) -> float`

One building's design cooling load in watts, from either `Q_design_W` or `Q_design_kW`. Giving both is refused by `validate_scenario`.

---

## Command line (`cli.py`)

`discoolpy new | check | plot | run | report | list`. See [Command line](cli.md). `discoolpy.cli.main(argv)` runs it in process and returns the exit code, which is what the tests use.

---

## Orchestration (`utils.py`)

### `build_system(config, strict_hydraulics=True) -> DistrictCoolingSystem`

Build the whole network from a scenario. Runs the structural checks first, then assembles the TESPy network, applies design specifications, and seeds starting values.

`strict_hydraulics=False` downgrades an ill-posed pressure specification from an exception to a warning. That is what `discoolpy plot` uses, so a scenario can be drawn before it converges.

### `DESIGN_DEFAULTS`

The fallback values for the `design` section, as a dict. A scenario that states a key overrides its fallback; a key in neither place raises.

### `default_pump_power_W(config) -> float`

The design pump shaft power that will actually be used: the scenario's `design.pump_power_W` if it has one, otherwise the flow sized against `NOMINAL_PUMP_HEAD_BAR`. See [Configuration reference](configuration.md#pump_power_w).

### `DistrictCoolingSystem`

| Attribute | Type | Meaning |
|---|---|---|
| `.network` | `tespy.networks.Network` | |
| `.chiller` | `Chiller` | The central plant |
| `.cooling_tower` | `CoolingTower` | Its condenser-water loop |
| `.buildings` | `list[Building]` | Every building, in scenario order |
| `.branch` | `Branch` | The **root** branch |
| `.branches` | `list[Branch]` | Every branch, root first, depth-first by terminal order |
| `.satellite_plants` | `list[SatellitePlant]` | Every distributed plant |
| `.chillers` | `list[Chiller]` | Central plant first, then every satellite's |
| `.storage` | `ColdStorage \| None` | The central store |
| `.chiller_to_closer` | `Connection` | |
| `.plant_control` | `str` | `"supply_temperature"` or `"evaporator_duty"` |
| `.notes` | `list[str]` | Decisions DisCoolPy made and thinks you should know about |
| `.branch_spec` | `BranchSpec` | The normalised scenario tree |
| `.building_map` | `dict[str, Building]` | By label |

Iterable as the legacy six-tuple `(network, chiller, buildings, branch, cooling_tower, chiller_to_closer)`.

### Other functions

| Function | Returns |
|---|---|
| `load_yaml_config(path)` | `dict`. Raises `FileNotFoundError` with the resolved path. |
| `build_standard_branch_system(config)` | The legacy six-tuple. |
| `network_topology(config)` | `BranchTopology`, the pressure graph, with no TESPy involved. |
| `network_design_mass_flows(config, root=None, building_flows=None)` | Per-branch design flows: `{"pipe": {...}, "buildings": [...], "terminals": {...}, "end_of_line": float, "inlet": float}`, keyed by branch label plus `""` for the root. |
| `compute_building_mass_flows(config)` | Design mass flow per building, in scenario order. |
| `total_design_mass_flow(config)` | Flow entering the root branch. |
| `design_evaporator_load(config)` | Design plant duty used to size the chiller. |
| `make_pipe_attrs(config)` | TESPy pipe attributes for the **root** branch. |
| `make_network_pipe_attrs(config)` | The same for every branch, keyed by label. |
| `branch_pipe_attrs(spec, config, design_flows=None)` | For one branch. |
| `branch_pipe_lengths(spec)` / `branch_pipe_headings(spec)` | Layout metadata for one branch. |
| `check_stratified_temperatures(config, storage)` | Warn when a tank's temperatures cannot be reached by the network. Returns the notes. |
| `make_chiller(cfg, label, Q_evap_W, native_offdesign_default=True)` | A `Chiller` from a `chiller` mapping. |
| `make_cooling_tower(cfg, config, label)` | A `CoolingTower`, falling back to `design` values. |
| `make_weather_and_load_profiles(config)` | A profile `DataFrame` for any number of buildings. |
| `make_riyadh_weather_and_load_profiles(config)` | Alias, kept for older scripts. |
| `make_schedule_from_profile(config, profile_df, name)` | A `SnapshotSchedule`. |
| `run_configured_case(config, profile_df, case, use_storage, progress=True)` | Result `DataFrame`, one row per snapshot. |
| `dispatch_satellite_plants(branch, snapshot, design_building_load_W=None)` | Sets every satellite's duty and its store's for one snapshot. |
| `ensure_output_dir(config)` / `output_path(config, key, default)` | Paths, resolved against the scenario file's directory. |
| `make_storage_from_config(config)` | A `ColdStorage` or `None`. |
| `write_storage_comparison_summary(config, no_storage, with_storage)` | Comparison CSV + Markdown summary. |
| `make_storage_comparison_plot(config, no_storage, with_storage)` | Comparison PNG. |
| `OCCUPANCY_ARCHETYPES` | `dict` of the six load-shape archetypes. |

---

## `Branch` (`branch.py`)

One street: supply/return pipes, an optional pump, splitter taps, building substations, and one or more end terminals. Branches nest, so a `Branch` is really a subtree.

```python
Branch(
    label,
    buildings=(),               # ordered; may be empty (a trunk link)
    terminals=None,             # default: one EndBypass from bypass_m/bypass_dp
    bypass_m=None,
    pump_placement="supply_inlet",   # or "return_outlet" or None
    pump_label=None,
    uniform_pipes=False,
    uniform_pipe_attrs=None,
    pipe_attrs=None,            # {local key: TESPy attrs}
    pipe_thermal=None,          # {local key: PipeThermal}
    bypass_dp=0.0,
    pipe_headings=None,         # {local key: bearing in degrees CCW from east}
    pipe_lengths=None,          # {local key: metres}
    origin=None,                # (x, y) of the branch inlet, metres
    heading_deg=0.0,
)
```

### Topology

| Member | Returns |
|---|---|
| `n_buildings`, `n_pipes_per_side`, `is_link` | |
| `walk()` | This branch and every descendant, depth-first in terminal order. |
| `sub_branches()` | Immediate children. |
| `all_buildings()` | Every building in the subtree, branch by branch, in supply order. |
| `satellite_plants()` | Every `SatellitePlant` in the subtree. |
| `pipe_items()` | `(local_key, Pipe)` for this branch. |
| `tree_pipe_items()` | `(qualified_key, Pipe)` across the subtree. Root keys bare, descendants prefixed with their branch label. |
| `end_supply_port()` / `end_return_port()` | The `(component, port)` the terminals attach to. |
| `bypass` | The first `EndBypass`'s valve, or `None`. Back-compatibility. |
| `connections` | `dict[str, Connection]` for this branch. Notable keys: `branch_in`, `branch_out`, `building_i_in/out`, `terminal_i_*`, and `bypass_in`/`bypass_out` when there is exactly one bypass. |
| `own_connections()` | This branch's connections, deduplicated. |
| `all_connections()` | The whole subtree's, deduplicated. |

### Assembly

| Method | Effect |
|---|---|
| `connect_between(src, src_port, sink, sink_port, inlet_label=None, outlet_label=None)` | Creates every connection in the subtree. |
| `add_to_network(nw)` | Adds the subtree's connections and every satellite chiller subsystem. |
| `set_design(pump_attrs=None, building_mass_flows=None, native_offdesign=False)` | Applies pipe, pump, building and terminal design attributes. Recurses. |
| `set_connection_start(key, m, T, p)` | Seeds one named connection. |

### Operation and reporting

| Method | Returns |
|---|---|
| `apply_ambient(ground_temperature_degC=None, air_temperature_degC=None)` | `{qualified_key: applied_T}`. Each pipe follows its own `ambient_source`. |
| `apply_snapshot(snapshot)` | Pushes weather onto every satellite's condenser-water loop. |
| `heat_gain_report()` | Subtree-wide. `per_pipe_W`, `supply_heat_gain_W`, `return_heat_gain_W`, `total_heat_gain_W`, `network_UA_W_K`, and `per_pipe` carrying each pipe's solved `Q_W`, `UA_W_K`, `UA_per_m_W_mK`, `lmtd_K`, `T_in_degC`, `T_out_degC`, `length_m`, `model`, `ambient_source`, `ambient_temperature_degC`, `side` and `branch`. Every figure is read back off the solved component, for all four heat models. |
| `satellite_report()` | Per-plant `Q_evap_W`, `compressor_power_W`, `cop`, `heat_rejection_W`, `storage_Q_W`, `delivered_Q_W`, `storage_soc`, plus totals. |
| `pressure_feasibility(tolerance_bar=1e-6)` | Flags any element whose solved pressure *rises* along the flow, the signature of an undersized pump. |
| `estimated_heat_gain_W(T_supply, T_return)` | Solver-independent estimate, for sanity checks. |

---

## Terminals (`branch.py`)

What closes the end of a branch. All three span the branch's end-of-line supply and return nodes.

### `EndBypass(label, m=None, heading_deg=None, dp=0.0)`

An end-of-line valve. `dp=None` leaves the drop free, what a real balancing valve does, and required once a branch forks.

### `SatellitePlant(label, m, heading_deg=None, chiller, cooling_tower, storage=None, balancing_dp=None, dispatch="fixed", min_load_fraction=0.2, design_Q_evap_W=None)`

A distributed plant. `chiller` and `cooling_tower` are required; `storage` sits in series downstream of the evaporator.

| Method | Meaning |
|---|---|
| `delivered_duty_W(load_fraction=1.0)` | Duty it should hand the district at this snapshot. |
| `apply_dispatch(delivered_W, storage_Q_W=0.0)` | Sets the evaporator duty to `delivered + stored` and returns it. |
| `apply_snapshot(snapshot)` | Weather onto its own tower. |

### `SubBranch(branch, label=None, m=None, heading_deg=None)`

Another `Branch`, fed from the end of this one.

### `Terminal` (base)

`label`, `m`, `heading_deg`, `connections`, `kind`; `pressure_spec()`, `release_pressure_spec()`, `inlet_connection()`, `outlet_connection()`, `start_values(...)`, `sub_branches()`.

---

## `Building` (`building.py`)

```python
Building(label, Q_design, pr=None, demand_profile=None,
         load_model="profile", envelope=None, thermal_mass=None)
```

| Member | Meaning |
|---|---|
| `component` / `heat_exchanger` | The TESPy `SimpleHeatExchanger`. |
| `inlet`, `outlet` | Its connections, after `connect_between`. |
| `indoor_temperature_degC` | Current zone temperature under a thermal-mass model. |
| `last_breakdown` | Conduction / solar / infiltration / internal split of the last applied demand. |
| `last_mass_state` | The last `ThermalMassState`. |
| `set_design(mass_flow=None, native_offdesign=False)` | |
| `set_demand(q_W)` / `set_hourly_demand(i)` / `set_snapshot_demand(snapshot)` | |
| `compute_demand(...)` | The full envelope + thermal-mass calculation. |
| `flexibility_W(...)` | Shed and pre-cool capability at this instant. |
| `load_hourly_demand_from_csv(path, column="Q", ...)` | |
| `set_start(m, T_in, T_out, p_in, p_out)` | |

### `ThermalMass` / `ThermalMassState`

First-order RC zone model with a comfort band. `ThermalMass.step(...)` integrates one snapshot exactly; the returned `ThermalMassState` carries `mode`, `setpoint_deviation_K`, `stored_energy_change_kWh`, `band_violation_K` and `capacity_limited`.

---

## `Chiller` (`chiller.py`)

A TESPy `Subsystem` holding a full vapour-compression cycle. Ports `in1`/`out1` are chilled water, `in2`/`out2` condenser water.

```python
Chiller(label, T_evap=2, T_cond=40, eta_s=0.75, Q_evap=50000,
        refrigerant="R134a", pr_evap_1=1.0, pr_evap_2=1.0,
        pr_cond_1=1.0, pr_cond_2=1.0, **attributes)
```

| Member | Meaning |
|---|---|
| `evaporator`, `condenser`, `compressor` | The TESPy components. |
| `internal_connections` | `{"chw_in", "chw_out", "cw_in", "cw_out", "refrigerant_after_*"}`. |
| `solved_Q_evap_W` | Positive evaporator duty after a solve. |
| `update_Q_evap(new_Q_W)` | Assert a new duty. |
| `release_Q_evap()` | Free the duty so the loop balance determines it. |
| `set_plant_control(mode, Q_evap_W=None)` | |
| `configure_native_offdesign(evaporator_ttd_l=None, condenser_ttd_u=None, use_pressure_loss_characteristics=True)` | |
| `enable_compressor_characteristic_extrapolation()` | Needed after every design-state reload. |
| `import_from_csv` / `import_from_json` / `export_to_csv` / `export_to_json` | Certification performance data. |
| `set_performance_data` / `get_performance_data` / `list_performance_data` | |
| `get_summary()` | |

---

## `CoolingTower` (`cooling_tower.py`)

```python
CoolingTower(label, T_in_chiller=30.0, T_out_chiller=35.0, p_in_chiller=3.0,
             fluid=None, pr=None, ambient_temperature=26.0, approach_temperature=4.0)
```

| Member | Meaning |
|---|---|
| `connect_to_chiller(chiller, condenser_in_port="in2", condenser_out_port="out2", ...)` | Builds the closed condenser-water loop. |
| `connections` | The three loop connections. |
| `set_design(native_offdesign=False)` | |
| `set_offdesign_ambient(ambient_temperature=None, condenser_inlet_temperature=None)` | |
| `apply_snapshot(snapshot)` | |
| `set_start(m=35.0, pr_start=0.999)` | |
| `heat_rejection` | Positive tower duty in W after a solve. |

---

## `ColdStorage` (`cold_storage.py`)

```python
ColdStorage(label, capacity_kWh, initial_soc, max_charge_kW, max_discharge_kW,
            charge_efficiency, discharge_efficiency, standby_loss_fraction_per_day,
            thermal=None, loss_model="fraction", coupling="supervisory",
            hydraulic_pr=0.999, storage_type="ice", target_chiller_load_kW=None,
            min_soc=0.08, max_soc=0.97,
            charge_allowed_above_degC=None, discharge_allowed_below_degC=None)
```

| Member | Meaning |
|---|---|
| `soc`, `energy_kWh`, `min_energy_kWh`, `max_energy_kWh` | |
| `dispatch(snapshot, base_chiller_load_W=None, target_chiller_load_W=None, ...)` | Advances one step, returns a `StorageDispatchResult`. |
| `dispatch_and_make_effective_snapshot(...)` | Supervisory coupling: also returns the rescaled snapshot. |
| `create_hydraulic_element()` / `heat_exchanger` | The `SimpleHeatExchanger` representing the store in the loop. |
| `connect_between(source, source_port, sink, sink_port, ...)` | Insert it in series. |
| `set_design(idle_Q_W=0.0)` | |
| `apply_dispatch_to_network(result)` | Push a dispatch decision onto the element. |
| `ambient_heat_gain_W(T_ambient)` | |
| `thermal_from_geometry(...)` | Derive a `StorageThermal` from tank dimensions. |
| `reset(soc=None)` | |
| `history_records(prefix="storage")` | |

### Stratified tanks

Set `ColdStorage.stratified` (or `storage.stratified.enabled` in YAML) to replace
the scalar reservoir with a layered tank. See
[Cold storage](cold_storage.md#stratified-tanks).

```python
StratifiedTank(
    volume_m3=None, height_m=None, diameter_m=None, height_to_diameter=2.5,
    layers=12, charged_temperature_degC=5.0, discharged_temperature_degC=13.0,
    initial_soc=0.5, wall_htc_W_m2K=0.28, vertical_conductivity_W_mK=0.897,
    cp_J_kgK=4180.0, rho_kg_m3=999.0, max_courant=0.5, max_sub_steps=4000,
)
```

| Member | Purpose |
|---|---|
| `step(duration_h, mass_flow_kg_s, inlet_temperature_degC, mode, ambient_temperature_degC)` | Advance one interval. `mode` is `"charge"`, `"discharge"` or `"idle"`. Returns a `TankStepResult`. |
| `mass_flow_for_power(power_W, inlet_temperature_degC, mode)` | Flow needed for a power against the *current* profile; `inf` once the span collapses. |
| `reset(soc=None)` | Set the profile to a state of charge, with a sharp front. |
| `energy_kWh`, `capacity_kWh`, `soc` | State. Capacity follows from geometry and the temperature span. |
| `cold_port_temperature_degC`, `warm_port_temperature_degC` | The bottom and top ports. |
| `thermocline_thickness_m(low=0.2, high=0.8)` | Transition thickness, metres. |
| `temperatures_degC` | Raw profile, bottom first. |
| `profile()` | Layer records: height, temperature, charged fraction. |
| `describe()` | Geometry, capacity and shell UA. |
| `layer_height_m`, `cross_section_m2`, `layer_mass_kg`, `total_mass_kg` | Derived geometry. |

`TankStepResult` carries `mode`, `mass_flow_kg_s`, `duration_h`,
`outlet_temperature_degC`, `inlet_temperature_degC`, `charge_power_W`,
`discharge_power_W`, `loop_heat_W`, `ambient_heat_gain_W`, the energy and SOC
before and after, `thermocline_thickness_m`, `profile_degC` and `sub_steps`;
plus `to_record(prefix="tank")`.

`StorageDispatchResult` carries `mode`, `storage_power_W`, `charge_power_W`, `discharge_power_W`, `soc_after`, `energy_after_kWh`, `chiller_load_offset_W`, `curtailed_request_W`, `ambient_heat_gain_W`, `fractional_loss_kWh` and `medium_temperature_degC`; and,
for a stratified tank, `tank_mass_flow_kg_s`, `tank_outlet_temperature_degC`,
`tank_inlet_temperature_degC`, `cold_port_temperature_degC`,
`warm_port_temperature_degC`, `thermocline_thickness_m` and `tank_profile_degC`.

---

## `TimeSnapshot` and `SnapshotSchedule` (`time_snapshot.py`)

### `TimeSnapshot`

One time step: `timestamp`, `resolution`, `building_loads`, `ambient_temperature`, `ground_temperature`, `solar_irradiance_W_m2`, `condenser_inlet_temperature`, `metadata`.

`apply(buildings, chiller=None, cooling_tower=None, update_chiller=True, update_cooling_tower=True, chiller_load_offset_W=0.0, branch=None, storage=None, storage_result=None)` pushes the snapshot onto the model and returns the total *applied* building duty. Passing `branch` also updates every pipe's ambient temperature and every satellite's condenser-water loop.

### `SnapshotSchedule`

An ordered sequence. `from_profile_arrays(...)`, `from_csv(...)`, `apply(...)`, `records()`; iterable, indexable, sized.

---

## Structural checking (`hydraulics.py`)

| Name | Purpose |
|---|---|
| `BranchTopology` | A branch's pressure graph: `label`, `building_pr`, `pipe_attrs`, `terminals`. TESPy-free. |
| `TerminalTopology` | One terminal: `key`, `kind`, `specified`, `spec`, `releasable`, `child`. |
| `PressureEdge` | One element: `key`, `node_a`, `node_b`, `specified`, `spec`. |
| `analyse_network_topology(root, plant_closed_loop=True)` | Full report for a branching network. |
| `analyse_pressure_topology(n, building_pr, pipe_attrs, bypass_fixed=True, plant_closed_loop=True)` | The single-branch wrapper. |
| `validate_network_topology(root, strict=True, ...)` | Raise or warn. |
| `validate_pressure_topology(...)` | Single-branch wrapper. |
| `resolve_network_pressure_specification(root, plant_closed_loop=True)` | `(actions, notes)`. Frees the minimum number of balancing valves. |
| `resolve_pressure_specification(...)` | Single-branch wrapper; returns `(building_pr, notes)`. |
| `suggest_pressure_specification(n, bypass_fixed=True)` | A valid single-branch specification. |
| `validate_thermal_degrees_of_freedom(pipe_keys, pipes_with_energy_spec, plant_control)` | The energy count. |
| `build_network_pressure_edges(root)` / `build_pressure_edges(...)` | The edge list. |

`PressureTopologyReport` carries `ok`, `message()`, `n_buildings`, `n_branches`, `n_forks`, `n_nodes`, `n_edges`, `n_loops`, `n_specified`, `n_required`, `redundant`, `unanchored_nodes` and `edges`.

---

## Layout (`layout.py`)

| Name | Purpose |
|---|---|
| `plot_network(system_or_branch, ax=None, title=None, annotate=True, annotate_pipes=False, show_pumps=True, legend=True, scale_bar=True, save_path=None, dpi=160, layout=None, **layout_kwargs)` | Draw the network. Returns the matplotlib `Axes`. |
| `compute_layout(system_or_branch, storage=None, cooling_tower=None, chiller=None, default_segment_m=150.0, building_offset_m=None, pipe_offset_m=None, stub_m=None)` | The layout as data, no matplotlib. |
| `NetworkLayout` | `nodes`, `edges`, `assumed_lengths`, `assumed_headings`, `geometry_is_complete`, `bounds()`, `extent_m`, `nodes_of(kind)`, `to_records()`. |
| `LayoutNode` | `key`, `kind`, `label`, `x`, `y`, `branch`, `meta`, `marker`. |
| `LayoutEdge` | `key`, `kind`, endpoints, `label`, `branch`, `meta`, `length_m`. |
| `NODE_MARKERS` | The marker convention: chiller `s`, cooling tower `8`, storage `o`, building `^`, junction `.`, pump `D`. |

---

## Heat-transfer physics (`thermal.py`)

No TESPy dependency, so every function is directly testable in closed form.

| Function | Returns |
|---|---|
| `buried_pipe_UA_per_m(inner_diameter_m, insulation_thickness_m, insulation_conductivity, burial_depth_m, ground, pipe_wall_thickness_m=0.0, pipe_wall_conductivity_W_mK=45.0, internal_film_W_m2K=2000.0, twin_spacing_m=None)` | W/(m·K). Series of internal film, wall, insulation and the soil shape factor, with an optional twin-pipe correction. |
| `surface_pipe_UA_per_m(..., wind_velocity_m_s=1.0, emissivity=0.9, ambient_temperature_degC=35.0)` | W/(m·K), including convection and radiation. |
| `tank_UA_W_K(volume_m3, insulation_thickness_m, ...)` | W/K for a cylindrical tank, optionally part-buried. |
| `tank_surface_area_m2(volume_m3, height_to_diameter=1.0)` | |
| `soil_temperature_degC(day_of_year, depth_m, mean_annual_degC, annual_amplitude_K, ...)` | Damped, phase-lagged soil temperature. |
| `external_film_coefficient(...)`, `twin_pipe_correction(...)` | |
| `lmtd_to_ambient(T_in, T_out, T_ambient)` | |
| `TESPY_NATIVE_GROUND_MEDIA` | The four `environment_media` names TESPy's own buried group accepts. A scenario asking for anything else is refused at configuration time rather than mid-solve. |
| `TESPY_MIN_INSULATION_THICKNESS_M` | 0.001 m, below which TESPy's native groups refuse an insulation layer. |

Dataclasses: `PipeThermal` (`model`, `Q_W`, `UA_W_K`, `UA_per_m_W_mK`, `length_m`, `ambient_source`, `ambient_temperature_degC`, `native_attrs`, `geometry`; `resolved_UA()`, `heat_gain_W(...)`), `EnvelopeThermal` (`gain_W(...)`), `StorageThermal` (`medium_temperature_degC(soc)`, `heat_gain_W(...)`).

---

## Reporting (`reporting.py`)

Printed reports and result figures for any DisCoolPy system. Every function takes
the same `source`: a `ScenarioResult`, a `CheckResult`, a `DistrictCoolingSystem`,
a results `DataFrame`, a path to a results CSV, or a `(system, frame)` pair.
See [Printed and plotted results](reporting.md).

| Name | Returns |
|---|---|
| `report_system(source, results=None)` | `SystemReport`: every component section in reading order. `to_text()`, `to_dict()`. |
| `print_report(source, results=None)` | Prints it and returns it. |
| `report_balance(...)` | Where the plant's cooling goes, the parasitic shares, and the loop closure residual. |
| `report_pipes(...)` | Conductances and design duties from a system; gain energy and its share of demand from a frame. |
| `report_buildings(...)` | Delivered energy per building, the envelope split, comfort band violations. |
| `report_chiller(...)` | Duty, power, COP, distribution temperatures and delta-T. |
| `report_cooling_tower(...)` | Heat rejection, condenser water, and the condenser loop closure. |
| `report_storage(...)` | Charge, discharge, tank gain, state of charge, round trip. |
| `report_satellites(...)` | Per-plant duty and the central-against-fleet peak. |
| `ComponentReport` | `title`, `rows`, `data` (floats, not strings), `notes`, `add(...)`, `to_text()`. |
| `plot_system(source, results=None, title=None, axes=None, save_path=None, dpi=160)` | Six-panel dashboard of the whole district. A matplotlib `Figure`. |
| `plot_balance(...)` | What the plant removes, the parasitic shares, electricity, loop closure. |
| `plot_pipes(...)` | Design gain and `UA` per pipe, gain over the run, energy per pipe, temperature along the index run. |
| `plot_buildings(..., max_series=8)` | Duty per building, envelope split, indoor temperature against the comfort band, energy per building. |
| `plot_chiller(...)` | Cooling produced, electrical power, COP against ambient, power duration curve. |
| `plot_cooling_tower(...)` | Rejection, condenser temperatures, rejection against ambient, closure. |
| `plot_storage(...)` | Charge and discharge, state of charge, tank gain against ambient, thermocline. |
| `plot_all(source, output_dir, results=None, prefix="", dpi=160, layout=True, report=True, close=True)` | Writes all seven, plus the plan view and the printed report. `dict` of name to `Path`. |
| `compare_pipe_heat_models(...)` | Solves one pipe with the closed-form `UA` and with TESPy's native group, and returns both conductances, both duties, their `ratio`, and `native_available` with a `reason` where the native group cannot take the geometry. |
| `resolve_source(source, results=None)` | The internal record the rest reads from. `col(name)`, `energy_kWh(name)`, `dt_h`, `time`, `has_results`. |
| `PALETTE`, `FIGURES` | The categorical hue order, and figure name to plotting function. |

---

## Flexibility (`flexibility.py`)

| Name | Purpose |
|---|---|
| `assess_flexibility(baseline, flexible, config=None, ...)` | Full `FlexibilityReport`. |
| `flexibility_envelope(...)` | Deliverable shed / pre-cool power over time. |
| `load_shape_metrics(series, ...)` | Peak, load factor, ramp statistics. |
| `heat_gain_summary(frame)` | Pipe, envelope and tank gain as shares of demand. |
| `FlexibilityReport`, `LoadShapeMetrics`, `HeatGainSummary` | |

The report separates **thermal** round-trip efficiency from **electric** round-trip efficiency, because a store that returns 81 % of the cooling it absorbed can still return 85 % of the electricity, depending on when it charged.

---

## Configuration (`config_schema.py`)

| Name | Purpose |
|---|---|
| `make_branch_specs(config)` | Normalise `branch:` into a tree of `BranchSpec`. |
| `BranchSpec` | `label`, `raw`, `building_indices`, `pipes`, `terminals`, `is_root`, `heading_deg`, `origin_m`, `parent`; `walk()`, `children()`, `pipe_keys`, `qualify(key)`, `setting(key, default)`. |
| `TerminalSpec` | `kind`, `label`, `raw`, `child`, `mass_flow_kg_s`, `dp`, `heading_deg`; `releasable`, `pressure_spec()`. |
| `make_buildings(config)` | Every `Building`, in scenario order. |
| `make_pipe_thermal(config)` | Root branch pipe heat models. |
| `make_network_pipe_thermal(config)` | All branches, keyed by label. |
| `branch_pipe_thermal(spec, config)` | One branch. |
| `make_storage(config)` / `make_storage_from_section(cfg)` | A `ColdStorage` or `None`. |
| `make_stratified_tank(cfg)` | A `StratifiedTank` from a `storage.stratified` section, or `None`. |
| `plant_control_mode(config)` | `"supply_temperature"` or `"evaporator_duty"`. |
| `heat_gains_enabled(config)` | Whether any component in the tree has a non-adiabatic heat model. |
| `autofill_branch_pressure(spec, pressure_ratio=0.998)` | Fill in a consistent set of pipe pressure ratios for one branch. Returns whether it did. Declines on a fork, on a branch with no buildings, and on one that already states a pressure. |
| `autofill_network_energy(branches, plant_control)` | Give every adiabatic pipe the `Q = 0` it implies, leaving one free where `evaporator_duty` needs it. Returns the qualified keys it filled. |
| `AUTO_PIPE_PRESSURE_RATIO` | The `pr` autofill hands a pipe it had to specify itself. |
| `PIPE_HEAT_MODELS`, `TERMINAL_KINDS` | The valid values. |
