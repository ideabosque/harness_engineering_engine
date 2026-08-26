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
    hsk_skill_artifact_bucket="my-artifacts",
)
```

## Key Configuration Settings

All settings use the `HSK_` prefix and are read from environment variables first, then from the setting dict.

| Setting | Required | Default |
|---|---|---|
| `hsk_skill_root` | Yes | — |
| `hsk_skill_artifact_bucket` | For deployed skills | — |
| `hsk_skill_artifact_prefix` | No | `skills/` |
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
    skill_refresh.py       # refreshLocalSkills — S3 download + atomic replace
    skill_registration.py  # registerSkills — scan + upsert
    skill_deployment.py    # deploySkillPackage — GitHub/ZIP intake + S3 + register
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

`skill(name)` (and, through it, the `mcp_skill_provider` `get_skill` MCP tool) is the only path that returns the SKILL.md body: `queries/skill.py::resolve_skill` dispatches `name` lookups to `handlers/skill_reader.py::skill()`, which resolves the active enabled version, refreshes the local cache from S3 if it is missing or stale, and returns `body`, `allowed_commands`, `cli_packages`, `local_content_checksum`, and `stale_index` on `SkillType`. `skill_uuid` lookups (admin/management use) fall back to a plain repo row fetch with no body and no refresh — use `skills(...)` for catalog browsing instead.

`deploySkillPackage` auto-activates a skill's first-ever version (`is_active=true`) so it is retrievable immediately; every version deployed after that lands inactive and needs an explicit `promoteSkillVersion` before agents see it.

## Testing

```bash
python -m pytest harness_engineering_engine/tests -v
```

Current coverage: 33 tests across frontmatter parsing, checksums, config, command executor, `queries.skill::resolve_skill` (name-lookup dispatch, not-found handling, uuid fallback), and `skill_deployment` (first-version auto-activation). Not yet covered: full deployment against a real GitHub source, `refreshLocalSkills`, `registerSkills`, GraphQL mutations end to end, auth/tenant boundaries, MCP integration, and command-executor timeout/output-cap/path-traversal behavior — see `docs/DEVELOPMENT_PLAN.md` §15 for the tracked list.

## Security Notes

- `run_command` **never** uses `shell=True`; it always passes structured argv lists.
- `allowed_commands` uses structured argv entries, not shell strings.
- Command execution is deny-by-default: a skill with no `allowed_commands` entry cannot run any command.
- Shell metacharacters (`|`, `&`, `;`, `>`, `<`, `` ` ``, `$(` ) are rejected before argument matching.
- The `HSK_SKILL_ROOT` path is normalized and enforced — filesystem reads are constrained to the configured root.
- In production, set `HSK_ALLOW_UNREGISTERED_CHANGES=false` (or omit it entirely) so stale local content is rejected.
