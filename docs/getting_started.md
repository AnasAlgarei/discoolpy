# Getting Started

This guide walks you through your first district cooling simulation in under ten minutes. By the end you will have a working two-building network solved at design point and stepped through a 24-hour load profile.

---

## Prerequisites

Make sure you have completed the [Installation](installation.md) steps and that the package is importable:

```bash
python -c "import discoolpy; print('OK')"
```

---

## Step 1 — Choose your configuration method

You can define a network in two equivalent ways:

**Option A — Python dictionary** (good for quick experiments and notebooks):

```python
config = {
    'network': {'chilled_water_cycle_closer': 'chw cycle closer'},
    'design': {
        'supply_temperature_degC': 7.0,
        'building_return_temperature_degC': 12.0,
        ...
    },
    ...
}
```

**Option B — YAML file** (recommended for reproducible projects):

```python
from discoolpy.utils import load_yaml_config
config = load_yaml_config('configs/tutorial_two_building.yaml')
```

The repository ships with ready-to-use YAML files in `configs/`. Start with `tutorial_two_building.yaml`.

---

## Step 2 — Understand the YAML schema

Below is the complete schema for a two-building network. Every key shown is required unless marked *optional*.

```yaml
# ── Network ──────────────────────────────────────────────────────────────
network:
  chilled_water_cycle_closer: chw cycle closer   # label for the cycle-closing connection

# ── Design conditions ─────────────────────────────────────────────────────
design:
  supply_temperature_degC: 7.0          # chilled-water supply temperature
  building_return_temperature_degC: 12.0 # return temperature from buildings
  ambient_temperature_degC: 35.0        # outdoor dry-bulb temperature
  condenser_inlet_temperature_degC: 30.0
  condenser_outlet_temperature_degC: 35.0
  chilled_water_pressure_bar: 3.0       # absolute pressure at chiller outlet
  condenser_pressure_bar: 3.0
  pump_power_W: 300.0                   # design pump shaft power
  pump_efficiency: 0.70
  bypass_mass_flow_kg_s: 0.05           # minimum bypass flow to prevent dead-heading
  cp_water_J_kgK: 4180.0

# ── Chiller ───────────────────────────────────────────────────────────────
chiller:
  label: my_chiller
  T_evap_degC: 2.0                      # evaporator saturation temperature
  T_cond_degC: 40.0                     # condenser saturation temperature
  eta_s: 0.80                           # isentropic efficiency of compressor
  refrigerant: R134a
  native_offdesign:
    enabled: true
    condenser_ttd_u: 5.0                # upper terminal temperature difference (K)

# ── Cooling tower ─────────────────────────────────────────────────────────
cooling_tower:
  label: my_cooling_tower
  fluid:
    water: 1.0
  approach_temperature_K: -5.0         # negative = outlet below ambient
  native_offdesign: false
  start_mass_flow_kg_s: 20.0           # initial guess for condenser-water flow

# ── Buildings ─────────────────────────────────────────────────────────────
# IMPORTANT: set `pr` on all buildings EXCEPT the last one.
buildings:
  - label: building_1
    Q_design_W: 150000.0
    pr: 0.995                           # pressure ratio across substation HX
  - label: building_2
    Q_design_W: 150000.0
    pr: null                            # last building — must be null (bypass rule)

# ── Distribution branch ───────────────────────────────────────────────────
branch:
  label: my_branch
  pump_placement: supply_inlet
  pump_label: main pump
  native_offdesign: false
  pipe_model: pressure_ratio            # or: length_derived_pr
  pipes:
    supply_1:
      Q: 0.0                            # adiabatic (no heat loss)
    supply_2:
      pr: 0.997                         # bridges the two pressure islands
      Q: 0.0
    return_2:
      Q: 0.0
    return_1:
      pr: 0.999                         # anchors the return-header pressure

# ── Solver ────────────────────────────────────────────────────────────────
solver:
  iterinfo: false
  max_iter: 200

# ── Outputs ───────────────────────────────────────────────────────────────
outputs:
  output_dir: my_outputs
```

---

## Step 3 — Pressure constraint rules

> **This is the most common source of errors for new users.**

TESPy's pressure structure matrix requires exactly the right number of independent pressure constraints. Follow these rules:

1. **Last building must have `pr: null`.** The last building in the chain is hydraulically short-circuited by the bypass valve. Setting its `pr` creates a circular dependency that makes the Jacobian singular.
2. **Set `pr` on supply pipes that bridge pressure islands.** In an N-building network, pipes `supply_2` through `supply_N` each bridge two pressure islands and need a `pr` value.
3. **Set `pr` on `return_1`.** This anchors the return-header pressure.
4. **Do not set `pr` on `return_2` through `return_N`.** These are already constrained by the Merge component's pressure-equality equations.

See [Pipe Parameter Guide](pipe_parameter_guide.md) for the full explanation and worked examples for 3- and 4-building networks.

---

## Step 4 — Build and solve the network

```python
from discoolpy.utils import build_standard_branch_system, load_yaml_config

config = load_yaml_config('configs/tutorial_two_building.yaml')

nw, chiller, buildings, branch, cooling_tower, c_chiller_to_cc = \
    build_standard_branch_system(config)

# Design solve
nw.solve(mode='design', max_iter=200)
assert nw.converged, 'Design solve did not converge!'
nw.save('my_design_state')

# Print design-point COP
Q_evap = chiller.evaporator.Q.val
W_comp = chiller.compressor.P.val
print(f'COP = {Q_evap / W_comp:.2f}')
```

---

## Step 5 — Run offdesign snapshots

```python
cp = config['design']['cp_water_J_kgK']
dT = (config['design']['building_return_temperature_degC']
      - config['design']['supply_temperature_degC'])

# Example: step through four load fractions
for frac in [1.0, 0.8, 0.6, 0.4]:
    for b in buildings:
        Q_design = next(bc['Q_design_W'] for bc in config['buildings']
                        if bc['label'] == b.label)
        Q = Q_design * frac            # positive W
        m = Q / (cp * dT)
        b.set_demand(Q)
        b.inlet.set_attr(m=m)

    nw.solve(mode='offdesign', design_path='my_design_state', max_iter=200)

    if nw.converged:
        COP = chiller.evaporator.Q.val / chiller.compressor.P.val
        print(f'Load {frac*100:.0f}%: COP = {COP:.2f}')
```

---

## Step 6 — Interactive tutorial

For a fully worked example with plots and summary statistics, open the Jupyter notebook:

```bash
cd examples
jupyter notebook tutorial_dummy_data.ipynb
```

The notebook covers the same steps above plus a synthetic 24-hour load profile and results visualisation.

---

## Next steps

| Topic | Resource |
|---|---|
| Choosing pipe lengths and diameters | [Pipe Parameter Guide](pipe_parameter_guide.md) |
| Adding cold/ice thermal storage | `examples/storage_comparison_example.py` |
| Three-building planned-length scenario | `configs/riyadh_three_building_length_derived_pr.yaml` |
| Full API reference | [API Reference](api_reference.md) |
