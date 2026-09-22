# Examples and scenarios

Everything that ships with DisCoolPy, what it demonstrates, how to run it, and what to look at in the output.

Four kinds of thing live here:

- **`configs/*.yaml`**, scenario files. Data, not code. Any of them can be loaded by any script.
- **`examples/blank_scenario.yaml`** and **`examples/blank_scenario.ipynb`**, a fill-in pair. The scenario is the template `discoolpy new` writes, kept in the repository so you can edit it in place; the notebook walks through it cell by cell. Both run unmodified.
- **`examples/*.py`** are runnable scripts: command line, non-interactive, and good for a batch run or a regression check.
- **`examples/*.ipynb`**, notebooks. The same material with explanation and plots, meant to be read as well as run.

All scripts and notebooks expect to be run **from the `examples/` directory**, because their config paths are relative.

---

## Where to start

| If you are… | Start with |
|---|---|
| Installing and want to know it works | `discoolpy check --all` |
| Writing your first scenario | `blank_scenario.ipynb` and `blank_scenario.yaml` |
| New to the tool | `tutorial_dummy_data.ipynb` |
| Laying out a real street network | `branching_networks.ipynb` + `config_branching_grid.yaml` |
| Sizing pipes from planned street lengths | `config_length_pipes.yaml` + [Pipe parameter guide](pipe_parameter_guide.md) |
| Asking "how much heat do the mains gain?" | `heat_gains_and_flexibility.ipynb` |
| Evaluating thermal storage | `storage_comparison_example.py` |
| Modelling a stratified chilled-water tank | `stratified_storage.ipynb` |
| Evaluating demand response / pre-cooling | `precooling_flexibility.ipynb` |
| Writing a scenario from scratch | `building_your_own_scenario.ipynb` |

---

## Scenarios (`configs/`)

### `config_tutorial.yaml`

**Two buildings, adiabatic pipes, no storage.** The smallest complete scenario. Uses `pipe_model: pressure_ratio`, so no geometry at all, which also makes it the scenario that exercises the layout plotter's schematic fallback.

Because nothing gains heat, `plant_control` defaults to the legacy `evaporator_duty`: the plant duty is asserted, and exactly one pipe (`return_1`) is left without an energy specification to close the loop. Useful for understanding the older convention; not what you want for real work.

Design point: 300 kW of building load, 7 °C supply, 12 °C return.

---

### `config_length_derived_pr.yaml`

**Three buildings, street lengths as the user-facing input, adiabatic pipes.** `pipe_model: length_derived_pr` computes each pipe's pressure ratio from `L`, `D` and `ks` with Darcy-Weisbach **at the design mass flow**, then hands TESPy a fixed `pr`.

That freezes the drop at design flow, so it overstates the drop at part load, but a fixed `pr` is much easier for the solver than the full Darcy group. Use it when the part-load hydraulics do not matter.

Demonstrates the "pipes fix the hydraulics, buildings absorb the residual" convention: six pipes fixed, the bypass fixed, all three building legs free.

---

### `config_length_pipes.yaml`

**The same three buildings with native Darcy hydraulics.** `pipe_model: darcy` hands `L`, `D` and `ks` straight to TESPy, which recomputes the drop from the actual mass flow at every snapshot. Slower and harder to converge; correct at part load.

Read it alongside `config_length_derived_pr.yaml`, the pair is the clearest illustration of what the two pipe models actually differ in.

---

### `config_riyadh_heat_gains.yaml`

**The flagship.** Three buildings on a 3 km buried network in Riyadh in July, with a hydraulically coupled ice store.

- Every pipe carries a `ua` heat model derived from its geometry and insulation, so TESPy solves the gain from the actual water temperature.
- Supply mains gain more than return mains, because the supply water is colder and its driving ΔT to the soil is larger.
- The store is **hydraulically coupled**: charging forces the plant to make water below the distribution setpoint, so the COP penalty of making ice appears in the results instead of being assumed away.
- The tank gains heat at `UA·(T_ambient − T_store)` against 45 °C afternoon air.

The plant produces about **8 % more cooling than the buildings consume**. Run it with `storage_comparison_example.py`.

---

### `config_precooling_flexibility.yaml`

**Demand-side flexibility with no store at all.** Three buildings with an envelope model (`UA`, solar aperture, infiltration) and a first-order RC thermal-mass model, so their demand responds to the weather and the zones can be pre-cooled inside a comfort band.

With `load_model: thermal_mass` the number in the demand profile is the *internal gain only*; the delivered cooling comes out of the zone energy balance, integrated exactly over each snapshot. The control mode per snapshot is read from the snapshot metadata: `track` (hold the setpoint, the inflexible reference), `precool` (cool to the band floor), `coast` (deliver nothing and drift to the ceiling).

Comfort is a hard constraint: `coast` still spends whatever cooling is needed to stay under `max_temperature_degC`, and any excursion is reported as `<building>_band_violation_K` and raised as a warning. Flexibility taken out of occupant comfort is not flexibility.

Run it with `precooling_flexibility_example.py`.

---

### `config_campus_five_buildings.yaml`

**Five buildings, mixed buried and above-ground pipes, stratified chilled-water tank.** Exists to prove the tool scales: the branch, the profile generator, the hydraulic checker and the heat-gain models all work for any number of buildings, and pipes can mix placements in one network.

The service-tunnel run to the student residence is `placement: surface` with `ambient_source: air`, so it tracks dry-bulb rather than soil and picks up far more heat per metre on a hot afternoon. The store is a **stratified** chilled-water tank: 425 m³ resolved into 14 layers, so its delivery temperature is a model output that degrades as the thermocline reaches the outlet. Its capacity follows from the geometry and the 7.0 → 13.5 °C span rather than being typed in.

Also demonstrates `pipe_model: darcy` with `fix_pump_power: false` and an explicit `pump_pressure_ratio`.

---

### `config_branching_grid.yaml`

**Six buildings, four branches, two forks, all three terminal types.** The scenario for a network that follows a street grid.

```
central plant
     |
[downtown trunk]  city hall -> conference centre
     |
     +--(north)--> [north spur]  north tower -> teaching block --> bypass
     |
     +--(east) --> [east spur]   airport hotel
                        |
                        +--(south)--> [south leg]  exhibition hall --> bypass
                        |
                        +-----------> satellite plant (chiller + cold store)
```

Demonstrates:

- **Forks.** Two of them, and what that costs in degrees of freedom: every bypass and the satellite's balancing valve is free, because the pipes already anchor both ends of each parallel path.
- **Building order per branch.** Each branch lists its own buildings; the first is the first tap after the inlet.
- **A satellite plant** with its own chiller, cooling tower and hydraulically coupled store, dispatched `proportional` so what it delivers follows the district while its own machine runs level.
- **Plan geometry.** Every pipe carries `heading_deg`, so `plot_network` draws the actual street grid.
- **Mixed placements down the tree.** The south leg crosses a service bridge (`placement: surface`, `ambient_source: air`) and picks up more heat per metre than any buried main in the network despite being the shortest route.

Run it with `branching_network_example.py` or read `branching_networks.ipynb`.

---

## The fill-in pair

### `blank_scenario.yaml` and `blank_scenario.ipynb`

**A blank pair to fill in.** The scenario file is the annotated template `discoolpy new` writes, kept in the repository so you can open it, edit it and run it without generating anything first. The notebook walks the same file cell by cell.

```bash
cd examples
discoolpy check blank_scenario.yaml
discoolpy run blank_scenario.yaml -n 48
jupyter lab blank_scenario.ipynb
```

Both run unmodified, so you can execute everything once to see the shape of a working scenario before touching a number. Every value you are meant to replace is marked TODO.

The scenario starts with `branch.autofill: true` and no pipe geometry, which is the fastest way to get a network solving. The notebook then shows the next step: real lengths, diameters and bearings, with `pipe_model: darcy` and `heat_model: ua`, so pressure drop and heat gain both come out of the geometry. Optional blocks for storage, a stratified tank, building thermal mass, satellite plants and sub-branches are commented out in the YAML, ready to be switched on one at a time.

The notebook ends with a list of things worth trying, in rough order of how much they change the answer.

---

## Scripts (`examples/`)

### `validate_scenario.py`

**The design-point sanity check. Run this first, and after every scenario edit.**

```bash
python validate_scenario.py                                   # default scenario
python validate_scenario.py --config ../configs/config_branching_grid.yaml
python validate_scenario.py --all                             # every shipped scenario
python validate_scenario.py --all --layout                    # and draw each one
```

The same check is `discoolpy check`, and `discoolpy.check_scenario` from Python. This script is the argparse wrapper that predates the CLI.

Solves the design point only: fast, and prints, branch by branch:

1. **The topology.** Which street carries which buildings, in what order, and how each one ends, with the design mass flow at every branch inlet and terminal.
2. **The hydraulic degree-of-freedom report.** Loops, nodes, specified elements. If it is ill-posed, the offending element is named and a valid specification suggested.
3. **Every pipe's heat model** and conductance, with the ambient source and temperature it tracks.
4. **The design solve** and the plant energy balance, including the closure residual (must be ~0).
5. **The operating point**: compressor power, COP, fleet COP if there are satellites, supply and return temperatures, and ΔT.
6. **Hydraulic feasibility**, whether any element's pressure *rises* along the flow, which is the signature of an undersized pump.
7. **Solved pressures and duties** for every building and terminal, so you can see each balancing valve's authority.

With `--layout` it also writes a plan-view drawing of each network into `outputs/layouts/`.

---

### `storage_comparison_example.py`

**The flagship storage-flexibility run.** Executes `config_riyadh_heat_gains.yaml` twice over a week of Riyadh July at 30-minute resolution, once without the store, once with, and produces a full flexibility assessment.

```bash
python storage_comparison_example.py
```

Outputs a comparison CSV, a Markdown summary, and a plot into `outputs/riyadh_heat_gains/`.

When the scenario's store is a **stratified tank** the run also reports what a scalar model cannot: the temperature the tank actually delivered, how far the thermocline spread, how much of the output came back within half a kelvin of the setpoint, and a picture of the final profile. The campus scenario carries one:

```bash
python storage_comparison_example.py --config ../configs/config_campus_five_buildings.yaml
``` The assessment reports peak reduction, energy penalty, thermal *and* electric round-trip efficiency, cost and carbon. A store that cuts peak power and cost while *increasing* kWh and emissions is the normal case, and whether that is a good trade depends entirely on the tariff.

---

### `precooling_flexibility_example.py`

**Pre-cool and coast within a comfort band, with no store.**

```bash
python precooling_flexibility_example.py
```

Runs `config_precooling_flexibility.yaml` twice, a setpoint-tracking reference and a pre-cooling schedule, and reports how much load was actually shifted, at what energy cost, and whether comfort held. Writes to `outputs/precooling_flexibility/`.

---

### `branching_network_example.py`

**The forking network, and what a satellite plant is worth.**

```bash
python branching_network_example.py
python branching_network_example.py --periods 96      # two days
python branching_network_example.py --no-plots
```

1. Builds `config_branching_grid.yaml`, prints the tree, and draws the layout, before solving anything.
2. Solves the design point and reports the energy balance with its closure residual, plus **heat gain branch by branch** in W per metre of route.
3. Runs the time series twice: once as written, and once with the satellite plant replaced by a plain bypass carrying the same slipstream. Same hydraulics, no chiller, so the difference is attributable to the plant.

The comparison is deliberately honest. The satellite takes about 10 % off the *central* machine's peak duty and power, but the *fleet* peak barely moves and fleet energy rises, because the satellite is a smaller, less efficient machine. A satellite plant moves duty; it does not create it.

Writes to `outputs/branching_grid/`.

---

## Notebooks (`examples/`)

### `tutorial_dummy_data.ipynb`

**Start here.** A 24-hour walkthrough of the two-building tutorial network with dummy data: build, solve at design, step through a load profile, plot the results. No heat gains, no storage, no branching, just the mechanics.

---

### `heat_gains_and_flexibility.ipynb`

**Where the conductances come from, and what they cost.** Derives a buried pipe's `UA` per metre from its geometry and insulation and checks it against the closed-form result; shows the soil temperature model; then runs a week of `config_riyadh_heat_gains.yaml` with and without the ice store and works through the flexibility assessment.

The notebook to read when the question is "how much heat does my network actually gain, and does the store pay for itself?".

---

### `stratified_storage.ipynb`

**The layered chilled-water tank.** Builds a tank from geometry, watches the thermocline travel during charge and discharge, and shows the delivery temperature degrading as the front reaches the outlet, the thing a scalar store cannot express.

Then puts it in the campus network, coupled hydraulically, and compares it against a scalar store of exactly the same capacity. The honest finding: over a horizon where the store is never driven deep the two agree closely, and the difference only appears when the tank is pushed, at which point the cold port climbs to the district's return temperature while state of charge still reads a few per cent.

---

### `precooling_flexibility.ipynb`

**Demand-side flexibility from building thermal mass.** The RC zone model, the comfort band, and the three control modes. Shows the load shift, the energy penalty, and the indoor temperature trajectories, including what happens when the terminal units are capacity-limited and the "flexibility" is really a comfort failure.

---

### `building_your_own_scenario.ipynb`

**Degrees-of-freedom counting, from first principles.** Builds the five-building campus scenario step by step, counting pressure and energy degrees of freedom as it goes, and deliberately breaking them to show what each error message looks like. The notebook to read before writing your first scenario from scratch.

---

### `branching_networks.ipynb`

**Branching, and the layout plotter.** Reads the tree, checks it is well posed, breaks it on purpose to show the fork's degree-of-freedom trap, draws the network as a plan, solves it, reports heat gain per branch, runs a day of operation, and compares the network with and without its satellite plant.

Ends with a short grammar for writing your own forking scenario and the four rules that cover almost everything that goes wrong.

---

## Running everything

A quick end-to-end check of the whole repository:

```bash
discoolpy check --all --layout                # every scenario, design point
cd examples
python branching_network_example.py --periods 48         # a forking network over a day
python storage_comparison_example.py                     # the flagship storage run
cd ..
pytest                                                   # 459 tests
```

---

## See also

- [Configuration reference](configuration.md), every key any of these scenarios uses.
- [Network topology](network_topology.md), what the branching scenario is doing.
- [Heat gains and flexibility](heat_gains.md), the physics behind the Riyadh and campus scenarios.
- [Network layout plots](layout.md), the plotter used by `--layout`.
