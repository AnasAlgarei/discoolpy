"""Sphinx configuration for the DisCoolPy documentation.

The pages are Markdown, parsed by MyST, so the same files stay readable on
GitHub and build on Read the Docs. Nothing here needs a local build to work
on: edit the Markdown and the site follows.

Build locally with::

    pip install -e ".[docs]"
    sphinx-build -b html docs docs/_build/html
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(".."))

project = "DisCoolPy"
author = "Anas Algarei"
copyright = f"{date.today().year}, {author}"

try:
    from discoolpy import __version__ as release
except Exception:  # the docs must still build without the package installed
    release = "1.0.0"
version = ".".join(release.split(".")[:2])

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx.ext.mathjax",
    "sphinx_copybutton",
]

source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
master_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# MyST. `colon_fence` lets a directive be written as ::: instead of ```, and
# `linkify` turns bare URLs into links. `dollarmath` covers the few formulae
# in the heat-gain pages.
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "dollarmath",
    "fieldlist",
    "linkify",
    "substitution",
    "tasklist",
]
myst_heading_anchors = 3

# Cross-page links in this repo are written as plain Markdown links between
# .md files so they work on GitHub too. MyST resolves them for the site.
myst_url_schemes = ("http", "https", "mailto", "ftp")

html_theme = "sphinx_rtd_theme"
html_title = f"DisCoolPy {release}"
html_theme_options = {
    "collapse_navigation": False,
    "navigation_depth": 3,
    "titles_only": False,
}
html_static_path = []

# Autodoc pulls TESPy and CoolProp in, which Read the Docs can install but
# which are slow. Keep the signatures readable when it does run.
autodoc_typehints = "description"
autodoc_member_order = "bysource"
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}
napoleon_google_docstring = True
napoleon_numpy_docstring = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
    "matplotlib": ("https://matplotlib.org/stable/", None),
    "tespy": ("https://tespy.readthedocs.io/en/main/", None),
}
intersphinx_disabled_reftypes = ["*"]

nitpicky = False
suppress_warnings = ["myst.header", "myst.xref_missing"]
