# Installation

DisCoolPy is a pure-Python package. Everything it needs is on PyPI and conda-forge, with the usual caveat about CoolProp needing a compiler on exotic platforms.

The recommended path is the conda environment file, which pins the dependency versions the tool is tested against.

---

## Requirements

| Requirement | Minimum | Notes |
|---|---|---|
| Python | 3.9 | Tested on 3.9 - 3.12 |
| TESPy | 0.9.16 | Tested on 0.9.16 and 0.11.0. Earlier releases lack `Network(iterinfo=…)`, `nw.units.set_defaults` and `Valve.dp`. |
| CoolProp | 6.4 | Fluid properties |
| numpy | 1.21 | |
| pandas | 1.5 | Result frames and profiles |
| matplotlib | 3.5 | Result plots and network layouts |
| PyYAML | 6.0 | Scenario files |
| scipy | 1.7 | |
| Jupyter | 1.0 | Optional, only for the notebooks |
| pytest | 7.0 | Optional, only for the test suite |
| Sphinx | 7.0 | Optional, only to build the documentation |

---

## Option A. Conda environment (recommended)

```bash
# 1. Clone the repository
git clone https://github.com/AnasAlgarei/discoolpy.git
cd discoolpy

# 2. Create the environment from the provided file
conda env create -f environment.yml

# 3. Activate it
conda activate discoolpy

# 4. Install the package in editable mode
pip install -e .
```

Editable mode (`-e`) matters: the examples and notebooks all `import discoolpy`, and several of them run from the `examples/` directory rather than the repository root.

---

## Option B. Pip and a virtual environment

```bash
git clone https://github.com/AnasAlgarei/discoolpy.git
cd discoolpy

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e .                   # pulls every runtime dependency
pip install -e ".[notebooks]"      # and Jupyter, if you want the notebooks
pip install -e ".[dev]"            # and pytest, black, flake8
pip install -e ".[docs]"           # and Sphinx, to build these pages
```

> **CoolProp** ships wheels for common platforms. If `pip install coolprop` tries to build from source and fails, install it from conda-forge instead: `conda install -c conda-forge coolprop`.

---

## Option C. Managed or HPC environment

When you cannot create a new environment:

```bash
pip install --user tespy coolprop pandas numpy matplotlib pyyaml scipy
pip install --user -e /path/to/discoolpy
```

---

## Verifying the installation

```bash
python -c "
import discoolpy
print('DisCoolPy', discoolpy.__version__)
from discoolpy import (
    Branch, Building, Chiller, ColdStorage, CoolingTower,
    EndBypass, SatellitePlant, SubBranch,
    build_system, load_yaml_config, plot_network,
)
print('All imports OK.')
"
```

Expected:

```
DisCoolPy 1.0.0
All imports OK.
```

The install also puts a `discoolpy` command on your path:

```bash
discoolpy --version
discoolpy list            # the scenarios shipped with the package
```

Then solve every shipped scenario at its design point. This takes well under a minute and is the fastest way to confirm the whole stack works:

```bash
discoolpy check --all
```

For each scenario it prints the branch tree, the hydraulic degree-of-freedom report, every pipe's heat conductance, the design solve, the plant energy balance with its closure residual, and the solved pressures branch by branch. Add `--layout` to write a plan-view drawing of each network into `outputs/layouts/` as well.

Exit status is 0 when every scenario is usable, so the same command works as a smoke test in CI.

---

## Running the test suite

```bash
pip install -e ".[dev]"
pytest                    # 459 tests, about a minute
pytest -m "not slow"      # unit tests only, a few seconds
pytest tests/test_branching.py -v
```

The suite covers the heat-transfer physics in closed form, exact RC integration and comfort-band enforcement, storage state-of-charge conservation, flexibility metric arithmetic, branching topology and degrees of freedom, layout geometry in metres, the scenario defaults and the CLI, and an end-to-end check that every shipped scenario reaches a converged design point with a chilled-water energy balance that closes to under 1 W.

---

## Running the notebooks

```bash
pip install -e ".[notebooks]"
cd examples
jupyter lab            # or: jupyter notebook
```

Open `tutorial_dummy_data.ipynb` first. The notebooks expect to be run **from the `examples/` directory**, because their config paths are relative (`../configs/...`).

If you use a dedicated environment, register it as a kernel so the notebooks can find it:

```bash
python -m ipykernel install --user --name discoolpy-venv --display-name discoolpy
```

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'discoolpy'`**
Run `pip install -e .` from the repository root, and check you are in the environment you think you are (`which python` / `where python`).

**A notebook cannot import `discoolpy` even though the shell can**
The notebook is using a different kernel. Register the environment as a kernel (above) and select it from *Kernel → Change kernel*.

**`CoolProp` installation fails**
`conda install -c conda-forge coolprop`.

**Lots of TESPy solver output**
Set `solver.iterinfo: false` in the scenario. To silence the part-load characteristic-line notices as well:

```python
import logging
logging.getLogger("TESPyLogger").setLevel(logging.ERROR)
```

Those notices mean an operating point fell outside a compressor characteristic's tabulated range. TESPy extrapolates correctly; DisCoolPy enables extrapolation deliberately so a deep part-load snapshot does not abort the run.

**`FutureWarning: kA is deprecated`**
Harmless. TESPy renamed `kA` to `UA`; DisCoolPy picks whichever the installed version provides. Filter it with `warnings.filterwarnings("ignore", category=FutureWarning)`.

**`ValueError: Over-determined hydraulics: …`**
The scenario fixes more pressure drops than the network has independent nodes. The message names the redundant element and the loop it closes. See [Network topology](network_topology.md) and [Pipe parameter guide](pipe_parameter_guide.md). Setting `branch.auto_relax_pressure: true` lets DisCoolPy repair it and tell you what it released.

**`ValueError: Chilled-water loop is under-specified: pipe(s) … have no energy specification`**
Under `plant_control: supply_temperature` (the default whenever heat gains are active) every pipe needs a heat model. `heat_model: adiabatic` sets `Q = 0` and counts.

**`RuntimeError: Design solve … did not converge`**
Work through the checklist in [Pipe parameter guide](pipe_parameter_guide.md#troubleshooting). The usual causes are an undersized pump (check `branch.pressure_feasibility()`), a satellite plant whose duty over its slipstream would drive the water below freezing, or pipe diameters far from the 0.8-2.5 m/s velocity band.
