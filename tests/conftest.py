import warnings
from pathlib import Path

import matplotlib
import pytest

# Headless, before anything imports pyplot. Without this the figure tests pick
# whatever interactive backend is installed and fail on a machine with no
# display, and which module happens to be imported first decides it.
matplotlib.use("Agg")

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "configs"

# TESPy emits FutureWarnings for kA/kA_char on 0.11 and characteristic-domain
# warnings at extreme part load. Neither affects correctness of the results.
warnings.filterwarnings("ignore", category=FutureWarning)


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return CONFIG_DIR


@pytest.fixture(scope="session")
def all_configs():
    return sorted(CONFIG_DIR.glob("*.yaml"))


@pytest.fixture
def tmp_config(tmp_path):
    """Load a scenario and redirect its outputs into a temporary directory."""
    from discoolpy import load_yaml_config

    def _load(name: str, **overrides):
        cfg = load_yaml_config(CONFIG_DIR / name)
        cfg.setdefault("outputs", {})["output_dir"] = str(tmp_path / name)
        for key, value in overrides.items():
            section, _, field = key.partition(".")
            if field:
                cfg.setdefault(section, {})[field] = value
            else:
                cfg[section] = value
        return cfg

    return _load
