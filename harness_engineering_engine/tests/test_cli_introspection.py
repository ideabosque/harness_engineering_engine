#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for read-only click CLI introspection (P9 grounding source)."""
from __future__ import print_function

from unittest.mock import patch

import pytest

from harness_engineering_engine.handlers.cli_introspection import (
    find_installed_packages_for_commands,
    list_commands,
    resolve_console_script_entry_point,
)


class TestCliIntrospection:
    def test_uninstalled_package_yields_nothing(self):
        assert list_commands("definitely-not-installed-xyz") == []
        assert resolve_console_script_entry_point("definitely-not-installed-xyz") is None

    def test_walks_a_real_group_of_groups_tree(self):
        click = pytest.importorskip("click")

        @click.group()
        def cli():
            pass

        @cli.group()
        def pipeline():
            """Pipeline orchestration commands."""

        @pipeline.command()
        def init():
            """Create a new pipeline run."""

        @pipeline.command(name="approve-publish")
        def approve_publish():
            """Human sign-off releasing held publishing stages."""

        @cli.command()
        def slides():
            """Slide text extraction tools."""

        with patch(
            "harness_engineering_engine.handlers.cli_introspection."
            "resolve_console_script_entry_point",
            return_value=("mytool", cli),
        ):
            commands = list_commands("example-cli-tool")

        argvs = {tuple(entry["argv"]) for entry in commands}
        assert argvs == {
            ("mytool", "pipeline", "approve-publish"),
            ("mytool", "pipeline", "init"),
            ("mytool", "slides"),
        }
        by_argv = {tuple(entry["argv"]): entry for entry in commands}
        assert (
            by_argv[("mytool", "pipeline", "approve-publish")]["note"]
            == "Human sign-off releasing held publishing stages."
        )


class TestFindInstalledPackagesForCommands:
    def test_finds_a_really_installed_console_script(self):
        """pip is present as a console-script in essentially any Python
        environment — a safe, deterministic real target."""
        result = find_installed_packages_for_commands(["pip", "not-a-real-command-xyz"])

        assert "pip" in result
        assert result["pip"]["package_name"].lower() == "pip"
        assert "version" in result["pip"]
        assert "not-a-real-command-xyz" not in result

    def test_empty_input_returns_empty_dict(self):
        assert find_installed_packages_for_commands([]) == {}

    def test_no_matches_returns_empty_dict(self):
        assert find_installed_packages_for_commands(["definitely-not-a-cli-xyz"]) == {}
