"""Command modules registered onto the main Typer app.

Each module in this package defines ``register(app: typer.Typer) -> None`` and
adds its commands there, so new commands never need edits to ``cli.py``.
"""
from __future__ import annotations

import importlib
import pkgutil

import typer


def register_all(app: typer.Typer) -> None:
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        register = getattr(module, "register", None)
        if register is None:
            raise RuntimeError(f"{module.__name__} has no register(app) function")
        register(app)
