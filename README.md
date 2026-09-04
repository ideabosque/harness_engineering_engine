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

`skill(name: "...")` is the agent-facing read path: it resolves the active version, refreshes the local runtime cache straight from git if it's missing or stale, and returns the full `SKILL.md` body plus `allowedCommands`, `cliPackages`, `localContentChecksum`, and `staleIndex`. Looking up by `skillUuid` instead returns the raw registration row (no body, no refresh) for admin tooling.

`deploySkillPackage` activates a skill's first-ever version automatically. Every version deployed after that stays inactive until you call `promoteSkillVersion(name, version)` — deploy alone never replaces what agents are currently served.

### Skills are sourced from git only — no artifact store, no ZIP upload

There is no S3 (or any other) artifact store, and no ZIP-upload path, in between a skill's source and the agent that reads it:

- `deploySkillPackage` clones the given git remote at `gitRef` (a branch or tag), validates each `SKILL.md` found, and registers the resolved commit SHA (`resolvedCommit`) alongside the ref. Re-deploying the same `gitRef` is a cheap no-op: the ref is resolved to a commit via `git ls-remote` (no clone) and compared against what's already registered — git is the *only* thing consulted to decide whether a new version exists.
- Every deployed version's content is cached locally so `promoteSkillVersion`/`rollbackSkill` can switch versions instantly with no remote fetch. A host that never cached a given version falls back to fetching straight from git, pinned to that exact commit.
- **HTTPS remotes** use whatever git credential helper is already configured on the host. **SSH remotes** (`git@host:org/repo.git` or `ssh://...`) use the system's default SSH identity (`ssh-agent` / `~/.ssh/config`) — set `HSK_GIT_SSH_KEY_PATH` only if a skill source needs a different key than your default one.

---

## Configuration

All settings can be provided via environment variables (prefix `HSK_`) or
passed directly to `Config.initialize()` as an engine setting dict.

| Setting | Required | Purpose |
|---|---|---|
| `HSK_SKILL_ROOT` | Yes | Folder containing skill directories. |
| `HSK_GIT_SSH_KEY_PATH` | No | Alternate SSH private key for git-over-SSH skill sources. Empty = use the system default identity. |
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
