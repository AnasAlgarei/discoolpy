# Configuration reference

Every section and key a DisCoolPy scenario can contain. A scenario is a YAML file (loaded with `load_yaml_config`) or an equivalent Python dictionary, nothing in the tool distinguishes them.

Only `buildings` is genuinely required. Everything else has a working default, including the whole `design` section.

The shortest scenario that solves is six lines:

```yaml
branch:
  autofill: true

buildings:
  - {label: building_1, Q_design_W: 150000.0}
  - {label: building_2, Q_design_W: 150000.0}
```

**Back-compatibility rule:** a scenario that does not mention a feature behaves exactly as it did before that feature existed. Every optional section defaults off, and any value a scenario states overrides its fallback.

---

## Contents

- [`metadata`](#metadata)
- [`network`](#network)
- [`design`](#design)
- [`chiller`](#chiller)
- [`cooling_tower`](#cooling_tower)
- [`buildings`](#buildings)
- [`branch`](#branch), including [`pipes`](#branchpipes), [`thermal_defaults`](#branchthermal_defaults), [`terminals`](#branchterminals)
- [`storage`](#storage)
- [`profiles`](#profiles)
- [`economics`](#economics)
- [`solver`](#solver)
- [`outputs`](#outputs)
- [Result columns](#result-columns)

---

## `metadata`

Free-form. Nothing reads it except your own scripts and the notebooks, which print `metadata.description`.

```yaml
metadata:
  name: config_branching_grid
  description: >
    What this scenario exists to demonstrate.
```

---

## `network`

| Key | Default | Meaning |
|---|---|---|
| `chilled_water_cycle_closer` | `"chilled water cycle closer"` | Label of the TESPy `CycleCloser` that closes the chilled-water loop. |

---

## `design`

Design-point boundary conditions. These size the plant and seed the solver.

| Key | Required | Default | Meaning |
|---|---|---|---|
Every key here has a fallback, so state only the ones you have an opinion about. The defaults are ordinary chilled-water district values, collected in `discoolpy.DESIGN_DEFAULTS`.

| Key | Default | Meaning |
|---|---|---|
| `supply_temperature_degC` | `7.0` | Chilled-water temperature **entering the distribution network**. Under `plant_control: supply_temperature` this is the setpoint the plant is solved against. |
| `building_return_temperature_degC` | `12.0` | Design return temperature from the buildings. With `supply_temperature_degC` it sets the design ΔT, and therefore every building's design mass flow. |
| `ambient_temperature_degC` | `35.0` | Design dry-bulb air temperature. Drives exposed-pipe gain and the cooling-tower boundary. |
| `ground_temperature_degC` | falls back to `ambient_temperature_degC` | Design soil temperature at burial depth. Drives buried-pipe gain. |
| `condenser_inlet_temperature_degC` | `30.0` | Condenser water entering the chiller. |
| `condenser_outlet_temperature_degC` | `35.0` | Condenser water leaving the chiller. |
| `chilled_water_pressure_bar` | `3.0` | Absolute pressure anchor at the branch inlet. |
| `condenser_pressure_bar` | `3.0` | Pressure anchor for the condenser-water loop. |
| `pump_power_W` | derived, see below | Design pump shaft power. Used as the pump specification when `branch.fix_pump_power` is true, and always as the chiller-sizing offset and the starting value. |
| `pump_efficiency` | `0.70` | Pump isentropic efficiency. |
| `bypass_mass_flow_kg_s` | `0.05` | Minimum flow through the default end-of-line bypass. Ignored for branches that declare their own `terminals`, those name their own `m_kg_s`. |
| `cp_water_J_kgK` | `4180.0` | Used to convert design loads into design mass flows. |
| `plant_control` | see below | `"supply_temperature"` or `"evaporator_duty"`. |

### `pump_power_W`

Zero is not a usable fallback: a pump doing no work has no isentropic efficiency, and TESPy divides by the enthalpy rise to compute one. So when a scenario says nothing, DisCoolPy sizes the pump from the total design mass flow against a nominal 1 bar head:

```
P = m_total * 1 bar / (rho * eta_pump)
```

That converges and lands in the right order of magnitude. It is not a sizing. Real district heads run from well under 1 bar on a short campus loop to 3 bar on a several-kilometre network, so put your own number here once you have one. `discoolpy.default_pump_power_W(config)` returns whichever value will be used.

### `plant_control`

Defaults to `"supply_temperature"` whenever any heat gain is active, and `"evaporator_duty"` otherwise.

- **`supply_temperature`**: the central chiller's evaporator duty is *released* and solved from the loop energy balance against the supply setpoint. This is the more faithful control model and the only one compatible with heat gains, a hydraulically coupled store, or a satellite plant.
- **`evaporator_duty`**: the legacy mode. The duty is asserted as the sum of the building loads plus `pump_power_W`, and exactly one pipe must be left without an energy specification to close the loop. Combining it with heat gains is refused, because the loop balance already determines the duty and asserting it too over-determines the network.

---

## `chiller`

The central plant. The same keys describe a satellite plant's chiller under [`terminals`](#branchterminals).

| Key | Default | Meaning |
|---|---|---|
| `label` | `"yaml_config_chiller"` | Prefixes every internal component label. |
| `T_evap_degC` | `2.0` | Evaporator saturation temperature. |
| `T_cond_degC` | `46.0` | Condenser saturation temperature. Mutually exclusive with `native_offdesign.condenser_ttd_u`. |
| `eta_s` | `0.75` | Compressor isentropic efficiency. |
| `refrigerant` | `"R134a"` | Any CoolProp fluid. |
| `pressure_ratios.evap_1` | `0.999` | Evaporator, water side. |
| `pressure_ratios.evap_2` | `0.999` | Evaporator, refrigerant side. |
| `pressure_ratios.cond_1` | `0.999` | Condenser, refrigerant side. |
| `pressure_ratios.cond_2` | `1.0` | Condenser, water side. |
| `native_offdesign.enabled` | `true` | Turn on TESPy design/offdesign metadata and characteristic lines. |
| `native_offdesign.evaporator_ttd_l` | `null` | Lower terminal temperature difference (K). Alternative to fixing `T_evap_degC`. |
| `native_offdesign.condenser_ttd_u` | `null` | Upper terminal temperature difference (K). Alternative to fixing `T_cond_degC`, specifying both over-determines the cycle. |
| `native_offdesign.use_pressure_loss_characteristics` | `false` | Let `zeta` follow a characteristic offdesign instead of holding `pr`. |

The evaporator duty is **not** a config key. It is sized from the building loads and then either asserted or released depending on `plant_control`.

---

## `cooling_tower`

| Key | Default | Meaning |
|---|---|---|
| `label` | `"yaml cooling tower"` | |
| `fluid` | `{water: 1.0}` | Condenser-water fluid composition. |
| `pr` | `null` | Water-side pressure ratio. Leave null when the chiller condenser already fixes the secondary pressure ratio, otherwise the closed loop is over-determined. |
| `approach_temperature_K` | `condenser_inlet − ambient` | Used to derive the condenser inlet temperature from ambient at a snapshot that does not give one explicitly. Negative means the tower delivers water *below* ambient dry bulb, which is what a wet tower does. |
| `native_offdesign` | `false` | Hold the design water flow and use TESPy's `kA_char` relation offdesign. |
| `start_mass_flow_kg_s` | `45.0` | Starting value for the condenser-water flow. |
| `condenser_inlet_temperature_degC` | from `design` | Override, mainly for satellite towers. |
| `condenser_outlet_temperature_degC` | from `design` | Override. |
| `condenser_pressure_bar` | from `design` | Override. |

---

## Checking the file

A misspelled key in YAML is not an error, it is a key nobody reads, so DisCoolPy checks the structure of a scenario before it builds anything from it. Every entry point does this; `discoolpy check` reports it first.

- **A key close to a real one stops the run**, naming both, because the scenario it produces is not the one that was written:

  ```
  Scenario error in my_scenario.yaml
    buildings[0].Q_desgin_W: unknown key 'Q_desgin_W'
    -> did you mean 'Q_design_W'?
  ```

- **A key close to nothing is a warning** and the run continues, because scenarios carry notes and annotations of their own.
- **`discoolpy check --strict`**, or `validate_scenario(config, strict=True)`, turns every warning into a failure. That is what you want in continuous integration.

The keys listed on this page are the ones the check knows about, as `discoolpy.SCENARIO_KEYS`. Every shipped scenario and template is tested against it.

---

## `buildings`

An ordered list. Each entry defines one building substation; which branch it sits on, and where in that branch, is decided by [`branch.buildings`](#branch).

| Key | Required | Default | Meaning |
|---|---|---|---|
| `label` | yes | - | Must be unique; it names a TESPy component and every result column for that building. |
| `Q_design_W` | yes* | - | Design cooling demand in W, positive. |
| `Q_design_kW` | yes* | - | The same in kW, which is the unit it usually arrives in. Give one of the two, not both. A `Q_design_W` under 10 kW is warned about, because a district substation that small does not exist and the likely cause is kilowatts typed into the watts key. |
| `pr` | no | `null` | Pressure ratio across the substation heat exchanger. `null` leaves the drop as a solved output, the balancing valve's duty. **Recommended.** |
| `archetype` | no | `"office"` | Load shape used by the synthetic profile generator: `office`, `hotel`, `retail`, `residential`, `hospital`, `datacentre`. |
| `load_model` | no | `"profile"` | `profile`, `envelope`, or `thermal_mass`. |
| `envelope.*` | no | - | Required for the non-profile models. See below. |
| `thermal_mass.*` | no | - | Required for `load_model: thermal_mass`. |

### `buildings[].envelope`

| Key | Default | Meaning |
|---|---|---|
| `UA_W_K` | `0.0` | Envelope conductance to outdoor air. |
| `solar_aperture_m2` | `0.0` | Effective glazing area for solar gain. |
| `infiltration_kg_s` | `0.0` | Outdoor-air mass flow. |
| `indoor_setpoint_degC` | `24.0` | |
| `internal_gain_W` | `0.0` | Constant internal gain. |
| `cp_air_J_kgK` | `1005.0` | |

With `load_model: envelope` the building's demand becomes `UA·ΔT + solar + infiltration + internal`, and the profile value is treated as the internal gain.

### `buildings[].thermal_mass`

A first-order RC zone model. This is what makes pre-cooling and load shedding representable.

| Key | Default | Meaning |
|---|---|---|
| `capacitance_J_K` *or* `capacitance_MJ_K` | required | Effective thermal capacitance of the zone. |
| `setpoint_degC` | envelope setpoint, else `24.0` | |
| `min_temperature_degC` | `setpoint − 3` | Bottom of the comfort band, how far pre-cooling may go. |
| `max_temperature_degC` | `setpoint + 2` | Top of the comfort band, how far coasting may go. |
| `max_cooling_W` | `null` | Terminal-unit capacity. |
| `precool_max_cooling_W` | `null` | Separate cap while pre-cooling. |
| `initial_temperature_degC` | setpoint | |

Exceeding the band is reported per snapshot as `<building>_band_violation_K` and raised as a warning at the end of a run, flexibility taken out of occupant comfort is not flexibility.

---

## `branch`

The root branch, and the root of the whole tree. A branch that declares no `terminals` is exactly the pre-branching topology: one street ending in one bypass valve.

| Key | Default | Meaning |
|---|---|---|
| `label` | `"district"` | Must be unique across the network; it prefixes every TESPy component on this branch. |
| `buildings` | all of them, in order | Ordered list of building **labels** on this branch. The first name is the first tap after the branch inlet. |
| `pump_placement` | `"supply_inlet"` on the root, `null` on children | `supply_inlet`, `return_outlet`, or `null` for no pump. |
| `pump_label` | `"main pump"` on the root | |
| `pipe_model` | `"pressure_ratio"` | `pressure_ratio`, `length_derived_pr`, or `darcy`. Inherited by children. |
| `fix_pump_power` | `true` unless `pipe_model: darcy` | Fix the pump shaft power at `design.pump_power_W`. |
| `pump_pressure_ratio` | - | Fix the pump pressure ratio instead. Used when `fix_pump_power: false`. |
| `native_offdesign` | `false` | Forward TESPy design/offdesign metadata to the pump and building heat exchangers. |
| `auto_relax_pressure` | `false` | Let DisCoolPy free the minimum number of balancing valves needed to make the network well posed, and report what it released. |
| `autofill` | `false` | Work the whole pressure and energy specification out from the number of buildings. See below. Not inherited by children; set it on each branch that wants it. |
| `bypass_dp` | `0.0` | Pressure change across the *default* bypass (the one created when no `terminals` are declared). `null` leaves it free. |
| `heat_model` | `"adiabatic"` | Default pipe heat model for this branch. Inherited by children. |
| `thermal_defaults` | - | Defaults every pipe on this branch inherits. Inherited by children. |
| `hydraulic` | - | Fluid properties for `length_derived_pr`. Inherited by children. |
| `origin_m` | `[0, 0]` | Plan-view position of the branch inlet, in metres. Layout only. |
| `heading_deg` | parent's, else `0` | Default bearing for this branch's pipes, degrees counter-clockwise from east. Layout only. |
| `pipes` | `{}` | See below. |
| `terminals` | one bypass | See below. |

Children inherit `pipe_model`, `heat_model`, `thermal_defaults`, `hydraulic`, `native_offdesign` and `auto_relax_pressure` from their parent unless they set their own.

### `branch.autofill`

Counting pressure degrees of freedom is the step that stops most people writing their first scenario, and the arithmetic is mechanical. `autofill: true` does it for you, and does two things:

- **Pressure.** Applies `suggest_pressure_specification(n, bypass_fixed)`, which is the standard answer for a ladder of *n* buildings: fix the pipes the count asks for, leave the building legs and the bypass to be solved. Each pipe it fixes gets `pr = 0.998`, a few millibar, which is the right order for a short adiabatic leg and keeps the design solve well conditioned.
- **Energy.** Gives every adiabatic pipe the `Q = 0` that TESPy needs written down. Under `plant_control: supply_temperature` that is every pipe; under `evaporator_duty` the root branch's last return pipe is left free to take the loop residual.

It declines, silently and without changing anything, in three cases:

| Case | Why |
|---|---|
| The branch forks | Which parallel path carries the free element is a modelling decision, not arithmetic |
| The branch has no buildings | A trunk link has nothing to count |
| The branch already states a pressure anywhere | A scenario that says something about pressure is assumed to mean it |

A pipe that carries a real heat model is never given a `Q`, because that would over-determine it.

Use it while you get the rest of the scenario right, then turn it off and write real pipe geometry. The two functions are also callable directly, as `discoolpy.autofill_branch_pressure` and `discoolpy.autofill_network_energy`.

### `branch.pipes`

Keyed `supply_1 … supply_n` and `return_1 … return_n`, where *n* is the number of buildings on that branch. A branch with no buildings is a trunk link, and gets one of each. Naming a pipe that does not exist is refused with the list of keys that do.

Supply pipe *i* runs from tap *i−1* to tap *i*; return pipe *i* runs from junction *i* back towards the plant. Both carry the same design flow: the buildings from tap *i* onwards, plus whatever leaves through the end of the line.

| Key | Used by | Meaning |
|---|---|---|
| `pr` | `pressure_ratio`, `length_derived_pr` | Pressure ratio across the pipe. |
| `L` | `darcy`, `length_derived_pr`, `ua`, layout | Length in metres. |
| `D` | `darcy`, `length_derived_pr`, `ua` | Inner diameter in metres. |
| `ks` | `darcy`, `length_derived_pr` | Absolute roughness in metres (≈ `5e-5` for steel). |
| `Q` or `Q_W` | `adiabatic`, `fixed` heat models | Fixed heat duty in W. `Q: 0.0` is an adiabatic pipe. |
| `heat_model` | all | Overrides `branch.heat_model` for this pipe. |
| `UA_W_K` | `ua` | Total conductance. Overrides geometry. |
| `UA_per_m_W_mK` | `ua` | Conductance per metre; multiplied by `L`. |
| `heading_deg` | layout | Bearing, degrees counter-clockwise from east. A return pipe inherits its supply counterpart's bearing. |
| any `thermal_defaults` key | `ua`, native models | Per-pipe override: `placement`, `insulation_thickness_m`, `ambient_source`, and the rest. |

### `branch.thermal_defaults`

| Key | Default | Meaning |
|---|---|---|
| `placement` | `"buried"` | `buried`, `surface`, `exposed`, or `tunnel`. |
| `insulation` | `"pur"` | Named material or a conductivity in W/(m·K). |
| `insulation_thickness_m` | `0.04` | |
| `burial_depth_m` | `1.0` | Buried pipes only. |
| `ground` | `"moist soil"` | Named soil or a conductivity. |
| `pipe_wall_thickness_m` | `0.0` | |
| `pipe_wall_conductivity_W_mK` | `45.0` | Carbon steel. |
| `internal_film_W_m2K` | `2000.0` | |
| `twin_spacing_m` | `null` | Centre-to-centre spacing of a supply/return pair in one trench. Applies the mutual-heating correction. |
| `wind_velocity_m_s` | `1.0` | Surface pipes only. |
| `emissivity` | `0.9` | Surface pipes only. |
| `ambient_source` | `"ground"` | `ground`, `air`, or `fixed`. Decides which temperature the pipe tracks as the weather changes. |

Pipe heat models: `adiabatic` (Q = 0), `fixed` (a given Q), `ua` (conductance from geometry, TESPy solves the gain from the actual water temperature), `tespy_buried` and `tespy_surface` (TESPy's own native pipe groups). See [Heat gains](heat_gains.md).

### `branch.terminals`

What closes the end of the branch. Omit the key entirely and you get one bypass valve carrying `design.bypass_mass_flow_kg_s`, the pre-branching topology. Declare it and the branch becomes the root of a tree.

Common to all terminal types:

| Key | Default | Meaning |
|---|---|---|
| `type` | `"bypass"` | `bypass`, `plant`, or `branch`. |
| `label` | derived | Must be unique within the branch. |
| `m_kg_s` | `0.0` for a bypass; **required** for a plant | Design mass flow. A `branch` terminal never names one, it carries whatever its subtree consumes. |
| `dp` | `null` | Pressure change across the terminal's valve. `null` (recommended) leaves it free. |
| `heading_deg` | fanned out | Bearing of this leg. Layout only. |

**`type: bypass`** is an end-of-line valve returning flow to the return header. Nothing further.

**`type: plant`** is a satellite plant: a chiller with its own cooling tower and optionally its own store, taking a slipstream from the end of the line, cooling it, and delivering it into the return header.

| Key | Default | Meaning |
|---|---|---|
| `Q_evap_kW` or `Q_evap_W` | **required** | Design (and maximum) evaporator duty. Asserted, not solved. |
| `dispatch` | `"fixed"` | `fixed` holds the design duty; `proportional` makes what it *delivers* follow the district's load. |
| `min_load_fraction` | `0.2` | Floor on the proportional duty, as a fraction of design. |
| `chiller` | `{}` | A [`chiller`](#chiller) mapping. |
| `cooling_tower` | `{}` | A [`cooling_tower`](#cooling_tower) mapping; unset keys fall back to `design`. |
| `storage` | - | A [`storage`](#storage) mapping. Sits in series downstream of the evaporator. |

**`type: branch`** is a sub-branch. The entry is itself a full `branch` mapping (`label`, `buildings`, `pipes`, `terminals`, `heading_deg`, …), so networks nest to any depth.

Full treatment, including the degree-of-freedom rules a fork introduces, in [Network topology](network_topology.md).

---

## `storage`

The central store. The same keys describe a satellite's store.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | |
| `label` | `"ice_storage"` | |
| `storage_type` | `"ice"` | `ice`, `chilled_water`, `pcm`. Labelling and reporting only. |
| `capacity_kWh` | `1600.0` | Usable cooling capacity. |
| `initial_soc` | `0.55` | |
| `max_charge_kW` / `max_discharge_kW` | `180.0` | Power limits. |
| `charge_efficiency` / `discharge_efficiency` | `0.90` / `0.92` | |
| `min_soc` / `max_soc` | `0.08` / `0.97` | Operating band. |
| `standby_loss_fraction_per_day` | `0.015` | Used by `loss_model: fraction`. |
| `loss_model` | `"fraction"` | `fraction`, `ua`, or `both`. |
| `coupling` | `"supervisory"` | `supervisory` rescales the building duties; `hydraulic` puts the store in the chilled-water loop as a real heat flow. |
| `hydraulic_pr` | `0.999` | Water-side pressure ratio when hydraulically coupled. |
| `target_chiller_load_kW` | `null` | The flat plant duty the store aims at. This is the load-levelling setpoint. |
| `minimum_load_fraction` | `0.08` | Floor on the plant duty a supervisory store may impose. |
| `charge_allowed_above_degC` / `discharge_allowed_below_degC` | `null` | Weather gates. |
| `stratified.*` | - | Layered tank; see below. |
| `max_tank_flow_kg_s` | from `max_charge_kW` | Largest flow the tank can pass, kg/s. Stratified tanks only. |
| `thermal.*` | - | See below. |

### `storage.stratified`

Replaces the scalar reservoir with a 1-D layered chilled-water tank. See
[Cold storage](cold_storage.md#stratified-tanks) for the full treatment.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | |
| `volume_m3` | from `thermal.volume_m3` | Tank volume. |
| `height_m`, `diameter_m` | - | Explicit geometry; any two of volume/height/diameter fix the third. |
| `height_to_diameter` | `2.5` | Used when only a volume is given. Tall and slender stratifies better. |
| `layers` | `12` | Equal-volume nodes. Twelve to sixteen is a good range. |
| `charged_temperature_degC` | from `thermal.storage_temperature_degC` | **Must equal the plant's supply setpoint.** |
| `discharged_temperature_degC` | from `thermal.discharged_temperature_degC` | **Must equal the design return temperature.** |
| `wall_htc_W_m2K` | `0.28` | Shell heat transfer coefficient. |
| `vertical_conductivity_W_mK` | `0.897` | Effective node-to-node conductivity. |
| `max_courant` | `0.5` | Sub-step control. |
| `max_sub_steps` | `4000` | Cap per call. |

With a tank present, `capacity_kWh` is **ignored** (geometry and the temperature
span determine it), `charge_efficiency` / `discharge_efficiency` are **ignored**
(the round trip is emergent), and `loss_model` must be `ua`, `fraction` or
`both` is refused, because the tank already computes its standing loss layer by
layer.

`max_tank_flow_kg_s` sits on the `storage` section, not here: it is a property of
the pumps and diffusers rather than the vessel.

### `storage.thermal`

| Key | Default | Meaning |
|---|---|---|
| `UA_W_K` | - | Tank conductance. Give this or the geometry. |
| `volume_m3` | - | Derive UA from tank geometry instead. |
| `insulation` / `insulation_thickness_m` | `"pur"` / `0.1` | |
| `height_to_diameter` | `1.0` | |
| `internal_film_W_m2K` / `external_film_W_m2K` | `300.0` / `12.0` | |
| `buried_fraction` | `0.0` | Fraction of the tank surface in contact with soil. |
| `ground` | `"moist soil"` | |
| `storage_temperature_degC` | `0.0` | Charged medium temperature. |
| `discharged_temperature_degC` | `null` | Discharged medium temperature. |
| `temperature_varies_with_soc` | `false` | A sensible-heat store warms as it discharges, so its ambient gain falls with state of charge. An ice store cannot do this. |

**Coupling matters.** A supervisory store is an accounting device: it rescales the building duties and never touches the network. A hydraulic store is a `SimpleHeatExchanger` between the cycle closer and the branch supply, so charging forces the plant to make water *below* the distribution setpoint, the COP penalty of making ice appears in the results instead of being assumed away.

---

## `profiles`

Read by `make_weather_and_load_profiles` to synthesise weather and per-building loads.

| Key | Default | Meaning |
|---|---|---|
| `start` | `"2026-07-01 00:00:00"` | First timestamp. |
| `periods` | `168` | Number of snapshots. |
| `freq` | `"1h"` | Any pandas frequency; `30min` is common. |
| `daily_high_degC` / `daily_low_degC` | - | One entry per day, or a scalar. |
| `peak_solar_W_m2` | `900.0` | Clear-sky peak. |
| `archetypes` | per building | Override a building's load shape. |
| `ground.depth_m` | `1.0` | Burial depth for the soil-temperature model. |
| `ground.mean_annual_degC` | `25.0` | |
| `ground.annual_amplitude_K` | `10.0` | Soil temperature is slow and damped: near-constant over a day, which is why a buried main and an exposed one behave differently on a hot afternoon. |

Bring your own profile instead by constructing the DataFrame yourself: `timestamp`, the weather columns, and one `<building label>_Q_W` column per building.

---

## `economics`

| Key | Default | Meaning |
|---|---|---|
| `tariff` | `0.0` | Currency per kWh, or a list of per-hour prices. |
| `carbon_intensity_kg_kWh` | `0.0` | Grid emissions factor. |

Used only by `flexibility.py`.

---

## `solver`

| Key | Default | Meaning |
|---|---|---|
| `iterinfo` | `false` | TESPy per-iteration output. |
| `max_iter` | `250` | |
| `progress_interval` | `48` | Snapshots between progress lines. |
| `initial_pressure_step_bar` | `0.05` | Pressure decrement per tap used to seed the starting values. Worth raising for a large network; a Newton solver started with every pressure equal has no gradient to tell parallel paths apart. |

---

## `outputs`

| Key | Default | Meaning |
|---|---|---|
| `output_dir` | `"outputs"` | Relative paths resolve against **the scenario file's** directory, so a scenario writes to the same place regardless of where it is launched from. |
| `profile_csv` | `"input_profile.csv"` | |
| `without_storage_csv` / `with_storage_csv` | | |
| `comparison_csv` | | |
| `summary_md` | | |
| `plot_png` | | |
| `layout_png` | | Used by the branching example. |

---

## Result columns

`run_configured_case` returns one row per snapshot. The columns that matter most:

| Column | Meaning |
|---|---|
| `timestamp`, `resolution_hours` | |
| `ambient_temperature_degC`, `ground_temperature_degC`, `solar_irradiance_W_m2` | Weather as applied. |
| `actual_building_total_Q_W` | Duty actually applied to the building heat exchangers. Differs from the profile when an envelope or thermal-mass model is in use. |
| `profile_building_total_Q_W` | What the profile asked for. |
| `chiller_Q_evap_W` | Central plant duty. Solved rather than asserted under `supply_temperature`. |
| `compressor_power_W`, `cop` | Central machine only. |
| `satellite_Q_evap_W` | What every satellite plant produces. |
| `satellite_delivered_Q_W` | What they hand the district, `produced − stored`. |
| `satellite_storage_Q_W` | Net exchange with satellite stores. |
| `satellite_compressor_power_W` | |
| `total_compressor_power_W`, `total_cooling_produced_W`, `fleet_cop` | The whole plant fleet. This is the electricity the district is billed for. |
| `pipe_heat_gain_W`, `pipe_supply_heat_gain_W`, `pipe_return_heat_gain_W` | Solved distribution gain, network-wide. |
| `pipe_<key>_Q_W` | Per pipe. Root-branch keys are bare (`pipe_supply_1_Q_W`); descendants are namespaced (`pipe_north_spur_supply_1_Q_W`). |
| `chw_supply_T_degC`, `chw_return_T_degC`, `chw_delta_T_K` | At the branch inlet and outlet. |
| `chw_plant_supply_T_degC` | Water leaving the plant. Diverges from the setpoint when a hydraulic store is charging. |
| `chw_total_m_kg_s`, `pump_power_W` | |
| `storage_*` | Central store mode, power, SOC, energy, losses. |
| `storage_cold_port_degC`, `storage_thermocline_m`, `storage_tank_T0_degC` ... | Stratified tanks only: the delivery temperature and the full profile. |
| `<building>_effective_Q_W`, `<building>_T_indoor_degC`, `<building>_band_violation_K`, … | Per building. |
| `chw_energy_residual_W` | **Closure check.** Everything entering the chilled water must leave through an evaporator. This should be numerically zero; a run warns if it exceeds 1 W. |

The closure identity, with everything the loop can contain:

```
Q_central + Q_satellite − Q_satellite_store
    = Q_buildings + Q_pipes + P_pump + Q_central_store
```

A satellite enters with the opposite sign because it takes duty out of the loop before it reaches the central machine.
