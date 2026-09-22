# Heat gains and flexibility

A district-cooling network is colder than everything around it. Buried mains gain
heat from the soil, an ice tank gains heat from the air, and a building gains
heat through its envelope. All three are loads the plant has to remove again, and
all three erode the flexibility a thermal store or a pre-cooling scheme can
deliver.

This page covers what DisCoolPy models, how to configure it, and where the
numbers come from.

---

## Why the plant duty must be a solved output

Before the upgrade, a scenario asserted the chiller's evaporator duty:

```
Q_evap  :=  sum(building loads) + pump_power
```

That is exactly right when the pipes are adiabatic, and only then. As soon as
anything else adds heat to the chilled-water loop, the loop energy balance

```
Q_evap  =  Q_buildings + Q_pipes + P_pump + Q_storage
```

already determines the duty, so asserting it as well is one equation too many.
TESPy does not mis-solve this; it refuses the network outright.

DisCoolPy therefore switches to `plant_control: supply_temperature` whenever heat
gains are active (automatically, you do not have to ask for it). The chiller
duty is released and solved, which is also the more faithful control model: a
real plant modulates to hold a supply-temperature setpoint and *reports* its duty.

Every result row carries `chw_energy_residual_W`, which must be numerically zero.
`run_configured_case` warns if it ever exceeds 1 W.

### The hidden slack this replaces

The original scenarios left one pipe without an energy specification, with a
comment reading "Q not needed for the last return pipe". That pipe was a slack
variable. In `config_length_pipes.yaml` the scenario asserted a 650 W pump while
the pump actually drew 1168 W, and the 518 W difference was silently reported as
`-518 W` of pipe *cooling*, a physical impossibility that nothing in the output
surfaced, because pipe duties were never reported at all.

`discoolpy.hydraulics.validate_thermal_degrees_of_freedom` now counts these
explicitly and refuses an ambiguous specification.

---

## Pipes

### Choosing a heat model

Set `branch.heat_model` as the default and override per pipe if needed.

| `heat_model` | What it does | When to use it |
|---|---|---|
| `adiabatic` | `Q = 0` | Reproducing pre-upgrade results, or a network so short that gain is negligible |
| `fixed` | A constant `Q` in W that you supply | You have measured losses and want to impose them |
| `ua` | DisCoolPy derives `UA` from geometry, then hands `UA` + ambient to TESPy's `UA_group` | **Recommended.** The gain is solved from the actual water temperature at every snapshot |
| `tespy_buried` / `tespy_surface` | Hands the geometry to TESPy's native pipe groups, which derive the conductance too | When you want TESPy's own formulation, and the geometry is inside what it accepts |

All three conducting models are solved by TESPy; the next section says exactly
what that means and why the conductance is derived here.

### What the solver actually solves, and what DisCoolPy only supplies

It is worth being exact about the division of labour, because "DisCoolPy
calculates the pipe heat loss" would be wrong.

Under `heat_model: ua` DisCoolPy computes one number, the conductance `UA`, and
hands it to TESPy together with an ambient temperature. That activates TESPy's
own `UA_group`, whose residual is

```
0 = m*(h_out - h_in) + UA * dT_log
```

and which is assembled into the same Jacobian as every other equation in the
network. The duty is solved simultaneously with the water temperatures, not
computed from them afterwards. Nothing in DisCoolPy's solve path evaluates a
heat flow: `heat_gain_report()` reads `Q`, `UA` and `lmtd` back off the solved
component, and `PipeThermal.heat_gain_W` exists only for sanity checks before a
solve and is called by nothing that runs during one.

So all three conducting models are native. The only thing that differs between
`ua` and `tespy_buried`/`tespy_surface` is where the conductance comes from:

| | `ua` | `tespy_buried` / `tespy_surface` |
|---|---|---|
| Conductance from | DisCoolPy's closed form | TESPy's own geometry equations |
| Duty equation | TESPy `UA_group` | TESPy `Q_ohc_group_*` |
| Solved implicitly | yes | yes |
| `UA` visible before the solve | yes | no |

### Why the conductance is worked out here rather than left to TESPy

Four reasons, and a measurement.

**They agree, so the choice is not about accuracy.** `compare_pipe_heat_models`
solves the same pipe both ways in a three-component TESPy network and returns
the two conductances:

```python
from discoolpy import compare_pipe_heat_models

compare_pipe_heat_models(inner_diameter_m=0.2, length_m=800.0,
                         insulation_thickness_m=0.05)
```

Across ordinary district geometry — DN125 to DN500, 30 to 80 mm of PUR, 1.2 to
2.5 m of cover, all four of TESPy's ground materials — the closed form runs 7 to
19 % above TESPy's buried group. That gap is not a disagreement about soil. It
is entirely TESPy's Wallentén implementation lumping the steel wall thickness
into the insulation layer: its `Beta` term uses `ln(D_ins/D_inner)` with the
*insulation* conductivity, so 6 mm of steel at 45 W/(m·K) is counted as 6 mm of
PUR at 0.027 W/(m·K). Set the wall thickness to zero and the two agree to within
2 %:

| Geometry | ratio, 6 mm wall | ratio, no wall |
|---|---|---|
| DN200, 50 mm PUR | 1.131 | 0.992 |
| DN200, 30 mm PUR | 1.188 | 0.982 |
| DN125, 50 mm PUR | 1.155 | 0.998 |
| DN500, 80 mm PUR | 1.066 | 0.985 |

The arccosh line-source solution and the first-order multipole solution are
therefore interchangeable at these depths, which is the result you would hope
for. Keeping a separate steel-wall resistance is the physically defensible half
of the difference.

**A shared trench has no native term.** TESPy's buried group is a single-pipe
model. Supply and return normally share a trench, and the warmer return line
raises the effective ground temperature the supply line sees. `twin_spacing_m`
adds the mutual resistance for that, which lowers the gain by 3.6 % in moist
soil and 11.4 % in dry soil on a DN200 pair at 0.7 m centres. There is no way
to say it natively.

**Three ordinary cases the native groups refuse.** Each one fails from inside
the residual evaluation, where the message names neither the pipe nor the value:

- *Ground outside four names.* TESPy's buried group knows `gravel`, `stones`,
  `dry soil` and `moist soil`. Sand, clay, rock and wet sand raise a `KeyError`
  mid-solve. The closed form takes those, and any numeric conductivity.
- *A bare or lightly insulated pipe.* TESPy rejects an insulation thickness
  below 1 mm, so an uninsulated run cannot be modelled at all. The closed form
  carries the soil resistance on its own.
- *Still air.* TESPy's surface group is a forced-convection correlation and
  divides by a Reynolds number, so `wind_velocity = 0` raises a
  `ZeroDivisionError`. The closed form stays finite, and adds the radiative term
  that dominates there — at a 43 °C Riyadh ambient, radiation is 38 % of the
  external film on an exposed chilled line, and TESPy's group omits it entirely.

DisCoolPy now checks all three while the scenario is still a scenario, and says
which pipe and what to do about it instead of letting the solver fail.

**A conductance you can see is a conductance you can defend.** `discoolpy check`
prints every pipe's `UA` before anything is solved, so an implausible insulation
value is caught in the specification rather than inferred from an implausible
result. A native group gives no conductance until after a converged solve.

None of which makes the native groups wrong. `tespy_buried` is the better choice
when you want TESPy's formulation specifically, or when you are comparing
against another TESPy model and want the same equations on both sides. It is
supported and tested, and now validated at configuration time. The default is
`ua` because it covers more ground, reports more, and fails earlier.

### Where the conductance comes from

For a buried pipe, four resistances in series:

```
1/U'  =  1/(pi*D_i*h_i)                        internal water film
       + ln(D_o/D_i) / (2*pi*k_steel)          pipe wall
       + ln(D_ins/D_o) / (2*pi*k_ins)          insulation
       + arccosh(2z/D_ins) / (2*pi*k_soil)     soil (semi-infinite medium)
```

`UA = U' * L`. The insulation term dominates by roughly an order of magnitude,
which is why insulation thickness is the design lever that matters and soil type
mostly is not. An optional `twin_spacing_m` adds the mutual-resistance term for
supply and return sharing a trench, which *reduces* net gain relative to two
independent pipes.

For an exposed or in-tunnel pipe the soil term is replaced by an external film
`h = 5.7 + 3.8*v` plus a linearised radiation term.

Typical results, DN200 with 50 mm PUR at 1.2 m in moist soil: **U' ≈ 0.45 W/(m·K)**.
Over a 3 km network at a 22 K drive that is roughly **20-35 kW**, or **2-5 % of
building demand**, which matches reported distribution losses for real systems.

### Buried pipes see the soil, not the air

This distinction changes the answer. Soil at 1.2 m lags the surface by about a
month and swings by roughly 60 % of the surface amplitude, so pipe heat gain is
close to constant over a day while air temperature swings 15 K. Set
`ambient_source: ground` for buried pipes (the default) and `air` for exposed
runs; `discoolpy.thermal.soil_temperature_degC` supplies the Kusuda harmonic and
the profile generator fills the column automatically.

### Example

```yaml
branch:
  pipe_model: darcy
  heat_model: ua
  bypass_dp: null                # free the bypass so every pipe can fix its own drop
  thermal_defaults:
    placement: buried
    insulation: pur              # or a numeric W/(m*K)
    insulation_thickness_m: 0.05
    burial_depth_m: 1.2
    ground: moist soil
    pipe_wall_thickness_m: 0.006
    twin_spacing_m: 0.7
    ambient_source: ground
  pipes:
    supply_1: {L: 800.0, D: 0.20, ks: 0.00005}
    return_1: {L: 800.0, D: 0.20, ks: 0.00005}
    # A tunnel section overrides only what differs:
    supply_2:
      L: 260.0
      D: 0.15
      ks: 0.00005
      placement: surface
      ambient_source: air
      insulation_thickness_m: 0.06
      wind_velocity_m_s: 0.4
```

You can also skip the geometry and give `UA_W_K` or `UA_per_m_W_mK` directly.

### Sizing check

Aim for **1-2 m/s** in the mains. Oversized pipes overstate heat gain (more
surface) and understate pumping at the same time, so the error compounds in a
direction that flatters the design.

---

## Buildings

`load_model` selects how a building's demand is produced.

| `load_model` | Profile value means | Adds |
|---|---|---|
| `profile` | Total demand | Nothing. Pre-upgrade behaviour |
| `envelope` | Internal/process gain only | `UA*(T_air - T_indoor) + solar + infiltration` |
| `thermal_mass` | Internal/process gain only | The above, plus an indoor temperature state |

### Envelope

```
Q = Q_internal(t) + UA_env*(T_air - T_indoor) + g_solar*I(t) + m_inf*cp*(T_air - T_indoor)
```

clipped at zero. Splitting the load this way is what makes weather sensitivity
representable: with a single scalar `Q` there is no term for ambient conditions
to act on.

### Thermal mass, and why it is the flexibility

A first-order zone model:

```
C dT/dt = UA*(T_air - T) + G - Q_cool
```

integrated **exactly** over each step:

```
T(t+dt) = T_inf + (T(t) - T_inf) * exp(-dt/tau),   tau = C/UA
```

Exact integration is not fussiness. A comfort band is a few kelvin wide while a
building's time constant is many hours; a forward-Euler step large enough to be
useful would overshoot the band and report flexibility that does not exist.

Control modes, supplied per snapshot via metadata `{label}_mass_mode`:

| mode | Behaviour |
|---|---|
| `track` | Hold the setpoint. The inflexible reference |
| `precool` | Cool towards the band floor, limited by `precool_max_cooling_W` |
| `coast` | Deliver nothing, capped at the band ceiling |
| `recover` | Return towards setpoint at the pre-cooling rate limit |
| `manual` | Deliver exactly `{label}_cooling_W` |

**Use `recover`.** Going straight from `coast` back to `track` asks for the
setpoint in one step, and every coil in the district goes to full output
simultaneously. The resulting rebound peak can be larger than the peak the event
was called to avoid, the most common way a real pre-cooling scheme fails.

Comfort is a hard constraint: `coast` still spends whatever cooling is needed to
stay under the ceiling. If the terminal units cannot manage it, the step reports
`band_violation_K` and `capacity_limited`, and `run_configured_case` warns.
Load shed by overheating a building is not flexibility.

```yaml
buildings:
  - label: office tower
    Q_design_W: 345000.0         # should match the peak the envelope actually produces
    pr: null
    load_model: thermal_mass
    envelope:
      UA_W_K: 6200.0
      solar_aperture_m2: 125.0   # effective area x SHGC
      infiltration_kg_s: 2.2
      indoor_setpoint_degC: 23.5
    thermal_mass:
      capacitance_MJ_K: 420.0    # heavyweight frame; tau ~ 19 h
      min_temperature_degC: 20.5
      max_temperature_degC: 26.0
      max_cooling_W: 400000.0        # coil capacity, defends the band ceiling
      precool_max_cooling_W: 300000.0  # discretionary control rate limit
```

---

## Cold / ice / PCM storage

### Ambient-driven losses

`standby_loss_fraction_per_day` treats the loss as a constant. It is not. An ice
tank sits near 0 °C, so on a 45 °C afternoon it loses roughly three times what it
loses on a 15 °C night, and the loss is largest exactly when the stored cooling
is worth most. Set `loss_model: ua`:

```
Q_gain = UA * (T_ambient - T_store)
```

`UA` comes from tank volume, insulation and an optional buried fraction, or you
can give it directly. For a 420 m³ tank with 100 mm PUR, `UA ≈ 82 W/K`, i.e.
3.5 kW at 43 °C, more than twice what a flat 1.5 %/day assumption predicts.

Ice and PCM hold a constant medium temperature (phase change). Sensible
chilled-water storage warms as it discharges, so set
`temperature_varies_with_soc: true` and give `discharged_temperature_degC`; its
gain then *falls* at low state of charge, which is one reason the two
technologies behave differently under the same weather.

`loss_model: fraction` (the default) and `both` are also available.

### Hydraulic coupling

This is the more consequential change.

**`coupling: supervisory`** (legacy) keeps the network energy-balanced by
*rescaling every building's heat duty* so the plant load matches the
post-dispatch figure. The solver stays happy, but the building heat exchangers
are no longer serving their real demand, and the reported chilled-water return
temperature is not the return temperature of the real system. It also shifts
cooling at unchanged COP, which systematically flatters storage.

**`coupling: hydraulic`** (recommended) puts the store in the plant section of
the chilled-water loop as a real `SimpleHeatExchanger` carrying

```
Q_hx = charge_power - discharge_power        [W, added to the water]
```

The chiller duty is solved from the loop, so `Q_evap = Q_buildings + Q_pipes +
P_pump + Q_storage` falls out rather than being asserted. It also captures the
penalty that makes ice storage interesting in the first place: to charge in
series the plant must produce water *below* the distribution setpoint, which
depresses the evaporating temperature and costs COP. You can see it directly in
the `chw_plant_supply_T_degC` column.

```yaml
storage:
  enabled: true
  storage_type: ice
  capacity_kWh: 2400.0
  coupling: hydraulic
  hydraulic_pr: 0.998
  loss_model: ua
  thermal:
    volume_m3: 420.0
    insulation: pur
    insulation_thickness_m: 0.10
    storage_temperature_degC: 0.0
  target_chiller_load_kW: 600.0    # near the MEAN plant duty, not the peak
```

Set `target_chiller_load_kW` near the mean plant duty. Set it near the peak and
the store charges nearly every hour, saturates, and stops cycling.

---

## Assessing flexibility

`discoolpy.flexibility.assess_flexibility(reference, flexible, ...)` compares two
result frames and returns a `FlexibilityReport` with `to_markdown()`,
`to_frame()` and `to_dict()`.

It separates four things that are easy to conflate:

- **Peak reduction**: what a demand charge pays for.
- **Energy penalty**: what the flexibility costs in kWh. Usually positive.
- **Thermal round trip**: cooling out ÷ cooling in, corrected for any net change
  in state of charge over the horizon.
- **Electric round trip**: electricity avoided while discharging ÷ electricity
  spent while charging. This is the number that decides whether running the store
  pays, and it differs from the thermal figure because charging and discharging
  happen at different condenser temperatures and different plant supply
  temperatures.

Plus a heat-gain breakdown, and cost/carbon if you supply `economics.tariff` and
`economics.carbon_intensity_kg_kWh` (scalar, hour-of-day mapping, column name, or
full-length sequence).

`flexibility_envelope(buildings, storage, dt_h, ambient, solar)` reports the
deliverable up/down cooling power *right now*, respecting comfort bands, state of
charge and power ratings, biddable rather than nameplate. Building thermal-mass
flexibility is also reported as **energy** (`precool_energy_kWh`,
`coast_energy_kWh`), because power alone does not say how long a bid can be held.

### A representative result

From `config_riyadh_heat_gains.yaml`, one week, 30-minute resolution:

| | Without store | With ice store |
|---|---:|---:|
| Peak compressor power | 201.8 kW | 175.0 kW (−13.3 %) |
| Compressor energy | 26 116 kWh | 26 361 kWh (+0.9 %) |
| Load factor | 0.770 | 0.897 |
| Electricity cost | 3732 | 3454 (−7.4 %) |
| Emissions | 14 364 kg | 14 498 kg (+0.9 %) |
| Pipe gain | 4.0 % of demand | 4.0 % of demand |
| Tank ambient gain | - | 0.6 % of demand |
| Thermal round trip | - | 0.809 |
| Electric round trip | - | 0.853 |

The store cuts peak power and cost while *increasing* kWh and emissions. Whether
that is a good trade depends entirely on the tariff, which is precisely the
question a flexibility assessment should put in front of you rather than hide.

---

## Where to look next

| | |
|---|---|
| `examples/heat_gains_and_flexibility.ipynb` | Full worked analysis of pipes, tank and storage flexibility |
| `examples/precooling_flexibility.ipynb` | Demand-side flexibility from building thermal mass |
| `examples/building_your_own_scenario.ipynb` | Degrees-of-freedom counting and a five-building campus |
| [Cold storage](cold_storage.md) | The storage component in full: dispatch, coupling, bookkeeping |
| `examples/branching_networks.ipynb` | Heat gain reported branch by branch on a forking network |
| [Pipe parameter guide](pipe_parameter_guide.md) | Choosing lengths, diameters, roughness, bearings and pressure anchors |
| [Network topology](network_topology.md) | Branches, terminals, forks, satellite plants |
| [Configuration reference](configuration.md) | Every YAML key, including `thermal_defaults` and `storage.thermal` |
| [API reference](api_reference.md) | Full API |

---

## Heat gains across a branching network

Everything on this page is per pipe, and a pipe belongs to a branch, so all of it
carries over to a network that forks without change. Two things are worth knowing.

**Settings are inherited.** A child branch takes its parent's `heat_model` and
`thermal_defaults` unless it names its own, so a network of mostly-identical
buried mains only needs its exceptions spelled out, the one spur that runs
above ground, the one main with thicker insulation.

**Reporting is namespaced.** `Branch.heat_gain_report()` on the root covers the
whole tree. Root-branch keys stay bare (`supply_1`), descendants are prefixed
with their branch label (`north spur/supply_1`), and the result frame follows the
same convention (`pipe_north_spur_supply_1_Q_W`).

Aggregating gain over a whole network hides where it comes from. Per-branch, in
`config_branching_grid.yaml`:

```
downtown trunk    12.44 kW over   1720 m =  7.23 W/m
north spur         5.58 kW over   1120 m =  4.98 W/m
east spur          5.52 kW over    820 m =  6.73 W/m
south leg          4.92 kW over    560 m =  8.78 W/m
```

The south leg is the shortest route in the network and picks up the most heat per
metre, because it crosses a service bridge in 42 degC air instead of being buried
in 31 degC soil. Nothing but a per-branch breakdown would show that, and it is
exactly the kind of finding that changes an insulation specification.
