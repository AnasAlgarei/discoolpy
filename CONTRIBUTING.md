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
python -c "import discoolpy; print('OK')"
cd examples && jupyter nbconvert --to notebook --execute tutorial_dummy_data.ipynb
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

**YAML schema.** New configuration keys should be documented in `docs/api_reference.md` and demonstrated in a new or updated YAML file in `configs/`.

---

## Adding a New Component

1. Create `discoolpy/my_component.py` following the dataclass pattern.
2. Export it from `discoolpy/__init__.py`.
3. Add a section to `docs/api_reference.md` describing the class, parameters, and usage.
4. Add a YAML key to the schema documentation in `docs/getting_started.md`.
5. Demonstrate the component in a new example script or extend an existing one.

---

## Pull Request Checklist

Before opening a pull request, confirm the following:

- The tutorial notebook (`examples/tutorial_dummy_data.ipynb`) executes without errors.
- All new public functions have docstrings and type hints.
- New YAML keys are documented in `docs/api_reference.md`.
- The `environment.yml` and `setup.py` are updated if new dependencies are added.
- The `README.md` feature table is updated if a new capability is added.

---

## Reporting Issues

Please use the GitHub issue tracker. When reporting a convergence problem, include:

- The YAML configuration file (or the Python dict equivalent).
- The full error message and traceback.
- The TESPy version (`python -c "import tespy; print(tespy.__version__)"`).
