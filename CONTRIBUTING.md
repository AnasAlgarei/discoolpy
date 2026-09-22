# Contributing to DisCoolPy

Thank you for your interest in contributing. This document describes the development workflow, coding standards, and pull-request process.

---

## Development Setup

```bash
# 1. Fork the repository on GitHub, then clone your fork
git clone https://github.com/YOUR_USERNAME/discoolpy.git
cd discoolpy

# 2. Create the conda environment
conda env create -f environment.yml
conda activate discoolpy

# 3. Install in editable mode with development extras
pip install -e ".[dev]"

# 4. Verify everything works
pytest                       # 459 tests, about a minute
discoolpy check --all        # every shipped scenario solves at its design point
```

---

## Workflow

1. Create a feature branch from `main`: `git checkout -b feature/my-feature`
2. Make your changes, following the coding standards below.
3. Run the tutorial notebook to confirm nothing is broken.
4. Commit with a clear message: `git commit -m "Add: description of change"`
5. Push and open a pull request against `main`.

---

## Coding Standards

The project follows these conventions:

**Python style.** PEP 8 with a line length of 100 characters. Use `black` for formatting (`black src/ examples/`) and `flake8` for linting.

**Type hints.** All public functions and methods should have type annotations.

**Docstrings.** Use Google-style docstrings for all public classes, methods, and functions. Include `Args`, `Returns`, and `Raises` sections where applicable.

**Component wrappers.** New components should follow the pattern established by `building.py` and `branch.py`: a `@dataclass` with `__post_init__` for TESPy component creation, `connect_between` for connection assembly, `set_design` for design-mode parameters, and `set_demand` or equivalent for offdesign updates.

**YAML schema.** A new configuration key has to be added to `SCENARIO_KEYS` in `discoolpy/config_schema.py`, or `validate_scenario` will warn about it wherever it is used. Document it in `docs/configuration.md` and demonstrate it in a new or updated YAML file in `configs/`. `tests/test_validation.py` asserts that every shipped scenario validates with no warnings, so a key added to the code and forgotten in the map turns the suite red.

---

## Adding a New Component

1. Create `discoolpy/my_component.py` following the dataclass pattern.
2. Export it from `discoolpy/__init__.py`.
3. Add a section to `docs/api_reference.md` describing the class, parameters, and usage.
4. Add its YAML keys to `SCENARIO_KEYS` and to `docs/configuration.md`.
5. Give it a report section or a figure panel in `discoolpy/reporting.py` if it produces results worth seeing.
6. Demonstrate the component in a new example script or extend an existing one.

---

## Pull Request Checklist

Before opening a pull request, confirm the following:

- `pytest` passes, and each test file passes on its own.
- `discoolpy check --all --strict` exits zero.
- The tutorial notebook (`examples/tutorial_dummy_data.ipynb`) executes without errors.
- All new public functions have docstrings and type hints.
- New YAML keys are in `SCENARIO_KEYS` and documented in `docs/configuration.md`.
- The `environment.yml` and `pyproject.toml` are updated if new dependencies are added.
- The `README.md` feature table is updated if a new capability is added.
- `CHANGELOG.md` has an entry.

---

## Reporting Issues

Please use the GitHub issue tracker. When reporting a convergence problem, include:

- The YAML configuration file (or the Python dict equivalent).
- The full error message and traceback.
- The TESPy version (`python -c "import tespy; print(tespy.__version__)"`).
