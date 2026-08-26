# -*- coding: utf-8 -*-
"""Harness Engineering Engine CLI package manager.

Handles registration, GitHub-based installation, version verification, and
package-level deployment locks for approved Python CLI packages.
"""
from __future__ import print_function

__author__ = "bibow"

import logging
from typing import Any, Dict


class CliPackageManager:
    """Manage Python CLI packages that skills depend on."""

    def __init__(self, logger: logging.Logger, setting: Dict[str, Any]) -> None:
        self.logger = logger
        self.setting = setting

    # ------------------------------------------------------------------
    # Placeholder: implementation depends on the CLI package registry DB
    # table, which is deferred until v1.1 (see DEVELOPMENT_PLAN.md §11).
    #
    # The current implementation raises ``NotImplementedError`` so that the
    # interface contract is documented while the runtime is being built.
    # ------------------------------------------------------------------

    def ensure_package(self, package_name: str) -> None:
        """Ensure the registered version of ``package_name`` is installed."""
        raise NotImplementedError(
            "CLI package management is deferred to v1.1. "
            "Use local skill scripts instead."
        )


__all__ = ["CliPackageManager"]

