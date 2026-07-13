# API Reference

This page documents all public classes, methods, and utility functions in the `DisCoolPy` package.

---

## Package layout

```
discoolpy/
├── branch.py        — Distribution branch (pipes, pump, bypass, buildings)
├── building.py      — Building substation heat exchanger
├── chiller.py       — Vapour-compression chiller
├── cold_storage.py  — Cold/ice thermal storage (supervisory model)
├── cooling_tower.py — Condenser heat-rejection tower
├── time_snapshot.py — Time-step data containers
└── utils.py         — YAML loading, system assembly, plotting
```

---

## `Chiller` — `chiller.py`

A dataclass wrapper around the TESPy components required to model a single-stage vapour-compression refrigeration cycle.

### Constructor parameters

| Parameter | Type | Description |
|---|---|---|
| `label` | `str` | Unique label prefix for all TESPy components and connections |
| `T_evap_degC` | `float` | Evaporator saturation temperature (°C) |
| `T_cond_degC` | `float` | Condenser saturation temperature (°C) |
| `eta_s` | `float` | Isentropic efficiency of the compressor (0–1) |
| `refrigerant` | `str` | CoolProp refrigerant name, e.g. `"R134a"` |
| `native_offdesign` | `dict` | Offdesign settings; set `enabled: true` to use TESPy's native characteristic lines. Optionally set `condenser_ttd_u` (upper terminal temperature difference in K). |

### Key methods

`add_to_network(nw)` — Add all chiller components and connections to a TESPy `Network` object.

`set_design(T_evap, T_cond, eta_s, p_evap, p_cond)` — Apply design-point parameters. Called internally by `build_standard_branch_system`.

`set_offdesign_ambient(ambient_temperature, condenser_inlet_temperature)` — Update condenser-side conditions for an offdesign snapshot.

### Key attributes (after solve)

| Attribute | Description |
|---|---|
| `chiller.evaporator.Q.val` | Evaporator heat transfer rate (W, positive = heat removed from chilled water) |
| `chiller.compressor.P.val` | Compressor shaft power (W) |
| `chiller.condenser.Q.val` | Condenser heat rejection rate (W) |

### YAML schema

```yaml
chiller:
  label: my_chiller
  T_evap_degC: 2.0
  T_cond_degC: 40.0
  eta_s: 0.80
  refrigerant: R134a
  native_offdesign:
    enabled: true
    condenser_ttd_u: 5.0
```

---

## `CoolingTower` — `cooling_tower.py`

Models the condenser heat-rejection tower as a TESPy `HeatExchangerSimple` component with an approach-temperature specification.

### Constructor parameters

| Parameter | Type | Description |
|---|---|---|
| `label` | `str` | Unique label prefix |
| `fluid` | `dict` | Fluid composition, e.g. `{"water": 1.0}` |
| `approach_temperature_K` | `float` | Tower outlet temperature relative to ambient (negative = outlet below ambient) |
| `native_offdesign` | `bool` | Whether to use TESPy's native offdesign model |
| `start_mass_flow_kg_s` | `float` | Initial guess for condenser-water mass flow |

### Key methods

`add_to_network(nw)` — Add tower component and connections to the network.

`set_offdesign_ambient(ambient_temperature, condenser_inlet_temperature)` — Update ambient and condenser inlet conditions for a snapshot.

`heat_rejection()` — Return positive tower heat rejection in W after a successful solve.

### YAML schema

```yaml
cooling_tower:
  label: my_cooling_tower
  fluid:
    water: 1.0
  approach_temperature_K: -5.0
  native_offdesign: false
  start_mass_flow_kg_s: 20.0
```

---

## `Branch` — `branch.py`

Assembles the full distribution branch: supply and return pipes, a circulation pump, Splitter/Merger junctions, building substation connections, and a bypass valve.

### Constructor parameters

| Parameter | Type | Description |
|---|---|---|
| `label` | `str` | Unique label prefix |
| `buildings` | `list[Building]` | Ordered list of `Building` objects to connect |
| `pump_placement` | `str` | `"supply_inlet"` (recommended) or `"return_outlet"` |
| `pump_label` | `str` | Label for the pump component |
| `native_offdesign` | `bool` | Whether to use TESPy's native pump offdesign model |
| `pipe_model` | `str` | `"pressure_ratio"` or `"length_derived_pr"` |
| `pipes` | `dict` | Per-pipe parameter dictionaries (see below) |

### Pipe model options

**`pressure_ratio`** — Each pipe is specified by a dimensionless pressure ratio `pr` (0–1) and optionally a heat loss `Q` (W). This is the simplest and most robust model.

**`length_derived_pr`** — Each pipe is specified by `length_m`, `diameter_m`, and `roughness_m`. The tool computes the Darcy-Weisbach friction factor and derives an equivalent pressure ratio. This model is intended for planned-network analysis where street lengths are known.

### Pipe dictionary keys

| Key | Unit | Description |
|---|---|---|
| `pr` | — | Pressure ratio (0–1). Required for `pressure_ratio` model. |
| `Q` | W | Heat loss through the pipe wall (0 = adiabatic). |
| `length_m` | m | Pipe length. Required for `length_derived_pr` model. |
| `diameter_m` | m | Inner pipe diameter. Required for `length_derived_pr` model. |
| `roughness_m` | m | Pipe wall roughness (default: 4.6 × 10⁻⁵ m for commercial steel). |

### Pressure constraint rules

These rules must be followed to avoid a singular Jacobian:

1. Set `pr` on all buildings **except the last one** in the chain.
2. Set `pr` on supply pipes `supply_2` through `supply_N` (these bridge pressure islands).
3. Set `pr` on `return_1` (anchors the return-header pressure).
4. Do **not** set `pr` on `return_2` through `return_N`.

### YAML schema

```yaml
branch:
  label: my_branch
  pump_placement: supply_inlet
  pump_label: main pump
  native_offdesign: false
  pipe_model: pressure_ratio
  pipes:
    supply_1:
      Q: 0.0
    supply_2:
      pr: 0.997
      Q: 0.0
    return_2:
      Q: 0.0
    return_1:
      pr: 0.999
```

---

## `Building` — `building.py`

Models a building substation as a `SimpleHeatExchanger` that removes heat from the chilled-water stream.

### Constructor parameters

| Parameter | Type | Description |
|---|---|---|
| `label` | `str` | Unique label |
| `Q_design_W` | `float` | Design-point cooling load (W, positive) |
| `pr` | `float \| None` | Pressure ratio across the substation heat exchanger. Set to `None` for the last building in the chain. |

### Key methods

`set_demand(q_W)` — Apply a positive cooling demand (W) to the heat exchanger. Call this before each offdesign solve.

`inlet.set_attr(m=value)` — Set the building mass flow rate (kg/s) for an offdesign snapshot.

### YAML schema

```yaml
buildings:
  - label: building_1
    Q_design_W: 150000.0
    pr: 0.995
  - label: building_2
    Q_design_W: 150000.0
    pr: null   # last building
```

---

## `ColdStorage` — `cold_storage.py`

A supervisory thermal storage model that tracks state of charge (SoC) and computes a load offset for the chiller at each time step. It does not add TESPy components to the network; instead, it modifies the effective chiller load via `chiller_load_offset_W`.

### Constructor parameters

| Parameter | Type | Description |
|---|---|---|
| `label` | `str` | Unique label |
| `capacity_kWh` | `float` | Total storage capacity (kWh) |
| `max_charge_rate_kW` | `float` | Maximum charging power (kW) |
| `max_discharge_rate_kW` | `float` | Maximum discharging power (kW) |
| `charge_efficiency` | `float` | Round-trip charge efficiency (0–1) |
| `discharge_efficiency` | `float` | Round-trip discharge efficiency (0–1) |
| `initial_soc` | `float` | Initial state of charge (0–1) |
| `strategy` | `str` | Dispatch strategy: `"load_leveling"` or `"peak_shaving"` |

### Key methods

`step(total_building_load_W, dt_hours)` — Advance the storage by one time step. Returns `(soc_new, load_offset_W)` where `load_offset_W` is the reduction in chiller load (positive = discharging).

`record()` — Return a dict with current SoC, charge/discharge power, and strategy for CSV export.

### YAML schema

```yaml
storage:
  enabled: true
  label: ice_storage
  capacity_kWh: 500.0
  max_charge_rate_kW: 200.0
  max_discharge_rate_kW: 200.0
  charge_efficiency: 0.90
  discharge_efficiency: 0.90
  initial_soc: 0.5
  strategy: load_leveling
```

---

## `TimeSnapshot` and `SnapshotSchedule` — `time_snapshot.py`

### `TimeSnapshot`

A data container for one steady-state operating point.

| Field | Type | Description |
|---|---|---|
| `timestamp` | `datetime` | Wall-clock time of the snapshot |
| `building_loads` | `dict[str, float]` | Mapping of building label to cooling load (W, positive) |
| `ambient_temperature` | `float` | Outdoor dry-bulb temperature (°C) |
| `condenser_inlet_temperature` | `float` | Condenser-water inlet temperature (°C) |

### `SnapshotSchedule`

An ordered collection of `TimeSnapshot` objects with convenience constructors.

`SnapshotSchedule.from_profile_arrays(start, resolution, building_profiles, ...)` — Create from numpy arrays.

`SnapshotSchedule.from_csv(csv_path, timestamp_column, building_columns, ...)` — Create from a CSV file.

`schedule.apply(index, buildings, chiller, cooling_tower, ...)` — Apply snapshot `index` to the model objects and return total building load.

---

## Utility functions — `utils.py`

### `load_yaml_config(path)`

Load a YAML scenario file and return a plain Python dictionary.

```python
config = load_yaml_config('configs/tutorial_two_building.yaml')
```

### `build_standard_branch_system(config)`

Assemble the full TESPy network from a config dictionary. Returns a tuple:

```python
nw, chiller, buildings, branch, cooling_tower, c_chiller_to_cc = \
    build_standard_branch_system(config)
```

| Return value | Type | Description |
|---|---|---|
| `nw` | `tespy.networks.Network` | The assembled TESPy network (not yet solved) |
| `chiller` | `Chiller` | Chiller component wrapper |
| `buildings` | `list[Building]` | Ordered list of building wrappers |
| `branch` | `Branch` | Distribution branch wrapper |
| `cooling_tower` | `CoolingTower` | Cooling tower wrapper |
| `c_chiller_to_cc` | `Connection` | The cycle-closing connection |

### `run_configured_case(config, profile_df, case, use_storage)`

Run a complete design + offdesign time-series simulation. Returns a `pandas.DataFrame` with one row per snapshot.

| Parameter | Type | Description |
|---|---|---|
| `config` | `dict` | Loaded YAML config |
| `profile_df` | `DataFrame` | Time-series profile with building load columns |
| `case` | `str` | Output case label (used for file naming) |
| `use_storage` | `bool` | Whether to enable the cold storage component |

### `make_pipe_attrs(config)`

Translate the `branch.pipes` section of a config dict into TESPy `Pipe.set_attr` keyword dictionaries. Handles both `pressure_ratio` and `length_derived_pr` pipe models.

### `make_storage_comparison_plot(config, no_storage, with_storage)`

Create the standard five-panel comparison plot (chiller load, compressor power, COP, storage SoC, load offset) and save it to the configured output directory.

### `write_storage_comparison_summary(config, no_storage, with_storage)`

Write a Markdown summary and CSV of key metrics for the paired storage comparison.
