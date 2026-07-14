# DisCoolPy

> A modular district-cooling modelling tool built on [TESPy](https://tespy.readthedocs.io/).

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![TESPy](https://img.shields.io/badge/TESPy-0.7%2B-green)](https://tespy.readthedocs.io/)

---
⚠️ **Note: DisCoolPy is currently in Beta (v0.1.0b1).** 
> The tool is undergoing active development and testing. APIs and modeling functions 
> may change without warning. We would love your feedback, please report any bugs 
> or suggestions in the [Issues](https://github.com/AnasAlgarei/discoolpy/issues) tab!

---

## Overview

**DisCoolPy** is an open-source Python tool for modelling and simulating district cooling networks. It is designed for **planned-system analysis** where you supply street/pipe lengths, building load proflies including peak loads, and chiller specifications, and the tool assembles a thermodynamically consistent TESPy network that can be solved at design point and stepped through time-varying load profiles.

### Key capabilities

| Feature | Description |
|---|---|
| **Modular components** | Independent wrappers for chillers, cooling towers, distribution branches, buildings, and cold/ice storage |
| **YAML configuration** | All inputs defined in a single YAML file; no code changes needed to switch scenarios |
| **Planned pipe lengths** | Supply actual street lengths and pipe diameters; the tool derives hydraulic pressure ratios automatically |
| **Thermal storage** | Supervisory charge/discharge model for chilled-water, PCM, or ice storage with load-leveling dispatch |
| **Time-varying simulation** | Nonlinear offdesign snapshots over hourly or half-hourly profiles — COP, compressor power, and temperatures all vary |


---

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/AnasAlgarei/discoolpy.git
cd discoolpy

# 2. Create the conda environment
conda env create -f environment.yml
conda activate discoolpy

# 3. Run the tutorial notebook: directley on IDE or type these lines in the terminal:
cd examples
jupyter notebook tutorial_dummy_data.ipynb
```

---

## Installation

See [docs/installation.md](docs/installation.md) for full instructions, including `pip`-only and manual dependency installation options.

**Requirements:** Python ≥ 3.9, TESPy ≥ 0.7, CoolProp, PyYAML, pandas, matplotlib, numpy.

---

## Documentation

All documentation lives in the `docs/` directory and can be read directly as Markdown:

| Document | Description |
|---|---|
| [Introduction](docs/index.md) | Tool overview, architecture, and design philosophy |
| [Installation](docs/installation.md) | Environment setup, dependencies, and verification |
| [Getting Started](docs/getting_started.md) | First simulation in 10 minutes |
| [Pipe Parameter Guide](docs/pipe_parameter_guide.md) | How to choose lengths, diameters, roughness, and pressure anchors |
| [API Reference](docs/api_reference.md) | Full component and utility function reference |

---

## Examples

### Tutorial notebook

`examples/tutorial_dummy_data.ipynb` walks through a complete two-building simulation:
1. Define the network in a Python dict or YAML file
2. Run the design-point solve
3. Build a synthetic 24-hour load profile
4. Step through offdesign snapshots
5. Plot chiller load, compressor power, and COP

### Storage comparison

`examples/storage_comparison_example.py` runs two identical three-building networks, one with a cold-storage tank and one without, and produces a side-by-side comparison of peak load, compressor power, and COP.

### Planned pipe lengths

`configs/config_length_derived_pr.yaml` demonstrates how to specify actual street lengths and pipe diameters. The tool converts these to equivalent pressure ratios, keeping the solver robust while preserving the physical meaning of the network geometry.

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

---

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details.

---

## Citation

If you use this tool in academic work, please cite it as:

```
DisCoolPy (2026).
https://github.com/AnasAlgarei/discoolpy
```
