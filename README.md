# Harness Engineering Engine

Agent-facing skill catalog, deployment, and retrieval for SilvaEngine.

---

## Quick Start

### Requirements

- Python 3.8+
- `HSK_SKILL_ROOT` environment variable pointing to your skills directory
- AWS credentials if using DynamoDB backend (or PostgreSQL for PG mode)

### Installation

```bash
pip install -e .
```

### Running

```python
import logging
from harness_engineering_engine.main import HarnessEngineeringEngine

engine = HarnessEngineeringEngine(
    logger=logging.getLogger(),
    db_backend="dynamodb",
    region_name="us-east-1",
    aws_access_key_id="...",
    aws_secret_access_key="...",
    hsk_skill_root="skills/",
    hsk_skill_artifact_bucket="my-skill-artifacts",
)
```

### GraphQL Endpoints

The engine exposes the following GraphQL operations:

| Operation | Type | Permission |
|---|---|---|
| `ping` | Query | — |
| `skills` | Query | Admin |
| `search_skills` | Query | Tenant-read |
| `skill(name)` | Query | Tenant-read |
| `insertUpdateSkill` | Mutation | Admin |
| `deleteSkill` | Mutation | Admin |
| `deploySkillPackage` | Mutation | Admin |
| `refreshLocalSkills` | Mutation | Admin |
| `rollbackSkill` | Mutation | Admin |
| `promoteSkillVersion` | Mutation | Admin |
| `disableSkill` | Mutation | Admin |
| `pruneSkillVersions` | Mutation | Admin |
| `registerSkills` | Mutation | Admin |
| `runCommand` | Mutation | Tenant-execute + allowlist |

`skill(name: "...")` is the agent-facing read path: it resolves the active version, refreshes the local runtime cache from S3 if it's missing or stale, and returns the full `SKILL.md` body plus `allowedCommands`, `cliPackages`, `localContentChecksum`, and `staleIndex`. Looking up by `skillUuid` instead returns the raw registration row (no body, no refresh) for admin tooling.

`deploySkillPackage` activates a skill's first-ever version automatically. Every version deployed after that stays inactive until you call `promoteSkillVersion(name, version)` — deploy alone never replaces what agents are currently served.

---

## Configuration

All settings can be provided via environment variables (prefix `HSK_`) or
passed directly to `Config.initialize()` as an engine setting dict.

| Setting | Required | Purpose |
|---|---|---|
| `HSK_SKILL_ROOT` | Yes | Folder containing skill directories. |
| `HSK_SKILL_ARTIFACT_BUCKET` | For deployed skills | S3 bucket for versioned ZIP artifacts. |
| `HSK_SKILL_ARTIFACT_PREFIX` | No | S3 key prefix (default `skills/`). |
| `HSK_SKILL_LOCAL_METADATA_FILE` | No | Metadata filename (default `.hsk-skill.json`). |
| `HSK_SKILL_REFRESH_ON_STARTUP` | No | Refresh local cache on startup. |
| `HSK_ALLOW_UNREGISTERED_CHANGES` | No | Allow reading stale local checksum (default `false`). |
| `HSK_RUN_COMMAND_ENABLED` | No | Global kill switch for `run_command` (default `false`). |
| `HSK_RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS` | No | Default timeout (default `30`). |
| `HSK_RUN_COMMAND_OUTPUT_LIMIT_BYTES` | No | Default output cap (default `20000`). |
| `HSK_RUN_COMMAND_WORKSPACE_ROOT` | No | Workspace root for `workspace_dir` scope. |
| `HSK_DRY_RUN` | No | Resolve and validate without executing (default `false`). |

---

## Skill Format

Each skill is a directory under `HSK_SKILL_ROOT` containing a `SKILL.md`:

```
skills/
  my-skill/
    SKILL.md
    scripts/
      helper.py
```

`SKILL.md` uses YAML frontmatter + markdown body:

```markdown
---
name: my-skill
description: >
  What the skill does and when to use it.
allowed_commands:
  - argv: ["python", "scripts/helper.py"]
---

You are a helpful assistant. Follow these steps...
```

---

## Development

```bash
# Install in dev mode
pip install -e .[dev]

# Run tests
python -m pytest harness_engineering_engine/tests -v

# Type checking
mypy harness_engineering_engine
```

---

## License

MIT
