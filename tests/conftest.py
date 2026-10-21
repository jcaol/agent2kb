"""Shared test setup."""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_from_kb_environment(monkeypatch):
    # KB_* variables override config files, so ones exported in the developer's shell (for
    # example KB_ACCESS_KEY from a manual a2kb setup) would leak into every test. Integration
    # tests read their own KB_IT_* settings, which are kept.
    for name in list(os.environ):
        if name.startswith("KB_") and not name.startswith("KB_IT_"):
            monkeypatch.delenv(name, raising=False)
