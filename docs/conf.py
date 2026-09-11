# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import pathlib
import sys

parent_path = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(parent_path))

from flexgate import __version__  # noqa: E402

# -- Project information -----------------------------------------------------

project = "flexgate"
author = "agony"
version = __version__
release = __version__

# -- General configuration ---------------------------------------------------

extensions = [
    "myst_parser",
]

myst_enable_extensions = [
    "colon_fence",
    "linkify",
]

# Allow cross-document links to implicit heading anchors (e.g. configuration.md#key-fallback)
myst_heading_anchors = 3

source_suffix = {".md": "markdown"}
templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
language = "zh_CN"

# -- Options for HTML output -------------------------------------------------

html_theme = "furo"
html_static_path = ["_static"]
html_title = f"flexgate {version} 文档"
html_theme_options = {
    "source_repository": "https://github.com/Agony5757/flexible-gateway",
    "source_branch": "main",
    "source_directory": "docs/",
}
