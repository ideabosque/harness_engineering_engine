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
| `ping` | Query | — | Ping the GraphQL service to confirm it is operational. |
| `skills` | Query | Admin | Retrieve the full catalog of registered skills (admin use only). |
| `searchSkills` | Query | Tenant-read | Semantic search across active skills available to an agent. |
| `skill(name)` | Query | Tenant-read | The primary agent read path. Resolves the active version, refreshes the local cache straight from git if stale, and returns the full `SKILL.md` body alongside metadata. |
| `cliPackages` | Query | Admin | Retrieve the full catalog of registered CLI packages. |
| `cliPackage` | Query | Admin | Retrieve metadata for a specific CLI package. |
| `insertUpdateSkill` | Mutation | Admin | Raw database-level skill registration (superseded by `deploySkillPackage`). |
| `deleteSkill` | Mutation | Admin | Delete a skill registration from the system. |
| `deploySkillPackage` | Mutation | Admin | Core deployment path. Clones the given git remote at `gitRef`, resolves the commit SHA, registers the skill, and automatically activates the first-ever version. |
| `refreshLocalSkills` | Mutation | Admin | Force a local refresh from the database for all skills. |
| `rollbackSkill` | Mutation | Admin | Revert the active status of a skill to a previous version. |
| `promoteSkillVersion` | Mutation | Admin | Promote a specific inactive version of a skill to be the active execution version. |
| `disableSkill` | Mutation | Admin | Disable a skill globally for the tenant. |
| `pruneSkillVersions` | Mutation | Admin | Clean up old cached versions from the local file system. |
| `registerSkills` | Mutation | Admin | Scan a local `HSK_SKILL_ROOT` and bulk register all discovered skills. |
| `runCommand` | Mutation | Tenant-execute | Execute a local subprocess for a skill. Checked against the `allowed_commands` frontmatter array for security, with built-in caps on execution time and output size. |
| `insertUpdateCliPackage` | Mutation | Admin | Register or update a CLI dependency package mapped to a GitHub repository. |
| `deleteCliPackage` | Mutation | Admin | Delete a CLI package registration. |
| `ensureCliPackage` | Mutation | Admin | Forces `pip install git+...` installation of a registered CLI package and verifies the installed version. |

### API Operations Details

#### 1. deploySkillPackage (Mutation)

The primary entry point for introducing a new skill into the system. It connects directly to a live Git remote.

```graphql
mutation {
  deploySkillPackage(
    gitRepositoryUrl: "git@github.com:ideabosque/autonomous-integration-testing-specialist.git"
    gitRef: "main"
  ) {
    deployed {
      skillUuid
      name
      version
      isActive
      resolvedCommit
      sourceType
    }
    skipped
    failed {
      name
      error
    }
  }
}
```

* **Behavior**: Clones the repo at `gitRef`, reads `SKILL.md`, calculates internal checksums, and updates the local disk cache (`.hsk-versions/`) and database. Re-deploying an unmodified git commit natively resolves to a cheap `git ls-remote` (no clone) and returns the skill name in the `skipped` array.

#### 2. runCommand (Mutation)

Executes a secure subprocess for a skill. Used heavily by Agent logic to execute associated scripts.

```graphql
mutation {
  runCommand(
    name: "cmd-skill"
    argv: ["python", "--version"]
    timeoutSeconds: 30
    outputLimitBytes: 20000
  ) {
    exitCode
    stdout
    stderr
    timedOut
    truncated
  }
}
```

* **Behavior**: Fails securely with a `PermissionError` if the `argv` does not strictly match an entry in the skill's `allowed_commands` array defined in `SKILL.md`. Safely isolates shell injection and enforces the provided limits.

#### 3. ensureCliPackage (Mutation)

Integrates external Python packages stored on GitHub into the local environment securely.

```graphql
mutation {
  ensureCliPackage(packageName: "multilingual-slide-video-agent") {
    packageName
    status
    message
  }
}
```

* **Behavior**: Checks if the previously registered `multilingual-slide-video-agent` version matches the active local environment (`importlib.metadata.version`). If it is missing or out of date, it triggers a `pip install` automatically against the registered GitHub URL.

#### 4. skill (Query)

The single entry point for an agent to actually consume the contents of a skill.

```graphql
query {
  skill(name: "autonomous-integration-testing-specialist") {
    name
    description
    body
    allowedCommands
    cliPackages
    gitRepositoryUrl
    gitRef
    resolvedCommit
  }
}
```

* **Behavior**: Resolves the skill's active version. If the active local copy on disk is missing or the internal checksum indicates drift, it automatically refreshes from Git pinned perfectly to the `resolvedCommit` recorded in the database. Returns the full `SKILL.md` body for prompt injection.

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

A skill is any directory (anywhere in its git repo — see "Discovery" below) containing a `SKILL.md`:

```
skills/
  my-skill/
    SKILL.md
    scripts/
      helper.py
```

`SKILL.md` uses YAML frontmatter + a markdown instruction body. The frontmatter has two required fields and two optional ones:

```markdown
---
name: my-skill
description: >
  What the skill does and when to use it.
allowed_commands:
  - argv: ["python", "scripts/helper.py"]
  - argv: ["msv", "pipeline", "status"]
cli_packages:
  - package_name: multilingual-slide-video-agent
    git_repository_url: https://github.com/ideabosque/multilingual_slide_video_production_system.git
    version: "0.1.0"
    git_ref: main
---

You are a helpful assistant. Follow these steps...
```

| Field | Required | Purpose |
|---|---|---|
| `name` | Yes | The skill's registered identity — must be unique per tenant. |
| `description` | Yes | Shown in `searchSkills`/`skill` results; also what agents match against. |
| `allowed_commands` | No | The **only** commands `runCommand` will ever execute for this skill (see "Security model" below). Omitted or empty means `runCommand` is fully denied for this skill. |
| `cli_packages` | No | External Python CLI dependencies this skill needs. Each entry needs at least `package_name`; adding `git_repository_url` and `version` makes it auto-registered *and* installed the moment the skill is deployed (see "CLI package dependencies" below) — omit them if the package is already registered separately via `insertUpdateCliPackage`. |

### Discovery

`deploySkillPackage` searches the whole cloned repo recursively for `SKILL.md` files — a skill can live at the repo root, one level down, or nested arbitrarily deep (e.g. a monorepo shaped like `.claude/skills/<name>/SKILL.md`). Every `SKILL.md` found is registered as its own skill, keyed by its own `name`, in a single deploy call.

### Security model — `allowed_commands` is deny-by-default

`allowed_commands` is not a formality — it is the entire boundary between what an admin approved at deploy time and what any tenant-execute caller can trigger via `runCommand` at runtime:

- **Empty or missing means denied, not "anything goes."** `runCommand` raises `PermissionError` immediately if a skill's `allowed_commands` is empty — there is no fallback to "allow everything."
- **Only exact, listed `argv` entries match.** `runCommand` never invokes a shell (`shell=False`) and never accepts an argv that isn't already in this list.
- **Only include commands you actually want a tenant-level caller able to run right now, unprompted.** If a CLI package exposes a command meant to be gated behind a human decision in conversation (a publish/approve/delete-style action), leave it out of `allowed_commands` even though the package is installed and the command technically exists — installing a `cli_packages` dependency does not imply every one of its subcommands should be runnable. There is currently no tooling that generates or suggests this list for you; the author has to already know (e.g. via the CLI's own `--help`) which subcommands exist and decide which ones belong here.

### CLI package dependencies

When a `cli_packages` entry includes `git_repository_url` and `version`, `deploySkillPackage` auto-registers it (equivalent to `insertUpdateCliPackage`) and installs/verifies it immediately (equivalent to `ensureCliPackage`) as part of that same deploy call — not deferred to the first `runCommand`. A failed install fails only that skill's entry in the deploy response (`failed`), it does not abort deploying the rest of a multi-skill repo, and the skill itself is not registered if its dependency can't be installed.

There is no database-level link between a skill and a CLI package — the only association is the `package_name` string appearing in both the skill's `SKILL.md` and the `hsk_cli_packages` registration row, matched at read/execute time. Renaming or deleting a CLI package registration does not update or block any skill that still references its old name; the next `runCommand` call for that skill would simply get a "not registered" error from `ensure_package`.

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
