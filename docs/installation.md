# Installation

This page covers all supported installation paths. The recommended approach is the conda environment file, which pins all dependency versions and avoids conflicts.

---

## Requirements

| Requirement | Minimum version |
|---|---|
| Python | 3.9 |
| TESPy | 0.7 |
| CoolProp | 6.4 |
| pandas | 1.5 |
| numpy | 1.21 |
| matplotlib | 3.5 |
| PyYAML | 6.0 |
| Jupyter (optional, for notebooks) | 7.0 |

---

## Option A — Conda environment (recommended)

This installs all dependencies into an isolated conda environment.

```bash
# 1. Clone the repository
git clone https://github.com/AnasAlgarei/discoolpy.git
cd discoolpy

# 2. Create the environment from the provided file
conda env create -f environment.yml

# 3. Activate the environment
conda activate discoolpy

# 4. Install the package in editable mode
pip install -e .
```

---

## Option B — pip only (no conda)

If you prefer to use pip inside an existing virtual environment:

```bash
# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Install dependencies
pip install tespy coolprop pandas numpy matplotlib pyyaml jupyter

# Install the package in editable mode
pip install -e .
```

> **Note:** CoolProp may require a C++ compiler on some platforms. If `pip install coolprop` fails, install it via conda: `conda install -c conda-forge coolprop`.

---

## Option C — Manual dependency installation

If you are working in a managed HPC or cloud environment where you cannot create new environments:

```bash
pip install --user tespy coolprop pandas numpy matplotlib pyyaml
pip install --user -e /path/to/discoolpy
```

---

## Verifying the installation

Run the following to confirm everything is importable:

```bash
python -c "
import discoolpy as mdc
print('Package version:', getattr(mdc, '__version__', 'dev'))
from discoolpy.utils import build_standard_branch_system, load_yaml_config
from discoolpy.chiller import Chiller
from discoolpy.branch import Branch
from discoolpy.building import Building
from discoolpy.cold_storage import ColdStorage
print('All imports OK.')
"
```

You should see:

```
Package version: dev
All imports OK.
```

---

## Running the tutorial notebook

```bash
cd examples
jupyter notebook tutorial_dummy_data.ipynb
```

The notebook is self-contained and uses only the package and standard scientific Python libraries.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'discoolpy'`**
Run `pip install -e .` from the repository root. Make sure you are in the correct conda/virtual environment.

**`CoolProp` installation fails**
Install via conda: `conda install -c conda-forge coolprop`.

**TESPy convergence warnings during examples**
These are informational messages from TESPy's iterative solver and do not indicate failure. Set `iterinfo: false` in your YAML `solver` section to suppress them.

**`AssertionError: Design solve did not converge`**
Check that your pressure-ratio constraints follow the rules in [Pipe Parameter Guide](pipe_parameter_guide.md). The most common cause is setting `pr` on the last building in the chain.
