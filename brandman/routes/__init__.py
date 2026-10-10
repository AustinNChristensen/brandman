"""REST routes, grouped by domain.

Route modules import shared models, services and helpers from
``brandman.main``, which includes every router below. Import
``brandman.main`` (not a route module) as the entry point.
"""
from brandman import main as _main  # noqa: F401  (ensures composition order)
