# Agent Notes for harness_engineering_engine

## Project Basics

- Python project managed via `pyproject.toml` (setuptools backend).
- Package name: `harness_engineering_engine`.
- CLI entrypoint: none (engine is loaded as a SilvaEngine service via the `deploy()` manifest).
- Dev dependencies: `pytest`, `black`, `flake8`, `mypy`.
- Postgresql backend is optional: `pip install harness-engineering-engine[postgresql]`.

## Running

```python
import logging
from harness_engineering_engine.main import HarnessEngineeringEngine

engine = HarnessEngineeringEngine(
    logger=logging.getLogger(),
    db_backend="dynamodb",                        # or "postgresql"
    region_name="us-east-1",
    aws_access_key_id="...",
    aws_secret_access_key="...",
    hsk_skill_root="skills/",                     # absolute or relative path
)
```

## Key Configuration Settings

All settings use the `HSK_` prefix and are read from environment variables first, then from the setting dict.

| Setting | Required | Default |
|---|---|---|
| `hsk_skill_root` | Yes | — |
| `hsk_git_ssh_key_path` | No | — (system default SSH identity) |
| `hsk_skill_local_metadata_file` | No | `.hsk-skill.json` |
| `hsk_skill_refresh_on_startup` | No | `False` |
| `hsk_allow_unregistered_changes` | No | `False` |
| `hsk_run_command_enabled` | No | `False` |
| `hsk_run_command_default_timeout_seconds` | No | `30` |
| `hsk_run_command_output_limit_bytes` | No | `20000` |
| `hsk_run_command_workspace_root` | No | — |
| `hsk_dry_run` | No | `False` |

## Architecture

```
harness_engineering_engine/
  handlers/
    config.py              # Centralized Config class (like rfq_engine)
    skill_frontmatter.py   # YAML frontmatter parser
    checksums.py           # Content / artifact checksums
    skill_path.py          # Guarded filesystem helpers
    skill_reader.py        # skill(name) — on-demand refresh + body retrieval
    skill_refresh.py       # refreshLocalSkills — git fetch + atomic replace
    skill_registration.py  # registerSkills — scan + upsert
    skill_deployment.py    # deploySkillPackage — git intake + local install + register
    skill_version_cache.py # per-version local cache backing promote/rollback
    git_client.py          # git ls-remote / clone, SSH identity handling
    command_executor.py    # Guarded run_command
    cli_package_manager.py # CLI package registration/install (v1.1)
    dynamodb/          # PynamoDB models
    postgresql/        # SQLAlchemy models
    repositories/      # Backend-dispatch layer (get_repo)
  queries/             # Graphene query resolvers
  mutations/           # Graphene mutation resolvers
  schema.py            # GraphQL Query / Mutations / type_class()
  main.py              # HarnessEngineeringEngine, deploy(), dispatch_graphql()
  types/               # Graphene ObjectTypes
  skills/              # Starter skills
```

## Code Style

- Follow SilvaEngine conventions: `__future__` import, `__author__ = "bibow"` header, single quotes for dict keys, type hints.
- All DB access goes through `get_repo("skill")` — never import model classes directly outside of `models/`.
- GraphQL mutations follow the `InsertUpdateXxx` / `DeleteXxx` / `ActionXxx` pattern.
- `searchSkills` is agent-facing; `skills` is admin-only (never expose the full catalog to agents as an MCP tool).
- Do not alias a `Query` `Field`'s argument via `foo = String(name="bar")` expecting the resolver to receive `kwargs["bar"]` — graphene keeps the *Python-side* key (`foo`) as the resolver kwarg and only renames the argument as seen by GraphQL clients. `schema.py`'s `skill`/`skills` fields hit this directly: declare arguments with the name you want to read in the resolver (`name`, `description`), not an aliased one.

## Agent-facing read path

`skill(name)` (and, through it, the `mcp_skill_provider` `get_skill` MCP tool) is the only path that returns the SKILL.md body: `queries/skill.py::resolve_skill` dispatches `name` lookups to `handlers/skill_reader.py::skill()`, which resolves the active enabled version, refreshes the local cache straight from git if it is missing or stale, and returns `body`, `allowed_commands`, `cli_packages`, `local_content_checksum`, and `stale_index` on `SkillType`. `skill_uuid` lookups (admin/management use) fall back to a plain repo row fetch with no body and no refresh — use `skills(...)` for catalog browsing instead.

`deploySkillPackage` auto-activates a skill's first-ever version (`is_active=true`) so it is retrievable immediately; every version deployed after that lands inactive and needs an explicit `promoteSkillVersion` before agents see it.

## Skills are sourced from git only — no artifact store, no ZIP upload

There is no S3 (or any other) intermediate artifact store, and no ZIP-upload path, between a skill's source and the agent that reads it:

- `handlers/git_client.py` clones the remote at `git_ref` and `handlers/skill_deployment.py` registers the resolved commit SHA (`resolved_commit`). Re-deploying the same `git_ref` is a cheap no-op — the ref is resolved to a commit via `git ls-remote` (no clone) and compared against what's already registered, so git alone decides whether a new version exists. Another host refreshes straight from the same remote, pinned to that commit (`handlers/skill_refresh.py`).
- `handlers/skill_version_cache.py` caches every deployed version's content locally (`HSK_SKILL_ROOT/.hsk-versions/<name>/<version>/`), so `promoteSkillVersion`/`rollbackSkill` (`mutations/skill_management.py`) can swap the live skill directory instantly instead of depending on the on-demand refresh path. A host that never cached a version falls back to fetching it straight from git; `pruneSkillVersions` discards the cache entry for pruned versions (git history remains the durable record).
- SSH remotes (`git@host:...` or `ssh://...`) use the system's default identity; set `HSK_GIT_SSH_KEY_PATH` only to force an alternate key.
- `handlers/checksums.py::compute_content_checksum` prunes hidden directories (e.g. `.git`) via in-place `os.walk` mutation — do not wrap that walk in `sorted()`, which forces the whole tree to be consumed before the prune filter can run and silently re-includes `.git` in the checksum (this bit refresh: two different git-fetch code paths produce differently-shaped `.git` internals for the identical commit).

## Testing

```bash
python -m pytest harness_engineering_engine/tests -v
```

Current coverage: 55 tests across frontmatter parsing, checksums, config, command executor, CLI package manager (registration, install, upgrade, verify, failure handling), git-based skill deployment (auto-activation, promote-gating, redeploy-skip), integration scenarios (registration, git deploy, on-demand git refresh, promote/rollback, command policy), and resilience/reconciliation (missing data, invalid data, disabled skills, cross-tenant RLS, kill-switch, resolved-commit integrity, single-active-version, content checksum).

## Security Notes

- `run_command` **never** uses `shell=True`; it always passes structured argv lists.
- `allowed_commands` uses structured argv entries, not shell strings.
- Command execution is deny-by-default: a skill with no `allowed_commands` entry cannot run any command.
- Shell metacharacters (`|`, `&`, `;`, `>`, `<`, `` ` ``, `$(` ) are rejected before argument matching.
- The `HSK_SKILL_ROOT` path is normalized and enforced — filesystem reads are constrained to the configured root.
- In production, set `HSK_ALLOW_UNREGISTERED_CHANGES=false` (or omit it entirely) so stale local content is rejected.
