#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for skill frontmatter parsing."""
from __future__ import print_function

import pytest

from harness_engineering_engine.handlers.skill_frontmatter import (
    parse_frontmatter,
)


class TestFrontmatterParsing:
    """Unit tests for YAML frontmatter parsing."""

    def test_valid_skill_with_all_fields(self):
        text = """---
name: rfq-assistant
description: >
  RFQ collection and quotation workflow.
allowed_commands:
  - argv: ["python", "scripts/build_quote.py"]
cli_packages:
  - package_name: my-package
    command: my-package
---

You are an RFQ assistant.
"""
        result = parse_frontmatter(text)
        assert result.frontmatter.name == "rfq-assistant"
        assert "RFQ collection" in result.frontmatter.description
        assert len(result.frontmatter.allowed_commands) == 1
        assert len(result.frontmatter.cli_packages) == 1
        assert "You are an RFQ assistant" in result.body

    def test_valid_skill_minimal(self):
        text = """---
name: minimal
description: >
  A minimal skill.
---

Do the thing.
"""
        result = parse_frontmatter(text)
        assert result.frontmatter.name == "minimal"
        assert result.frontmatter.allowed_commands == []

    def test_missing_frontmatter(self):
        with pytest.raises(ValueError, match="does not contain a YAML frontmatter"):
            parse_frontmatter("No frontmatter here.\n")

    def test_malformed_yaml(self):
        text = "---\nname: bad\n: invalid\n---\n\nBody.\n"
        with pytest.raises(ValueError, match="Invalid YAML frontmatter"):
            parse_frontmatter(text)

    def test_missing_name(self):
        text = """---
description: A skill without a name.
---

Body.
"""
        with pytest.raises(ValueError, match="'name' is required"):
            parse_frontmatter(text)

    def test_missing_description(self):
        text = """---
name: no-description
---

Body.
"""
        with pytest.raises(ValueError, match="'description' is required"):
            parse_frontmatter(text)

    def test_allowed_commands_not_a_list(self):
        text = """---
name: bad-commands
description: A skill with bad commands.
allowed_commands: "not a list"
---

Body.
"""
        with pytest.raises(ValueError, match="'allowed_commands' must be a list"):
            parse_frontmatter(text)

    def test_reference_files_parsed_and_defaults_empty(self):
        text = """---
name: rfq-assistant
description: RFQ workflow.
reference_files:
  - "references/dispatch_table.md"
  - "config/*.yaml"
---

Body.
"""
        result = parse_frontmatter(text)
        assert result.frontmatter.reference_files == [
            "references/dispatch_table.md",
            "config/*.yaml",
        ]

        minimal = parse_frontmatter(
            "---\nname: minimal\ndescription: Minimal.\n---\n\nBody.\n"
        )
        assert minimal.frontmatter.reference_files == []

    def test_reference_files_not_a_list(self):
        text = """---
name: bad-references
description: Bad reference_files.
reference_files: "not a list"
---

Body.
"""
        with pytest.raises(ValueError, match="'reference_files' must be a list"):
            parse_frontmatter(text)

    def test_declared_fields_distinguishes_absent_from_empty(self):
        """P9 needs to tell 'author wrote an empty list on purpose' apart
        from 'author never mentioned this field' — raw_frontmatter is the
        mechanism; parsed.frontmatter alone can't make that distinction."""
        explicit_empty = parse_frontmatter(
            "---\nname: a\ndescription: d\nallowed_commands: []\n---\n\nBody.\n"
        )
        absent = parse_frontmatter(
            "---\nname: a\ndescription: d\n---\n\nBody.\n"
        )
        assert explicit_empty.frontmatter.allowed_commands == []
        assert absent.frontmatter.allowed_commands == []
        assert "allowed_commands" in explicit_empty.raw_frontmatter
        assert "allowed_commands" not in absent.raw_frontmatter

    def test_leading_utf8_bom_is_stripped(self):
        """Some editors/tools default to 'UTF-8 with BOM' — a leading
        U+FEFF must not make an otherwise-valid SKILL.md look frontmatter-
        less. Found live: every SKILL.md in a real repo saved this way
        failed to deploy with "does not contain a YAML frontmatter block"
        even though the file was visually correct."""
        text = "﻿---\nname: bom-test\ndescription: Has a BOM.\n---\n\nBody.\n"
        result = parse_frontmatter(text)
        assert result.frontmatter.name == "bom-test"
        assert result.body == "Body."

    def test_body_is_stripped(self):
        text = """---
name: strip-test
description: Strip test.
---

  Hello world.

"""
        result = parse_frontmatter(text)
        assert result.body == "Hello world."
