"""Shim for tooling that still expects a setup.py.

Every piece of packaging metadata lives in pyproject.toml. This file exists so
that `pip install -e .` works on pip versions predating PEP 660 support.
"""

from setuptools import setup

setup()
