# -*- coding: utf-8 -*-
"""P9 — OpenAI-assisted generation of a skill's missing sections.

``generate_missing_sections`` is called by ``handlers.skill_deployment``
during ``deploySkillPackage``, once per skill, and only for whichever of
``allowed_commands``/``cli_packages``/``reference_files`` that skill's own
``SKILL.md`` left entirely absent (never for a field the author already
populated, even with an empty list — see docs/DEVELOPMENT_PLAN.md §5). It
never raises: a missing API key, a network failure, or a malformed
response all degrade to "generate nothing" for the OpenAI-backed fields,
which preserves ``allowed_commands``' own deny-by-default guarantee (an
empty/absent list means `runCommand` stays denied, never "allow
everything").

Two different grounding strategies, matched to two different risk levels:

- ``cli_packages`` — found by a deterministic, local, LLM-free step: scan
  the body text for backtick-quoted command tokens, then check each
  against every *actually installed* console-script on this host
  (``handlers.cli_introspection.find_installed_packages_for_commands``).
  Nothing is invented — no OpenAI call, no guessed ``git_repository_url``,
  no attempt to install anything; a candidate that isn't already installed
  is simply dropped.
- ``allowed_commands``/``reference_files`` — proposed by OpenAI, grounded
  in the skill's own body text plus the *real* command tree of every
  declared-or-discovered ``cli_packages`` entry (never an invented argv)
  and the actual list of files present in the skill's directory (never an
  invented path).
"""
from __future__ import print_function

__author__ = "bibow"

import json
import logging
import re
import shlex
from typing import Any, Dict, List, Set

from . import cli_introspection
from .config import Config

SYSTEM_PROMPT = """You are helping configure a tool-use skill for an AI agent platform.

You will be given a skill's name, description, and full instruction body, \
the real command tree of its CLI package dependency (with each subcommand's \
own help text), and the list of files actually present in its directory. \
Propose values ONLY for the fields explicitly requested.

Rules:
- allowed_commands is a strict allowlist: only include a subcommand if the \
skill's own body text treats it as something that should run \
automatically/unprompted. If the body describes a command as requiring \
human review, sign-off, or explicit conversational approval before use \
(e.g. an "approve", "publish", "release", or similar gated action), you \
MUST exclude it, even though it is a real, installed command.
- reference_files must only name paths from the provided file list, \
never invent a path. Prefer files that look like documentation/config \
the agent would need to read (not scripts, not binary/vendor assets, not \
test fixtures).
- If you are not confident a field should contain anything, return an \
empty list for it rather than guessing.

Respond with a single JSON object with only the requested keys. \
allowed_commands entries look like {"argv": ["cmd", "subcommand"]}. \
reference_files is a list of path strings."""

# Backtick-quoted spans are how every real skill body in this project
# writes a CLI invocation (see e.g. the multilingual-slide-video-agent
# skills' `msv pipeline ...` style). Only the first token of each span is
# a command-name candidate; it's verified against real installed
# console-scripts before it's ever trusted (see
# cli_introspection.find_installed_packages_for_commands) — a false
# positive here just means "no match found," never an invented package.
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_FENCED_CODE_RE = re.compile(
    r"```(?:[A-Za-z0-9_-]+)?[ \t]*\n(.*?)\n?```",
    re.DOTALL,
)

# Ambient developer/environment tooling — near-universally installed, and
# routinely mentioned in a skill's body as a setup/prerequisite aside
# (e.g. "installed via `pip install -e .`"), never as this skill's own
# domain CLI dependency. Verifying against real installed console-scripts
# (see find_installed_packages_for_commands) can't tell those two cases
# apart on its own, since a tool like pip really is installed on nearly
# every host — so it's excluded from candidacy outright, before that
# lookup ever runs.
_AMBIENT_TOOLING_DENYLIST = {
    "pip", "pip3", "python", "python3", "git", "npm", "npx", "yarn", "pnpm",
    "node", "docker", "docker-compose", "make", "cargo", "go", "cmake",
    "conda", "poetry", "pipenv", "virtualenv", "brew", "apt", "apt-get",
    "curl", "wget", "sh", "bash", "source", "cd", "export", "env",
}
_CONSOLE_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


def _valid_candidate_token(token: str) -> bool:
    if not token or "/" in token or "\\" in token:
        return False
    if not _CONSOLE_SCRIPT_NAME_RE.match(token):
        return False
    if token.startswith(("-", "$", ".")):
        return False
    if token in _AMBIENT_TOOLING_DENYLIST:
        return False
    return True


def _first_command_token(line: str) -> str:
    """Return the command token from a shell-like example line.

    This intentionally does not try to interpret shell syntax. It only
    handles the common documentation shape used in SKILL.md command blocks:
    optional prompts, optional environment assignments, then a command.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return ""

    while stripped.startswith(("$", ">")):
        stripped = stripped[1:].strip()

    try:
        parts = shlex.split(stripped, posix=True)
    except ValueError:
        parts = stripped.split()

    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        key, _, _value = parts[0].partition("=")
        if not key.replace("_", "").isalnum():
            break
        parts = parts[1:]

    return parts[0] if parts else ""


def _shell_example_argv(line: str) -> List[str]:
    """Parse one documented shell command line into argv-ish tokens.

    Optional bracket notation like ``[--repo PATH]`` is documentation, not
    part of argv, so it is dropped. Dynamic values for common IOA options are
    globbed so command_executor can still match real user inputs safely.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return []

    while stripped.startswith(("$", ">")):
        stripped = stripped[1:].strip()

    try:
        parts = shlex.split(stripped, posix=True)
    except ValueError:
        parts = stripped.split()

    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        key, _, _value = parts[0].partition("=")
        if not key.replace("_", "").isalnum():
            break
        parts = parts[1:]

    filtered: List[str] = []
    in_optional = False
    for part in parts:
        if part.startswith("["):
            in_optional = True
        if not in_optional:
            filtered.append(part)
        if in_optional and part.endswith("]"):
            in_optional = False

    wildcard_value_options = {"--ingredient", "--repo", "--date"}
    for idx, part in enumerate(filtered[:-1]):
        if part in wildcard_value_options:
            filtered[idx + 1] = "*"

    return filtered


def _extract_candidate_commands(body: str) -> List[str]:
    candidates: List[str] = []
    seen: Set[str] = set()

    def add_candidate(token: str) -> None:
        if not _valid_candidate_token(token):
            return
        if token in seen:
            return
        seen.add(token)
        candidates.append(token)

    for match in _FENCED_CODE_RE.finditer(body):
        for line in match.group(1).splitlines():
            add_candidate(_first_command_token(line))

    inline_body = _FENCED_CODE_RE.sub("", body)
    for match in _BACKTICK_RE.finditer(inline_body):
        text = match.group(1).strip()
        if not text:
            continue
        first_token = text.split()[0]
        add_candidate(first_token)
    return candidates


def _derive_allowed_commands_from_examples(
    body: str,
    commands_by_package: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Build allowlist entries from fenced command examples.

    A derived entry is accepted only when its argv starts with a real
    introspected leaf command. This keeps the convenience deterministic while
    preserving the deny-by-default model.
    """
    command_prefixes = [
        entry.get("argv", [])
        for commands in commands_by_package.values()
        for entry in commands
        if isinstance(entry.get("argv"), list)
    ]
    if not command_prefixes:
        return []

    allowed: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for match in _FENCED_CODE_RE.finditer(body):
        for line in match.group(1).splitlines():
            argv = _shell_example_argv(line)
            if not argv:
                continue
            if not any(argv[: len(prefix)] == prefix for prefix in command_prefixes):
                continue
            key = json.dumps(argv, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            allowed.append({"argv": argv})
    return allowed


_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_json_fence(raw: str) -> str:
    """Strip a ```json ... ``` (or bare ``` ... ```) wrapper some models
    add even when a strict JSON response was requested — e.g. Ollama
    Cloud's glm-5.2 does this despite response_format={"type":
    "json_object"}. A response with no fence at all passes through
    unchanged."""
    match = _JSON_FENCE_RE.match(raw)
    return match.group(1) if match else raw


def _call_openai(system_prompt: str, user_prompt: str) -> str:
    """Isolated so tests can mock this one call — never imports ``openai``
    unless generation is actually going to happen."""
    from openai import OpenAI

    # base_url is the same shared, non-HSK-prefixed setting other engines
    # in this gateway already use (proxy / Azure OpenAI / custom
    # endpoint) — empty means the openai SDK's own default
    # (api.openai.com).
    client = OpenAI(
        api_key=Config.OPENAI_API_KEY,
        base_url=Config.OPENAI_BASE_URL or None,
    )
    response = client.chat.completions.create(
        model=Config.OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        timeout=30,
    )
    return response.choices[0].message.content or "{}"


def _build_user_prompt(
    skill_name: str,
    description: str,
    body: str,
    commands_by_package: Dict[str, List[Dict[str, Any]]],
    available_files: List[str],
    missing_fields: Set[str],
) -> str:
    parts = [
        f"Skill name: {skill_name}",
        f"Description: {description}",
        "",
        "Body:",
        body,
        "",
    ]
    for package_name, commands in commands_by_package.items():
        parts.append(f"CLI package '{package_name}' command tree:")
        for entry in commands:
            argv = " ".join(entry["argv"])
            note = entry.get("note", "")
            parts.append(f"  - {argv}: {note}")
        parts.append("")

    if "reference_files" in missing_fields:
        parts.append("Files present in this skill's directory:")
        for path in available_files:
            parts.append(f"  - {path}")
        parts.append("")

    parts.append(f"Generate only these fields: {sorted(missing_fields)}")
    return "\n".join(parts)


def _discover_cli_packages(body: str) -> List[Dict[str, Any]]:
    """Deterministic, LLM-free discovery of an already-installed CLI a
    skill's body text mentions but its SKILL.md never declared.

    Only ever reports a package that is genuinely installed on this host
    (real ``package_name``/``version`` from ``importlib.metadata``) — never
    a ``git_repository_url`` (there is no reliable way to derive that from
    prose, and inventing one would be actively dangerous, since a wrong
    URL could point ``ensure_package`` at installing the wrong thing). A
    discovered entry without a ``git_repository_url`` simply can't be
    auto-installed elsewhere by P8 — it documents what's already true on
    this host, nothing more.
    """
    candidates = _extract_candidate_commands(body)
    matches = cli_introspection.find_installed_packages_for_commands(candidates)
    return [
        {"package_name": info["package_name"], "version": info["version"]}
        for info in matches.values()
    ]


def generate_missing_sections(
    logger: logging.Logger,
    skill_name: str,
    description: str,
    body: str,
    cli_packages: List[Dict[str, Any]],
    available_files: List[str],
    missing_fields: Set[str],
) -> Dict[str, Any]:
    """Return only the keys in ``missing_fields`` that were successfully
    generated (a subset — possibly empty). Never raises."""
    missing_fields = missing_fields & {"allowed_commands", "cli_packages", "reference_files"}
    if not missing_fields:
        return {}

    result: Dict[str, Any] = {}

    # cli_packages discovery is local/deterministic — no OpenAI call, no
    # API key required — so it runs independent of the rest below.
    discovered_cli_packages: List[Dict[str, Any]] = []
    if "cli_packages" in missing_fields:
        discovered_cli_packages = _discover_cli_packages(body)
        if discovered_cli_packages:
            result["cli_packages"] = discovered_cli_packages

    openai_fields = missing_fields & {"allowed_commands", "reference_files"}
    if not openai_fields:
        return result

    effective_cli_packages = list(cli_packages) + discovered_cli_packages
    if not effective_cli_packages:
        # Nothing to introspect and ground allowed_commands in — skip
        # rather than let the model invent commands for a package it
        # can't see. reference_files (if requested) doesn't need this.
        openai_fields = openai_fields - {"allowed_commands"}
        if not openai_fields:
            return result

    if not Config.OPENAI_API_KEY:
        logger.info(
            f"Skipping section generation for '{skill_name}': no OpenAI API key configured."
        )
        return result

    commands_by_package: Dict[str, List[Dict[str, Any]]] = {}
    for pkg in effective_cli_packages:
        package_name = pkg.get("package_name") or pkg.get("distribution_name")
        if not package_name:
            continue
        commands = cli_introspection.list_commands(package_name)
        if commands:
            commands_by_package[package_name] = commands

    if "allowed_commands" in openai_fields and not commands_by_package:
        openai_fields = openai_fields - {"allowed_commands"}
        if not openai_fields:
            return result

    if "allowed_commands" in openai_fields:
        derived_allowed_commands = _derive_allowed_commands_from_examples(
            body, commands_by_package
        )
        if derived_allowed_commands:
            result["allowed_commands"] = derived_allowed_commands
            openai_fields = openai_fields - {"allowed_commands"}
            if not openai_fields:
                return result

    user_prompt = _build_user_prompt(
        skill_name, description, body, commands_by_package, available_files, openai_fields
    )

    try:
        raw = _call_openai(SYSTEM_PROMPT, user_prompt)
        parsed = json.loads(_strip_json_fence(raw))
    except Exception as e:
        logger.warning(
            f"Section generation failed for '{skill_name}' — deploying without it: {e}"
        )
        return result

    if not isinstance(parsed, dict):
        return result

    if "allowed_commands" in openai_fields:
        candidate = parsed.get("allowed_commands")
        if isinstance(candidate, list):
            valid = [
                entry
                for entry in candidate
                if isinstance(entry, dict) and isinstance(entry.get("argv"), list)
            ]
            result["allowed_commands"] = valid

    if "reference_files" in openai_fields:
        candidate = parsed.get("reference_files")
        if isinstance(candidate, list):
            available_set = set(available_files)
            # Defensive: only keep paths that actually exist — never trust
            # the model to only name real files.
            result["reference_files"] = [
                p for p in candidate if isinstance(p, str) and p in available_set
            ]

    return result


__all__ = ["generate_missing_sections"]
