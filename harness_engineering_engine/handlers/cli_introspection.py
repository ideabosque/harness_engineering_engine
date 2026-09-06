# -*- coding: utf-8 -*-
"""Read-only introspection of an installed CLI package's command tree.

Used by P9 section generation (``handlers.skill_deployment``) to ground
what it asks an LLM to propose for a skill's missing ``allowed_commands`` —
the model is given the CLI's *real* subcommands and their own help text,
not left to guess at a plausible-looking argv.

Only ``click``-based CLIs are supported. A CLI built some other way (or not
installed at all) simply yields nothing here — there is no fallback that
scrapes ``--help`` output, since that would be far less reliable than
walking click's own command tree.
"""
from __future__ import print_function

__author__ = "bibow"

import importlib.metadata
from typing import Any, Dict, Iterator, List, Optional, Tuple


def resolve_console_script_entry_point(package_name: str) -> Optional[Tuple[str, Any]]:
    """Find the installed console-script entry point for a distribution.

    Returns ``(script_name, click_command)`` or ``None`` when the
    distribution isn't installed, has no ``console_scripts`` entry point,
    the entry point fails to import, or it isn't a ``click`` command.
    """
    try:
        dist = importlib.metadata.distribution(package_name)
    except importlib.metadata.PackageNotFoundError:
        return None

    console_scripts = [ep for ep in dist.entry_points if ep.group == "console_scripts"]
    if not console_scripts:
        return None

    try:
        import click
    except ImportError:
        return None

    for ep in console_scripts:
        try:
            loaded = ep.load()
        except Exception:
            continue
        if isinstance(loaded, click.BaseCommand):
            return ep.name, loaded

    return None


def walk_click_commands(command: Any, prefix: List[str]) -> Iterator[Tuple[List[str], str]]:
    """Yield ``(argv, help_text)`` for every leaf command in a click tree."""
    import click

    if isinstance(command, click.Group):
        for sub_name, sub_command in sorted(command.commands.items()):
            yield from walk_click_commands(sub_command, prefix + [sub_name])
    else:
        help_text = (command.help or command.short_help or "").strip()
        help_text = " ".join(help_text.split())
        yield prefix, help_text


def find_installed_packages_for_commands(
    command_names: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Reverse-lookup installed console-scripts by name.

    For each of ``command_names`` (candidate CLI tokens spotted in a
    skill's body text — see ``handlers.section_generator``) that actually
    resolves to a console-script on *this host*, returns
    ``{"package_name": ..., "version": ...}`` keyed by that command name.
    A candidate that doesn't match anything installed is simply absent
    from the result — this only ever reports what's really there, never
    installs or invents anything.
    """
    wanted = set(command_names)
    found: Dict[str, Dict[str, Any]] = {}
    if not wanted:
        return found

    for dist in importlib.metadata.distributions():
        try:
            entry_points = dist.entry_points
        except Exception:
            continue
        for ep in entry_points:
            if ep.group == "console_scripts" and ep.name in wanted and ep.name not in found:
                name = dist.metadata.get("Name") or getattr(dist, "name", None) or ep.name
                found[ep.name] = {"package_name": name, "version": dist.version}

    return found


def list_commands(package_name: str) -> List[Dict[str, Any]]:
    """Return every discoverable ``{"argv": [...], "note": ...}`` for a package.

    Empty list if the package isn't installed, has no console-script entry
    point, or isn't click-based — never raises.
    """
    resolved = resolve_console_script_entry_point(package_name)
    if resolved is None:
        return []

    script_name, root_command = resolved
    return [
        {"argv": argv, "note": note}
        for argv, note in walk_click_commands(root_command, [script_name])
    ]


__all__ = [
    "resolve_console_script_entry_point",
    "walk_click_commands",
    "list_commands",
    "find_installed_packages_for_commands",
]
