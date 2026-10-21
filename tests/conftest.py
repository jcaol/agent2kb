"""Shared test setup."""

import os

import pytest

# Typer forces coloured --help output when it sees GITHUB_ACTIONS (or FORCE_COLOR), and the
# ANSI codes split flags such as "--http" apart in the captured text. It reads this switch
# at import time, so it is set here, before any test imports agent2kb.main.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
# Rich forces colour for the app's own console output on the same signals.
for _name in ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE"):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _isolate_from_kb_environment(monkeypatch):
    # KB_* variables override config files, so ones exported in the developer's shell (for
    # example KB_ACCESS_KEY from a manual a2kb setup) would leak into every test. Integration
    # tests read their own KB_IT_* settings, which are kept.
    for name in list(os.environ):
        if name.startswith("KB_") and not name.startswith("KB_IT_"):
            monkeypatch.delenv(name, raising=False)
