#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for P9's OpenAI-assisted section generation.

Every test mocks ``_call_openai`` — no real API calls anywhere in this
suite. The function must never raise: a missing API key, no cli_packages,
no missing fields, an API failure, or a malformed response all degrade to
"generate nothing" rather than propagating an error into deploySkillPackage.
"""
from __future__ import print_function

import json
import logging
from unittest.mock import patch

from harness_engineering_engine.handlers.config import Config
from harness_engineering_engine.handlers.section_generator import (
    _discover_cli_packages,
    _extract_candidate_commands,
    _strip_json_fence,
    generate_missing_sections,
)

logger = logging.getLogger("test")


class TestGenerateMissingSections:
    def setup_method(self):
        self._original_key = Config.OPENAI_API_KEY
        Config.OPENAI_API_KEY = "test-key"

    def teardown_method(self):
        Config.OPENAI_API_KEY = self._original_key

    def test_no_missing_fields_skips_the_call(self):
        with patch(
            "harness_engineering_engine.handlers.section_generator._call_openai"
        ) as mock_call:
            result = generate_missing_sections(
                logger, "skill", "desc", "body",
                [{"package_name": "pkg"}], [], set(),
            )
        assert result == {}
        mock_call.assert_not_called()

    def test_no_cli_packages_skips_the_call(self):
        with patch(
            "harness_engineering_engine.handlers.section_generator._call_openai"
        ) as mock_call:
            result = generate_missing_sections(
                logger, "skill", "desc", "body",
                [], [], {"allowed_commands"},
            )
        assert result == {}
        mock_call.assert_not_called()

    def test_no_api_key_skips_the_call(self):
        Config.OPENAI_API_KEY = ""
        with patch(
            "harness_engineering_engine.handlers.section_generator._call_openai"
        ) as mock_call:
            result = generate_missing_sections(
                logger, "skill", "desc", "body",
                [{"package_name": "pkg"}], [], {"allowed_commands"},
            )
        assert result == {}
        mock_call.assert_not_called()

    def test_no_introspectable_commands_drops_allowed_commands_request(self):
        """If the only missing field is allowed_commands and nothing can be
        introspected, skip entirely rather than let the model invent argv."""
        with patch(
            "harness_engineering_engine.handlers.cli_introspection.list_commands",
            return_value=[],
        ), patch(
            "harness_engineering_engine.handlers.section_generator._call_openai"
        ) as mock_call:
            result = generate_missing_sections(
                logger, "skill", "desc", "body",
                [{"package_name": "uninstalled-pkg"}], [], {"allowed_commands"},
            )
        assert result == {}
        mock_call.assert_not_called()

    def test_successful_generation_filters_to_requested_and_valid_entries(self):
        commands = [
            {"argv": ["msv", "pipeline", "init"], "note": "Start a run."},
            {"argv": ["msv", "pipeline", "approve-publish"], "note": "Human sign-off."},
        ]
        openai_response = json.dumps(
            {
                "allowed_commands": [
                    {"argv": ["msv", "pipeline", "init"]},
                    "not-a-dict",  # invalid entry, must be dropped
                ],
                "reference_files": [
                    "references/dispatch_table.md",
                    "does/not/exist.md",  # not in available_files, must be dropped
                ],
            }
        )
        with patch(
            "harness_engineering_engine.handlers.cli_introspection.list_commands",
            return_value=commands,
        ), patch(
            "harness_engineering_engine.handlers.section_generator._call_openai",
            return_value=openai_response,
        ) as mock_call:
            result = generate_missing_sections(
                logger,
                "slide-video-orchestration",
                "desc",
                "Never call approve-publish without human approval.",
                [{"package_name": "multilingual-slide-video-agent"}],
                ["references/dispatch_table.md", "config/pipeline.yaml"],
                {"allowed_commands", "reference_files"},
            )

        mock_call.assert_called_once()
        assert result["allowed_commands"] == [{"argv": ["msv", "pipeline", "init"]}]
        assert result["reference_files"] == ["references/dispatch_table.md"]

    def test_only_requested_fields_are_returned(self):
        """Even if the model's JSON includes both keys, only the ones
        actually in missing_fields should come back."""
        openai_response = json.dumps(
            {
                "allowed_commands": [{"argv": ["msv", "status"]}],
                "reference_files": ["config/pipeline.yaml"],
            }
        )
        with patch(
            "harness_engineering_engine.handlers.cli_introspection.list_commands",
            return_value=[{"argv": ["msv", "status"], "note": "Status."}],
        ), patch(
            "harness_engineering_engine.handlers.section_generator._call_openai",
            return_value=openai_response,
        ):
            result = generate_missing_sections(
                logger, "skill", "desc", "body",
                [{"package_name": "pkg"}],
                ["config/pipeline.yaml"],
                {"allowed_commands"},  # reference_files NOT requested
            )

        assert "allowed_commands" in result
        assert "reference_files" not in result

    def test_api_failure_returns_empty_dict_not_an_exception(self):
        with patch(
            "harness_engineering_engine.handlers.cli_introspection.list_commands",
            return_value=[{"argv": ["msv", "status"], "note": "Status."}],
        ), patch(
            "harness_engineering_engine.handlers.section_generator._call_openai",
            side_effect=RuntimeError("network error"),
        ):
            result = generate_missing_sections(
                logger, "skill", "desc", "body",
                [{"package_name": "pkg"}], [], {"allowed_commands"},
            )
        assert result == {}

    def test_markdown_fenced_response_still_parses(self):
        """Some models (e.g. Ollama Cloud's glm-5.2) wrap valid JSON in a
        ```json fence even when a strict JSON response was requested —
        generate_missing_sections must still parse it, not treat it as a
        malformed response."""
        fenced_response = (
            "```json\n"
            + json.dumps({"allowed_commands": [{"argv": ["msv", "status"]}]})
            + "\n```"
        )
        with patch(
            "harness_engineering_engine.handlers.cli_introspection.list_commands",
            return_value=[{"argv": ["msv", "status"], "note": "Status."}],
        ), patch(
            "harness_engineering_engine.handlers.section_generator._call_openai",
            return_value=fenced_response,
        ):
            result = generate_missing_sections(
                logger, "skill", "desc", "body",
                [{"package_name": "pkg"}], [], {"allowed_commands"},
            )

        assert result["allowed_commands"] == [{"argv": ["msv", "status"]}]

    def test_malformed_json_response_returns_empty_dict(self):
        with patch(
            "harness_engineering_engine.handlers.cli_introspection.list_commands",
            return_value=[{"argv": ["msv", "status"], "note": "Status."}],
        ), patch(
            "harness_engineering_engine.handlers.section_generator._call_openai",
            return_value="not valid json {{{",
        ):
            result = generate_missing_sections(
                logger, "skill", "desc", "body",
                [{"package_name": "pkg"}], [], {"allowed_commands"},
            )
        assert result == {}


class TestStripJsonFence:
    def test_strips_json_language_fence(self):
        raw = '```json\n{"allowed_commands": []}\n```'
        assert _strip_json_fence(raw) == '{"allowed_commands": []}'

    def test_strips_bare_fence(self):
        raw = '```\n{"reference_files": []}\n```'
        assert _strip_json_fence(raw) == '{"reference_files": []}'

    def test_passes_through_unfenced_json_unchanged(self):
        raw = '{"allowed_commands": []}'
        assert _strip_json_fence(raw) == raw

    def test_real_glm_5_2_response_shape_parses_after_stripping(self):
        """Regression case: Ollama Cloud's glm-5.2 wraps its answer in a
        ```json fence even with response_format=json_object requested —
        this is the exact shape that previously failed with
        'Expecting value: line 1 column 1 (char 0)'."""
        raw = '```json\n{\n  "greeting": "Hello"\n}\n```'
        assert json.loads(_strip_json_fence(raw)) == {"greeting": "Hello"}


class TestExtractCandidateCommands:
    def test_extracts_first_token_of_backtick_spans(self):
        body = "Run `msv pipeline init` then check `msv pipeline status`."
        assert _extract_candidate_commands(body) == ["msv"]

    def test_deduplicates_preserving_order(self):
        body = "`msv init` and later `pytest tests/` and again `msv status`."
        assert _extract_candidate_commands(body) == ["msv", "pytest"]

    def test_ambient_tooling_is_never_a_candidate(self):
        """pip/python/git/etc. are near-universally installed and routinely
        mentioned as setup/prerequisite asides (e.g. "installed via `pip
        install -e .`"), never as a skill's own domain CLI dependency —
        they must never surface as a cli_packages candidate."""
        body = (
            "Installed via `pip install -e .`. Uses `git log` internally "
            "and needs `python3 -m venv .venv` first, then run `msv status`."
        )
        assert _extract_candidate_commands(body) == ["msv"]

    def test_skips_flags_paths_and_dotfiles(self):
        body = "See `--help`, `./scripts/run.sh`, `$HOME`, and `.env`."
        assert _extract_candidate_commands(body) == []

    def test_no_backticks_yields_nothing(self):
        assert _extract_candidate_commands("Plain prose, no code spans.") == []


class TestDiscoverCliPackages:
    def test_discovers_a_really_installed_command_mentioned_in_body(self):
        """pytest is a real console-script installed wherever this test
        suite itself runs — a safe, deterministic target without needing
        to mock the host."""
        body = "This skill shells out to `pytest --collect-only` to check dependencies."
        result = _discover_cli_packages(body)

        names = {entry["package_name"].lower() for entry in result}
        assert "pytest" in names
        for entry in result:
            # Never invents a source — only package_name/version, both
            # read from real installed metadata.
            assert set(entry.keys()) == {"package_name", "version"}

    def test_no_real_command_mentioned_yields_nothing(self):
        body = "This skill just reasons about text, no CLI involved."
        assert _discover_cli_packages(body) == []


class TestGenerateMissingSectionsCliPackagesDiscovery:
    def setup_method(self):
        self._original_key = Config.OPENAI_API_KEY
        Config.OPENAI_API_KEY = "test-key"

    def teardown_method(self):
        Config.OPENAI_API_KEY = self._original_key

    def test_cli_packages_discovery_does_not_require_an_api_key(self):
        """Discovery is local/deterministic — no OpenAI call needed — so
        it must still work even with no key configured at all."""
        Config.OPENAI_API_KEY = ""
        body = "Uses `pytest --collect-only` to introspect the test suite."

        with patch(
            "harness_engineering_engine.handlers.section_generator._call_openai"
        ) as mock_call:
            result = generate_missing_sections(
                logger, "skill", "desc", body,
                [], [], {"cli_packages"},
            )

        mock_call.assert_not_called()
        names = {entry["package_name"].lower() for entry in result.get("cli_packages", [])}
        assert "pytest" in names

    def test_no_candidates_found_omits_cli_packages_key(self):
        with patch(
            "harness_engineering_engine.handlers.section_generator._call_openai"
        ) as mock_call:
            result = generate_missing_sections(
                logger, "skill", "desc", "No CLI mentioned here.",
                [], [], {"cli_packages"},
            )

        mock_call.assert_not_called()
        assert "cli_packages" not in result

    def test_discovered_cli_packages_ground_allowed_commands_generation(self):
        """A package found only via discovery (not declared at all) must
        still be introspected for real commands, same as a declared one."""
        body = "Uses `pytest --collect-only` to check the test suite."
        openai_response = json.dumps(
            {"allowed_commands": [{"argv": ["pytest", "--collect-only"]}]}
        )

        with patch(
            "harness_engineering_engine.handlers.cli_introspection.list_commands"
        ) as mock_list_commands, patch(
            "harness_engineering_engine.handlers.section_generator._call_openai",
            return_value=openai_response,
        ) as mock_call:
            mock_list_commands.return_value = [
                {"argv": ["pytest", "--collect-only"], "note": "List collected tests."}
            ]

            result = generate_missing_sections(
                logger, "skill", "desc", body,
                [],  # nothing declared — pytest is only found via discovery
                [],
                {"allowed_commands", "cli_packages"},
            )

        mock_call.assert_called_once()
        # list_commands must have been asked about the *discovered* package.
        called_package_names = {c.args[0] for c in mock_list_commands.call_args_list}
        assert "pytest" in called_package_names
        assert result["allowed_commands"] == [{"argv": ["pytest", "--collect-only"]}]
        assert any(e["package_name"].lower() == "pytest" for e in result["cli_packages"])
