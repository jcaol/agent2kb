"""Optional dependencies: import them lazily and say which extra to install.

The core install carries only what every command needs. Provider SDKs and
media tooling live in extras (``pip install "agent2kb[groq]"``); each import
site goes through ``import_optional`` so a missing package produces one line
naming the extra instead of a bare ``ModuleNotFoundError``.
"""

from __future__ import annotations

import importlib
import shutil
from typing import Any

# Distribution name used in the pip hint. Kept in one place so a rename
# touches a single line.
DIST_NAME = "agent2kb"


class MissingExtraError(ImportError, RuntimeError):
    """An optional package is not installed; the message names the extra.

    Also a RuntimeError: callers written before extras existed catch that.
    """

    def __init__(self, module: str, extra: str, purpose: str):
        self.module = module
        self.extra = extra
        self.purpose = purpose
        super().__init__(
            f"{purpose} needs the optional package '{module}'. "
            f'Install it with: pip install "{DIST_NAME}[{extra}]"'
        )


def import_optional(module: str, extra: str, purpose: str) -> Any:
    """Import ``module`` (dotted names allowed), or raise ``MissingExtraError``.

    ``purpose`` completes the sentence "<purpose> needs ..." — e.g.
    "The Groq provider". A broken install (a dependency of the package is
    missing) gets the same hint: reinstalling the extra is the fix either way.
    """
    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise MissingExtraError(module, extra, purpose) from e


def require_executable(name: str, extra: str, purpose: str) -> str:
    """Return the path of an executable that an extra installs, or raise."""
    path = shutil.which(name)
    if path:
        return path
    raise MissingExtraError(name, extra, purpose)
