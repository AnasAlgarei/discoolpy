# Cold, ice and PCM storage

How the `ColdStorage` component works: what it stores, how it is dispatched, how
it loses cooling, where it sits in the network, and how to read what it did.

This page is the reference for `discoolpy/cold_storage.py`. For the YAML keys in
isolation see [Configuration reference](configuration.md#storage); for how a
store fits into a flexibility assessment see
[Heat gains and flexibility](heat_gains.md).

---

## Contents

- [What the component is](#what-the-component-is)
- [The central choice: supervisory or hydraulic](#the-central-choice-supervisory-or-hydraulic)
- [State: capacity, SOC and the reserve band](#state-capacity-soc-and-the-reserve-band)
- [Dispatch: deciding the power](#dispatch-deciding-the-power)
- [Energy bookkeeping: the exact step](#energy-bookkeeping-the-exact-step)
- [Losses](#losses)
- [**Stratified tanks**](#stratified-tanks)
- [Hydraulic coupling in the network](#hydraulic-coupling-in-the-network)
- [Stores at satellite plants](#stores-at-satellite-plants)
- [Configuration](#configuration)
- [What a run reports](#what-a-run-reports)
- [Assessing what the store bought](#assessing-what-the-store-bought)
- [Sizing and common failure modes](#sizing-and-common-failure-modes)
- [Known wrinkles](#known-wrinkles)
- [API summary](#api-summary)

---

## What the component is

`ColdStorage` is an **aggregate reservoir of cooling**. It holds an amount of
stored cooling in kWh, accepts a charge or discharge power at each snapshot, and
loses cooling to its surroundings.

By default it does not model stratification, a specific tank geometry's internal
flow, or the phase-change front in an ice bank. Stratification can be turned on
for a chilled-water tank, see [Stratified tanks](#stratified-tanks), and the
other two remain below the resolution at which a district network is planned.

What it models by default, and what most simple storage models get wrong:

1. **The tank gains heat as a function of ambient temperature**, not as a flat
   percentage per day. An ice tank sits near 0 °C, so it loses roughly three
   times as much on a 45 °C afternoon as on a 15 °C night, and the loss is
   largest exactly when the stored cooling is worth most.
2. **The store can be a real element in the chilled-water loop.** Charging then
   forces the plant to produce water *below* the distribution setpoint, which
   depresses the evaporating temperature and costs COP. A model that shifts
   energy at unchanged efficiency systematically flatters storage.
3. **A chilled-water tank can be resolved into layers.** Its delivery
   temperature is then a model output that degrades as the thermocline reaches
   the outlet, so the store stops being useful before its state of charge
   reaches zero. See [Stratified tanks](#stratified-tanks).

One instance represents one store. The scenario's top-level `storage:` section
builds the central store; a `type: plant` terminal can carry its own, see
[Stores at satellite plants](#stores-at-satellite-plants).

Three descriptive `storage_type` values are accepted: `ice`, `chilled_water`
and `pcm`. The type is a label for reporting; the physics comes from the capacity,
efficiencies and the thermal description, not from the name.

---

## The central choice: supervisory or hydraulic

`coupling` decides whether the store is part of the network or an accounting
device sitting beside it. It is the single most consequential storage setting.

### `coupling: supervisory`: the legacy approximation

The store never enters the TESPy network. Instead, every building's heat duty is
**rescaled** so the plant load matches the post-dispatch figure:

```
factor        = (sum of building loads + charge − discharge) / (sum of building loads)
effective_Q_i = Q_i × factor
```

That keeps the solver happy, but it has three consequences worth being explicit
about:

- The building heat exchangers no longer serve their actual demand, so the
  solved chilled-water **return temperature is not the return temperature of the
  real system**.
- Energy is shifted at **unchanged efficiency**. Charging costs nothing beyond
  the round-trip efficiencies you typed in, so the model cannot tell you what
  making ice actually costs.
- The rescale is floored at `minimum_load_fraction` of the real load, so the
  plant is never driven to an implausibly small duty.

It is kept because pre-upgrade scenarios reproduce exactly under it.

### `coupling: hydraulic`: recommended

The store becomes a TESPy `SimpleHeatExchanger` in the **plant section** of the
chilled-water loop, between the cycle closer and the branch supply:

```
    chiller ──► cycle closer ──► [ storage HX ] ──► pump ──► branch supply
       ▲                                                          │
       └──────────────── branch return ◄──────────────────────────┘
```

It carries a real heat flow:

```
Q_hx = charge_power − discharge_power        [W, added to the water]
```

Charging adds heat to the water leaving the plant, so the plant must produce it
colder to still hit the distribution setpoint. Discharging removes heat, so the
plant may run warmer. The chiller's evaporator duty is **released** and solved
from the loop balance rather than asserted:

```
Q_evap = Q_buildings + Q_pipes + P_pump + Q_storage
```

Because the store sits upstream of the point where the supply setpoint is
anchored, the temperature depression falls out of the solve rather than being
imposed. In the shipped Riyadh scenario, charging at 240 kW pulls the plant's
leaving water from the 7.0 °C setpoint down to **5.33 °C, a 1.67 K
depression**, which is exactly `Q / (ṁ · cp)`.

> **Which to use.** Use `hydraulic` unless you are reproducing an old result.
> It is the only mode in which the efficiency penalty of charging is a model
> output rather than an assumption.

Enabling a hydraulic store also switches `plant_control` to
`supply_temperature`, because the loop energy balance now determines the plant
duty and asserting it as well would over-determine the network. See
[Network topology](network_topology.md#energy-exactly-one-free-variable).

---

## State: capacity, SOC and the reserve band

| Quantity | Meaning |
|---|---|
| `capacity_kWh` | Nominal usable cooling capacity. |
| `energy_kWh` | Currently stored cooling. The live state variable. |
| `soc` | `energy_kWh / capacity_kWh`. |
| `min_soc`, `max_soc` | The operating band. |
| `min_energy_kWh` | `capacity_kWh × min_soc`, a floor the store never goes below. |
| `max_energy_kWh` | `capacity_kWh × max_soc`, a ceiling it never exceeds. |

`initial_soc` sets the starting state, clamped into the band at construction.
`reset(soc=None)` returns the store to `initial_soc` (or a value you give) and
clears the dispatch history; `run_configured_case` calls it after the design
solve so a time series always starts from a defined state.

The band is not cosmetic. A real ice bank keeps a reserve so it can respond to
an unexpected call, and never charges to a state where the last increment takes
disproportionate compressor work. Both the loss model and the dispatch limiter
respect the floor: **losses can erode the store down to `min_energy_kWh` but no
further**, and discharge can never take it below.

---

## Dispatch: deciding the power

`dispatch(snapshot, ...)` advances the store by one snapshot and returns a
`StorageDispatchResult`. The power it applies is decided in a strict order of
precedence.

### Sign convention

> **Positive `storage_power_W` means discharge**, the store supplies cooling
> and *reduces* the chiller duty. Negative means charge.
>
> `chiller_load_offset_W = charge_power_W − discharge_power_W`, so it is
> positive while charging (extra plant load) and negative while discharging.

Both conventions appear in the result record and in the output columns. They are
opposite in sign by design: one is written from the store's point of view, the
other from the plant's.

### Precedence

1. **An explicit `requested_storage_power_W` argument** to `dispatch` overrides
   everything. This is how a supervisory controller or an optimiser drives the
   store.
2. **Snapshot metadata `storage_power_W`** (or `<label>_power_W`), a signed
   power, again bypassing mode logic.
3. **A mode**, taken from the `mode` argument, then metadata `storage_mode`,
   then metadata `<label>_mode`, defaulting to `auto`.

### Modes

| Mode | Behaviour |
|---|---|
| `charge` | Charge at `charge_power_W` if given (argument or metadata), else at `max_charge_kW`. |
| `discharge` | Discharge at `discharge_power_W` if given, else at `max_discharge_kW`. |
| `idle` | Zero power. Losses still apply. |
| `auto` | Load-levelling. Also accepted: `load_leveling`, `load-levelling`, `load_level`. |

### Load levelling (`auto`)

The store aims the plant at a flat target:

```
requested_power = base_chiller_load_W − target
```

so it discharges when the plant would otherwise run above the target and charges
when it would run below. The target is resolved from, in order: the
`target_chiller_load_W` argument, snapshot metadata `target_chiller_load_W`, then
`target_chiller_load_kW` on the store. **If no target can be found the store goes
idle** rather than guessing.

`base_chiller_load_W` matters and is easy to get wrong. With heat gains active,
the *plant* duty and the sum of the building loads differ by several per cent, so
a controller aiming at the wrong one will charge during the peak.
`run_configured_case` therefore passes

```
base_load = snapshot.total_building_load + pipe gain + pump heat − satellite duty
```

using the previous snapshot's solved values as a one-step predictor, seeded from
the design solve. Pipe gain varies only weakly with load, which is what makes
that prediction good enough.

### Temperature guards

Two optional guards block operation on weather grounds. Both read the snapshot's
`condenser_inlet_temperature` when present and fall back to
`ambient_temperature`.

| Key | Behaviour |
|---|---|
| `charge_allowed_above_degC` | Charging proceeds only when the temperature is **at or below** this value. |
| `discharge_allowed_below_degC` | Discharging proceeds only when the temperature is **at or above** this value. |

So `charge_allowed_above_degC: 25` means "only charge when it is 25 °C or
cooler", charge overnight, and `discharge_allowed_below_degC: 35` means "only
discharge when it is 35 °C or hotter", discharge into the peak.

> **The key names read backwards relative to what they do.** They are documented
> here by behaviour, which is what the code implements and what a district store
> wants. Do not infer the behaviour from the name.

When a guard blocks a request the mode is reported as `idle_temperature_guard`
and the power is zero.

### Curtailment

The requested power is then clipped by, in order, the power rating and the
energy actually available or storable within the band. Whatever could not be
delivered is reported as `curtailed_request_W`:

```
curtailed_request_W = |requested power| − |delivered power|
```

A run in which this is persistently large is telling you the store is
power-limited or energy-limited against the duty being asked of it.

---

## Energy bookkeeping: the exact step

One call to `dispatch` does the following, in this order.

**1. Snapshot duration.** `dt_h` comes from the snapshot's `resolution`
(a `timedelta`, or a number read as hours). It must be positive.

**2. Record the state before anything happens**: `energy_before_kWh`,
`soc_before`.

**3. Compute the losses** for this interval, evaluated at `soc_before`:

```
gain_kWh       = ambient_heat_gain_W(T_ambient) × dt_h / 1000
fractional_kWh = fractional loss (see below)
loss           = min( max(energy_before − min_energy, 0),  gain_kWh + fractional_kWh )
available      = max( min_energy, energy_before − loss )
```

Losses are applied **before** dispatch, and are capped so they can never take
the store below its reserve floor.

**4. Determine the requested power** by the precedence rules above, then apply
the temperature guards.

**5. Apply the power, limited by rating and by the band.**

Charging:

```
headroom_kWh   = max(max_energy − available, 0)
energy_limit   = headroom_kWh / (charge_efficiency × dt_h)      [kW]
charge_kW      = min(requested, max_charge_kW, energy_limit)
energy_after   = available + charge_efficiency × charge_kW × dt_h
```

Discharging:

```
deliverable_kWh = max(available − min_energy, 0) × discharge_efficiency
energy_limit    = deliverable_kWh / dt_h                        [kW]
discharge_kW    = min(requested, max_discharge_kW, energy_limit)
energy_after    = available − discharge_kW × dt_h / discharge_efficiency
```

**6. Clamp** `energy_after` into `[min_energy, max_energy]` and store it.

Note where the efficiencies act. **Charging**: only
`charge_efficiency × P × dt` of what the plant produces reaches the store.
**Discharging**: delivering `P × dt` to the district costs
`P × dt / discharge_efficiency` from the store. Both losses are real and both
appear in the thermal round-trip figure.

### Worked example

A 1000 kWh store at 50 % SOC, band 5-95 %, `charge_efficiency` 0.90,
`discharge_efficiency` 0.95, tank `UA` 100 W/K at 30 °C ambient, one-hour steps.

**Step 1, charge at 150 kW.**

```
ambient gain   = 100 W/K × (30 − 0) K = 3.00 kW  →  3.000 kWh
available      = 500 − 3.000                     =  497.000 kWh
stored         = 0.90 × 150 kW × 1 h             = +135.000 kWh
energy_after   = 497.000 + 135.000               =  632.000 kWh
chiller offset = +150 kW   (extra plant load)
```

The plant produced 150 kWh of cooling; 135 kWh reached the store.

**Step 2, discharge at 190 kW.**

```
available      = 632 − 3.000                     =  629.000 kWh
drawn          = 190 kW × 1 h / 0.95             = −200.000 kWh
energy_after   = 629.000 − 200.000               =  429.000 kWh
chiller offset = −190 kW   (plant load avoided)
```

The district received 190 kWh; the store gave up 200 kWh.

**Curtailment example.** The same store at 90 % SOC asked to charge at 200 kW
has only 50 kWh of headroom, so it is limited to
`50 / (0.90 × 1) = 55.6 kW`; the remaining 144.4 kW is reported as
`curtailed_request_W` and the store ends the step at exactly `max_soc`.

---

## Losses

`loss_model` selects the channel or channels.

| Value | Channel |
|---|---|
| `fraction` | A flat fraction of stored cooling per day. Pre-upgrade behaviour. |
| `ua` | `Q = UA × (T_ambient − T_medium)`. **Recommended.** |
| `both` | Both, summed. |

`ua` and `both` require a `StorageThermal`; construction raises if one is
missing rather than silently reporting zero loss.

### The UA channel

```
Q [W] = max(0, UA_W_K × (T_ambient − T_medium))
```

Clamped at zero, so a store colder than its surroundings only ever *gains* heat,
it never harvests cooling from a cold night.

The ambient temperature the shell sees is resolved from, in order: snapshot
metadata `<label>_ambient_temperature_degC`, metadata
`storage_ambient_temperature_degC`, then the snapshot's `ambient_temperature`.
The first two exist so a tank in a plant room or a buried pit can be given its
own temperature instead of tracking outdoor dry bulb.

### Medium temperature, and why ice and chilled water differ

`StorageThermal.medium_temperature_degC(soc)` returns the temperature that
drives the gain:

- **Ice and PCM**: the phase change pins the temperature, so it is constant.
  Leave `temperature_varies_with_soc` at `false`.
- **Sensible chilled water**: the mean tank temperature rises as it discharges.
  Set `temperature_varies_with_soc: true` and give
  `discharged_temperature_degC`; the medium temperature is then interpolated
  linearly between the discharged and charged values by SOC.

The consequence is real: a sensible store's ambient gain **falls as it
discharges**, because it is getting warmer and closer to ambient. An ice store
cannot do that, which is one reason the two technologies behave differently
under identical weather.

### Where UA comes from

Either give `UA_W_K` directly, or give `volume_m3` and let it be derived from
geometry by `tank_UA_W_K`:

```
area = external area of a vertical cylinder of that volume and height:diameter
R    = 1/h_internal  +  t_insulation/k_insulation  +  1/h_external
UA   = area / R
```

with `buried_fraction` blending a soil-side film (`k_soil / 0.5 m`) into
`h_external` for a partly buried tank. A plane-wall series resistance is used
rather than a cylindrical one: for a tank the insulation is thin relative to the
diameter, so the curvature correction is well inside the uncertainty of the
insulation conductivity itself.

For the shipped Riyadh store (420 m³, 100 mm PUR, height-to-diameter 1.2) this
gives **UA = 82.2 W/K**, and therefore:

| Ambient | Gain |
|---:|---:|
| 15 °C | 1.23 kW |
| 30 °C | 2.47 kW |
| 45 °C | 3.70 kW |

against **1.50 kW constant** from a 1.5 %/day flat model. At the afternoon peak
the UA model is 2.5× the flat one, and 3.0× its own night-time value.

### The fractional channel

```
loss_fraction = 1 − (1 − standby_loss_fraction_per_day) ^ (dt_h / 24)
loss_kWh      = min(energy_before − min_energy,  energy_before × loss_fraction)
```

Compounded rather than pro-rated, so the daily figure is honoured exactly
regardless of snapshot resolution. It returns zero once the store is at or below
its floor.

---

## Stratified tanks

Everything above treats the store as a single number: an amount of cooling in
kWh. For most district planning that is the right resolution. It cannot,
however, answer the question a chilled-water tank actually raises in operation,
**at what temperature does the cooling come out, and for how long?**

A chilled-water store has no membrane. Cold water sits at the bottom because it
is denser, warm return water floats on top, and the only thing separating them
is a **thermocline** a fraction of a metre thick. As the tank cycles, that
thermocline thickens by conduction and by the mixing every inflow causes, so
the water leaving the cold port creeps upward in temperature long before the
tank is empty. A district does not want kilowatt-hours; it wants cold water at a
temperature. A scalar state of charge cannot express the difference, and a plant
sized on one will be short of capacity exactly when it matters.

Setting `storage.stratified.enabled: true` replaces the scalar reservoir with a
one-dimensional layered tank. Everything else, the dispatch modes, the
precedence rules, the temperature guards, the hydraulic coupling, the reporting
, works unchanged.

### The model

The tank is divided into `layers` equal-volume nodes, indexed **0 at the bottom**
(coldest) to `n-1` at the top (warmest). Each node is fully mixed, so the stack
is a chain of stirred tanks in series, the standard 1-D multinode formulation,
and the same structure used by
[mosaik-heatpump's hot water tank](https://mosaik.readthedocs.io/en/3.3.1/ecosystem/components/mosaik-heatpump/models/hotwatertank.html).
The direction of use is inverted for district cooling:

```
        CHARGING                          DISCHARGING

   warm out ──┐  ┌── top                warm in ──┐  ┌── top
              │██│  13 °C                         │██│  13 °C
              │██│                                │██│
              │▓▓│  ← thermocline                 │▓▓│  ← thermocline
              │░░│    moves UP                    │░░│    moves DOWN
              │░░│   5 °C                         │░░│   5 °C
   cold in  ──┘  └── bottom             cold out ─┘  └── bottom
```

Charging pushes cold water in at the **bottom** and displaces warm water out of
the **top**; discharging draws cold from the bottom while warm return water
enters the top. In both cases the bulk of the water moves through the stack and
each node exchanges, per unit time:

- **advection** from its upstream neighbour at the through-flow rate,
- **ambient gain** `UA_i · (T_env − T_i)` through its own share of the shell,
- **vertical conduction** to the nodes above and below.

The update is explicit Euler, sub-stepped so that no node exchanges more than
half its own heat capacity in one go. After every sub-step the profile is checked
for buoyant instability: any pair where a lower node is *warmer* than the one
above it is mixed to their mean.

> **A difference from mosaik-heatpump.** That model *swaps* an unstable pair;
> DisCoolPy *mixes* it. Both conserve the same energy, the nodes have equal mass
>, but an unstable pair physically overturns and blends rather than cleanly
> trading places, and mixing converges monotonically instead of being able to
> oscillate.

`discoolpy/stratified.py` imports no TESPy, so the physics is testable in closed
form: energy in equals energy stored to nine significant figures, and the
standing loss equals the shell UA times the driving difference.

### Capacity is not an input

The geometry and the temperature span determine it:

```
capacity_kWh = m · cp · (T_discharged − T_charged) / 3.6e6
```

so `capacity_kWh` in the scenario is **ignored** when a tank is present. This is
a real constraint the scalar model let you dodge: the campus scenario previously
declared 3200 kWh alongside a 2800 m³ volume, figures that disagree by a factor
of eight. If you want a particular capacity, size the volume for it:

```
V = E · 3.6e6 / (ρ · cp · ΔT)
```

Stored cooling is measured **against the discharged temperature**, per layer and
floored at zero, so a tank sitting entirely at the return temperature has a state
of charge of zero.

### The temperatures must match the network

The tank charges to whatever the plant supplies and discharges to whatever the
district returns. So:

- `charged_temperature_degC` should be the **supply setpoint**;
- `discharged_temperature_degC` should be the **design return temperature**.

A tank asked to charge colder than the plant supplies can never fill: it drifts
isothermal and quietly stops storing anything. A scalar store tolerated the
mismatch because its temperatures only fed the UA loss term; here they are
structural. `build_system` checks both and warns, and records the warning in
`system.notes`:

```
Stratified tank 'chilled_water_store' is configured to charge to 5.0 degC but the
plant supplies 7.0 degC, so it can never reach that state and its usable capacity
will be smaller than configured. Set charged_temperature_degC to the supply setpoint.
```

### How dispatch changes

The controller still asks for a **power**, so every mode, guard and precedence
rule described above applies unchanged. What differs is that the power is not
simply granted:

1. The requested power is clipped so one step cannot drive the tank through its
   state-of-charge band.
2. It is converted into the mass flow that would deliver it **against the
   temperature difference the tank can offer right now**,
   `ṁ = P / (cp · ΔT)`, where ΔT is between the warm top and the arriving cold
   water while charging, or between the warm return and the cold bottom while
   discharging.
3. That flow is clipped to `max_tank_flow_kg_s`.
4. The tank is stepped, and the power that comes back is whatever the layer
   physics actually produced.

This is the whole point. As the thermocline reaches the outlet the available
temperature difference collapses, so the same requested power needs a flow the
tank cannot pass, and the delivered power falls away, while a scalar store would
happily keep delivering its rated power until state of charge hit zero. The
shortfall appears in `storage_curtailed_request_W` like any other curtailment.

A worked drain of a 600 m³ tank at 40 kg/s, from full:

| hour | SOC | outlet °C | delivered kW |
|---:|---:|---:|---:|
| 1 | 0.76 | 5.00 | 1337 |
| 2 | 0.52 | 5.01 | 1336 |
| 3 | 0.28 | 5.10 | 1321 |
| 4 | 0.10 | **6.82** | **1033** |
| 5 | 0.02 | 10.45 | 427 |

At hour 4 the thermocline reaches the port: the outlet warms by nearly 2 K and a
fifth of the power disappears, with 10 % of the charge still nominally in the
tank.

### Losses

The tank owns its standing loss and computes it layer by layer from
`wall_htc_W_m2K` times each layer's share of the shell, so it satisfies
`loss_model: ua` on its own and needs **no** separate `thermal` block. Combining
a stratified tank with `loss_model: fraction` or `both` is refused rather than
silently double-counted.

One-way efficiencies are likewise **not** applied. With a resolved tank the round
trip is emergent: it comes out of mixing, conduction and shell gain, so
applying `charge_efficiency` on top would count the same loss twice. Leave them
at their defaults; they are ignored.

### Configuration

```yaml
storage:
  enabled: true
  label: chilled_water_store
  storage_type: chilled_water
  initial_soc: 0.67
  max_charge_kW: 420.0
  max_discharge_kW: 420.0
  coupling: hydraulic          # a stratified tank wants to be in the loop
  loss_model: ua               # the tank owns its shell loss, layer by layer

  stratified:
    enabled: true
    volume_m3: 425.0
    layers: 14
    height_to_diameter: 2.6           # tall and slender stratifies better
    charged_temperature_degC: 7.0     # == the plant's supply setpoint
    discharged_temperature_degC: 13.5 # == the district's return
    wall_htc_W_m2K: 0.22
    vertical_conductivity_W_mK: 0.897

  max_tank_flow_kg_s: 15.5
  target_chiller_load_kW: 1420.0
  min_soc: 0.05
  max_soc: 0.95
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn the layered model on. |
| `volume_m3` | from `thermal.volume_m3` | Tank volume. |
| `height_m`, `diameter_m` | - | Explicit geometry; any two of volume/height/diameter fix the third. |
| `height_to_diameter` | `2.5` | Used when only a volume is given. |
| `layers` | `12` | Number of equal-volume nodes. |
| `charged_temperature_degC` | from `thermal.storage_temperature_degC` | The cold end of the span. |
| `discharged_temperature_degC` | from `thermal.discharged_temperature_degC` | The warm end, and the energy reference. |
| `wall_htc_W_m2K` | `0.28` | Shell heat transfer coefficient. |
| `vertical_conductivity_W_mK` | `0.897` | Effective node-to-node conductivity, larger than still water because it stands in for wall conduction and small-scale mixing. |
| `max_courant` | `0.5` | Sub-step control. Lower is more accurate and slower. |
| `max_sub_steps` | `4000` | Cap per call, so a pathological flow cannot hang a run. |

`max_tank_flow_kg_s` sits on the `storage` section rather than inside
`stratified`, because it is a property of the pumps and diffusers rather than of
the vessel. It defaults to whatever `max_charge_kW` implies across the design
span.

### Extra result columns

A stratified tank adds these to every result row, on top of the standard storage
columns. They exist only when a tank is in use, so a with/without comparison
simply will not have them on the reference side.

| Column | Meaning |
|---|---|
| `storage_tank_mass_flow_kg_s` | Water actually pushed through the tank. |
| `storage_tank_outlet_temperature_degC` | Flow-weighted outlet over the step. **While discharging this is what the district receives.** `NaN` when idle. |
| `storage_tank_inlet_temperature_degC` | What arrived at the inlet port. |
| `storage_cold_port_degC`, `storage_warm_port_degC` | Bottom and top port temperatures. Unlike the outlet these are defined even when idle, so a plot of them shows the ports drifting during standby. |
| `storage_thermocline_m` | Thickness of the transition zone, by the 20/80 band convention. |
| `storage_tank_T0_degC` … | The full profile, bottom first. |

### Reading the thermocline

`storage_thermocline_m` is the height over which the profile passes between 20 %
and 80 % of the way from charged to discharged. A freshly filled tank reports
roughly zero, there is no transition inside it, only cold water. The figure
grows as the tank cycles, and when it approaches the tank height the store has
effectively lost its usable capacity even though its mean temperature may still
look healthy.

### Sizing a stratified tank

**Match the temperatures to the network first.** Everything else follows from
the span they define.

**Then size the volume for the capacity you want**, not the other way round.

**Use enough layers.** Twelve to sixteen is a good range. Below about six the
thermocline is wider than the discretisation can represent and the model will
understate the usable capacity; above about thirty the extra resolution buys
little and costs sub-steps.

**Make it tall and slender.** A `height_to_diameter` of 2-4 stratifies far better
than a squat vessel, because the thermocline occupies a smaller fraction of the
height. This is why real chilled-water TES vessels are shaped the way they are,
and the model reproduces the effect rather than assuming it.

**Set `max_tank_flow_kg_s` to what the diffusers can actually pass.** Real
diffusers are sized to keep the inlet Froude number low precisely so they do not
destroy the thermocline; a model allowed unlimited flow will report capacity a
real tank could not deliver.

### What it does and does not buy you

Over a horizon where the store is never driven deep, a stratified tank and a
scalar one of the same capacity give nearly the same answer, and that is a
legitimate result, not a failure of the model. The campus scenario at its shipped
settings agrees to within about half a per cent on peak compressor power.

The difference appears when the store is pushed. Driven hard, the campus tank's
cold port climbs all the way to the district's **return** temperature, at which
point it is circulating water that does no cooling at all, while its state of
charge still reads a few per cent. The scalar model has no such curve to draw: it
has an amount, not a temperature, so it reports the store as merely low rather
than useless.

So the value of resolving the tank is not that it always changes the number. It
is that it tells you **when the simpler model would have misled you**, and it
gives you the delivery temperature, which is what a district actually contracts
on.

### Limitations

- **One dimension.** No radial variation, no plume modelling, no explicit
  diffuser physics. Inlet mixing is represented only through the fully-mixed node
  assumption and the effective vertical conductivity, so a badly designed
  diffuser cannot be modelled directly, cap `max_tank_flow_kg_s` instead.
- **Sensible heat only.** An ice or PCM store is not stratified in this sense;
  the phase change pins the temperature. Keep those on the scalar model with
  `loss_model: ua`.
- **One-step temperature lag.** The tank's port conditions come from the previous
  snapshot's solved supply and return temperatures, seeded from the design solve.
  This is the same one-step predictor the parasitic-gain estimate already uses,
  and for the same reason: those temperatures are only known after the solve.
- **The thermocline is grid-dependent.** A coarse tank reports a thicker
  thermocline than a fine one, because a node is the smallest transition it can
  represent. Conclusions drawn from the model should be checked against a finer
  discretisation; the shipped tests verify the qualitative result holds at 8, 12
  and 20 layers.

### API

```python
from discoolpy import StratifiedTank, TankStepResult
```

| Member | Purpose |
|---|---|
| `StratifiedTank(volume_m3, height_m, diameter_m, height_to_diameter, layers, charged_temperature_degC, discharged_temperature_degC, initial_soc, wall_htc_W_m2K, vertical_conductivity_W_mK, max_courant, max_sub_steps)` | Construction. |
| `step(duration_h, mass_flow_kg_s, inlet_temperature_degC, mode, ambient_temperature_degC)` | Advance one interval; returns a `TankStepResult`. |
| `mass_flow_for_power(power_W, inlet_temperature_degC, mode)` | Flow needed for a power against the *current* profile; `inf` when the span has collapsed. |
| `reset(soc)` | Set the profile to a state of charge, with a sharp front. |
| `energy_kWh`, `capacity_kWh`, `soc` | State. |
| `cold_port_temperature_degC`, `warm_port_temperature_degC` | The two ports. |
| `thermocline_thickness_m(low=0.2, high=0.8)` | Transition thickness. |
| `temperatures_degC` | The raw profile, bottom first. |
| `profile()` | Layer records with height, temperature and charged fraction. |
| `describe()` | Static geometry, capacity and shell UA. |

`TankStepResult` carries `mode`, `mass_flow_kg_s`, `duration_h`,
`outlet_temperature_degC`, `inlet_temperature_degC`, `charge_power_W`,
`discharge_power_W`, `loop_heat_W`, `ambient_heat_gain_W`, the energy and SOC
before and after, `thermocline_thickness_m`, `profile_degC` and `sub_steps`;
plus `to_record(prefix="tank")`.

---

## Hydraulic coupling in the network

### Assembly

`build_system` wires a hydraulic store automatically:

```python
storage.create_hydraulic_element()          # a SimpleHeatExchanger, made once
Connection(cycle_closer, "out1", storage.heat_exchanger, "in1")
branch.connect_between(storage.heat_exchanger, "out1", chiller, "in1")
storage.set_design(idle_Q_W=0.0)
```

To place one by hand, `connect_between(source, source_port, sink, sink_port)`
inserts it in series and returns the two connections.

### Design point

`set_design(idle_Q_W=0.0)` sizes the network around the **idle** store, zero
heat flow, and applies `hydraulic_pr` as the water-side pressure ratio. Sizing
around a charging or discharging tank would bake a transient operating mode into
every offdesign solve.

### Each snapshot

`apply_dispatch_to_network(result)` sets the element's duty to
`result.chiller_load_offset_W`, i.e. `charge − discharge`. `run_configured_case`
does this through `TimeSnapshot.apply(..., storage=..., storage_result=...)`.

### The closure check

Everything entering the chilled water must leave through an evaporator. Every
result row carries the residual:

```
chw_energy_residual_W = Q_evap + Q_satellite − Q_satellite_store
                        − Q_buildings − Q_pipes − P_pump − Q_storage_offset
```

It should be numerically zero: the shipped scenarios close to about 1×10⁻⁸ W,
and a run warns if it ever exceeds 1 W. If you change anything about how the
store is coupled, this is the number that tells you whether you got it right.

---

## Stores at satellite plants

A `type: plant` terminal may carry its own `storage:` block, using exactly the
same keys. The store sits **in series downstream of the satellite's
evaporator**, so:

```
delivered to the district = Q_evap − Q_store
```

While the satellite's store charges, the plant runs harder than the district
feels; while it discharges, the district gets more than the plant is making.
Combined with `dispatch: proportional` on the satellite, this is what lets a
distributed chiller **run level while its output follows demand**, which is the
reason to put a store at a satellite at all.

`dispatch_satellite_plants` handles this each snapshot: it computes the duty the
plant should deliver, dispatches the store against that as the base load, and
then sets the chiller to `delivered + stored`.

Reported per plant as `satellite_<name>_storage_Q_W` and
`satellite_<name>_storage_soc`, and in aggregate as `satellite_storage_Q_W`.

See [Network topology](network_topology.md#satellite-plants).

---

## Configuration

```yaml
storage:
  enabled: true
  label: ice_storage
  storage_type: ice              # ice | chilled_water | pcm  (descriptive)

  capacity_kWh: 2400.0
  initial_soc: 0.60
  min_soc: 0.05
  max_soc: 0.97

  max_charge_kW: 260.0
  max_discharge_kW: 260.0
  charge_efficiency: 0.94
  discharge_efficiency: 0.96

  coupling: hydraulic            # hydraulic | supervisory
  hydraulic_pr: 0.998            # water-side pressure ratio when hydraulic

  loss_model: ua                 # ua | fraction | both
  standby_loss_fraction_per_day: 0.015     # used by fraction / both
  thermal:
    volume_m3: 420.0             # ...or give UA_W_K directly
    insulation: pur
    insulation_thickness_m: 0.10
    height_to_diameter: 1.2
    internal_film_W_m2K: 300.0
    external_film_W_m2K: 12.0
    buried_fraction: 0.0
    ground: moist soil
    storage_temperature_degC: 0.0
    discharged_temperature_degC: null
    temperature_varies_with_soc: false

  target_chiller_load_kW: 600.0  # the flat plant duty `auto` aims at
  minimum_load_fraction: 0.08    # supervisory only: floor on the rescaled load
  charge_allowed_above_degC: null      # charge only at or BELOW this
  discharge_allowed_below_degC: null   # discharge only at or ABOVE this
```

Every key has a working default except `capacity_kWh`. `make_storage(config)`
builds the object; `make_storage_from_section(cfg)` does the same from a bare
mapping, which is how a satellite's inline store is built.

> `minimum_load_fraction` is read by `run_configured_case` and defaults to
> **0.08** there. Calling `make_effective_snapshot` or
> `dispatch_and_make_effective_snapshot` directly gets the method default of
> **0.05** instead. The scenario-level value is the one that applies to a normal
> run.

---

## What a run reports

`run_configured_case` emits one row per snapshot. The storage columns:

| Column | Meaning |
|---|---|
| `storage_mode` | The mode actually used, including `idle_temperature_guard`. |
| `storage_power_W` | Signed, store's view: **positive = discharge**. |
| `storage_charge_power_W` | Charge power, ≥ 0. |
| `storage_discharge_power_W` | Discharge power, ≥ 0. |
| `storage_chiller_load_offset_W` | Plant's view: `charge − discharge`. |
| `storage_soc_after` | State of charge at the end of the step. |
| `storage_energy_after_kWh` | Stored cooling at the end of the step. |
| `storage_curtailed_request_W` | Requested but not delivered. |
| `storage_ambient_heat_gain_W` | Instantaneous UA gain. |
| `storage_ambient_heat_gain_kWh` | The same, integrated over the step. |
| `storage_fractional_loss_kWh` | The flat-fraction channel. |
| `storage_medium_temperature_degC` | Medium temperature used for the gain. |

With no store, these are still emitted, with `storage_mode` set to `"none"` and
the numeric fields zero or `NaN`, so a with/without comparison lines up column
for column without special-casing.

`ColdStorage.history` holds every `StorageDispatchResult` in order, and
`history_records(prefix="storage")` flattens it to dictionaries for a DataFrame.

---

## Assessing what the store bought

`assess_flexibility(reference, flexible, config)` compares a run with the store
against one without. Two figures deserve attention.

**Thermal round trip**: cooling out divided by cooling in, corrected for state
of charge drift:

```
cycled_in  = charged  − max(net SOC change, 0)
cycled_out = discharged + min(net SOC change, 0)
thermal_rt = cycled_out / cycled_in
```

A horizon that does not start and end at the same SOC distorts the ratio in both
directions, ending fuller means charge that was never discharged; ending emptier
means discharge that was never charged for. Netting both sides out is the honest
comparison, and the residual drift is reported so you can judge how cyclic the
run really was.

**Electric round trip**: the extra compressor energy spent while charging
against the compressor energy avoided while discharging. This is the number that
decides whether a store is worth running, and it is the one a supervisory model
cannot produce at all. It is **withheld** when the horizon is not cyclic (drift
above 10 % of what was charged), because comparing the charging and discharging
of different energy can produce a ratio above 1, and reporting that would be
worse than reporting nothing.

For the shipped Riyadh week: thermal 0.809, electric 0.853, peak compressor power
−13.3 %, electricity cost −7.4 %, compressor energy **+0.9 %**, emissions +0.9 %.

> A store that cuts peak power and cost while *increasing* kWh and carbon is the
> normal case, not a modelling error. Whether it is a good trade depends entirely
> on the tariff, which is the question a flexibility assessment should put in
> front of you rather than hide.

Note that the electric round trip here (0.853) is *better* than the thermal one
(0.809). Charging happens overnight, when the condenser is cooler and the
machine is more efficient, and that advantage more than offsets the evaporator
penalty from making colder water. The depression is a genuine cost at the
evaporator; whether it dominates is a result, not an assumption.

---

## Sizing and common failure modes

**Set `target_chiller_load_kW` near the mean plant duty, not the peak.** Too
high and the store charges nearly every hour, saturates at `max_soc`, and stops
cycling. The Riyadh scenario aims at 600 kW against a mean plant duty of about
545 kW.

**Start near the cyclic steady state.** `initial_soc` far from where the store
naturally settles wastes the front of the horizon and can suppress the electric
round-trip figure entirely. If a run reports large SOC drift, adjust
`initial_soc` toward where it ended and re-run.

**Match power to capacity.** A store rated at `max_charge_kW` cannot fill its
usable band in less than
`capacity × (max_soc − min_soc) / (charge_efficiency × max_charge_kW)` hours. If
that exceeds the available off-peak window, the capacity is unreachable.

**Watch `storage_curtailed_request_W`.** Persistent curtailment means the
controller is asking for something the store cannot do, the target is
unreachable, or the band is too narrow.

**Check the closure residual** after any change to coupling or dispatch.

---

## Known wrinkles

Two things to be aware of, both in the legacy path:

- **The guard key names are inverted** relative to their behaviour, as described
  in [Temperature guards](#temperature-guards). The behaviour is the sensible
  one; the names are not.
- **A supervisory effective snapshot does not carry ground temperature or solar
  irradiance.** `make_effective_snapshot` rebuilds a `TimeSnapshot` with only the
  timestamp, loads, resolution, ambient and condenser temperatures, and
  metadata. Since `pipe_ambient_temperature` falls back to air when ground is
  absent, buried pipes in a *supervisory storage* run track dry-bulb air rather
  than soil, and envelope models lose their solar term. No shipped scenario hits
  this, all of them use `coupling: hydraulic`, but a scenario combining
  `coupling: supervisory` with `heat_model: ua` pipes would be affected. It is
  one more reason to prefer hydraulic coupling.

---

## API summary

```python
from discoolpy import ColdStorage, StorageDispatchResult, StorageThermal
```

### Construction

```python
ColdStorage(
    label, capacity_kWh, initial_soc=0.5,
    max_charge_kW=250.0, max_discharge_kW=250.0,
    charge_efficiency=0.92, discharge_efficiency=0.94,
    standby_loss_fraction_per_day=0.02,
    thermal=None, loss_model="fraction", coupling="supervisory",
    hydraulic_pr=0.999, storage_type="ice",
    target_chiller_load_kW=None, min_soc=0.05, max_soc=0.98,
    charge_allowed_above_degC=None, discharge_allowed_below_degC=None,
)
```

### State

| Member | Meaning |
|---|---|
| `energy_kWh` | Stored cooling, live. |
| `soc` | State of charge. |
| `min_energy_kWh`, `max_energy_kWh` | The band, in kWh. |
| `reset(soc=None)` | Return to `initial_soc` (or a given SOC); clears history. |
| `history` | Every `StorageDispatchResult`, in order. |
| `history_records(prefix="storage")` | The same, flattened. |

### Dispatch

| Method | Purpose |
|---|---|
| `dispatch(snapshot, base_chiller_load_W=None, mode=None, requested_storage_power_W=None, target_chiller_load_W=None, charge_power_W=None, discharge_power_W=None)` | Advance one step. |
| `request_from_snapshot(...)` | Resolve `(mode, requested power)` without advancing. |
| `ambient_heat_gain_W(T_ambient)` | Current UA gain in W. |

### Hydraulic coupling

| Method | Purpose |
|---|---|
| `create_hydraulic_element()` | Create (once) the `SimpleHeatExchanger`. |
| `heat_exchanger` | That element; raises if the store is not hydraulic. |
| `connect_between(source, source_port, sink, sink_port, ...)` | Insert in series. |
| `connections` | The two connections created. |
| `set_design(idle_Q_W=0.0)` | Design attributes, plus `hydraulic_pr`. |
| `apply_dispatch_to_network(result)` | Push a dispatch onto the element. |

### Supervisory coupling

| Method | Purpose |
|---|---|
| `apply_to_snapshot(...)` | Dispatch and apply the modified load to components. |
| `make_effective_snapshot(snapshot, result, minimum_load_fraction=0.05)` | The rescaled snapshot. |
| `dispatch_and_make_effective_snapshot(...)` | Both, in one call. |

### Helpers

| Function | Purpose |
|---|---|
| `ColdStorage.thermal_from_geometry(volume_m3, insulation_thickness_m, ...)` | Build a `StorageThermal` from tank dimensions. |
| `make_storage(config)` | Build from a full scenario. |
| `make_storage_from_section(cfg)` | Build from a bare `storage` mapping. |
| `tank_UA_W_K(...)`, `tank_surface_area_m2(...)` | The underlying geometry functions. |

### `StorageDispatchResult`

`timestamp`, `mode`, `requested_storage_power_W`, `storage_power_W`,
`charge_power_W`, `discharge_power_W`, `chiller_load_offset_W`,
`energy_before_kWh`, `energy_after_kWh`, `soc_before`, `soc_after`,
`standby_loss_kWh`, `curtailed_request_W`, `effective_chiller_load_W`,
`ambient_temperature_degC`, `medium_temperature_degC`, `ambient_heat_gain_W`,
`ambient_heat_gain_kWh`, `fractional_loss_kWh`; plus
`to_record(prefix="storage")`.

---

## See also

- [Configuration reference](configuration.md#storage), the YAML keys in isolation.
- [Heat gains and flexibility](heat_gains.md), the tank gain in context with pipe and envelope gains.
- [Network topology](network_topology.md#satellite-plants), stores at distributed plants.
- `examples/storage_comparison_example.py`: the flagship with/without run.
- `examples/stratified_storage.ipynb`: the layered tank, worked through with plots.
- `examples/heat_gains_and_flexibility.ipynb`: heat gains, worked through with plots.
- `configs/config_riyadh_heat_gains.yaml`: a hydraulically coupled ice store.
- `configs/config_campus_five_buildings.yaml`: a **stratified** chilled-water tank.
