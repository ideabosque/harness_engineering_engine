# Continuous Integration Scenarios SOP — Harness Engineering Engine

> **Status: APPROVED — re-certified 2026-09-04.** This SOP was pre-filled by the
> Autonomous Integration Testing Specialist from project discovery, confirmed
> by the owner, and executed through Phase 13. It was originally certified
> 2026-08-27 (v1.1, PostgreSQL + CLI package manager) and **revised 2026-09-04
> (v1.2) to reflect the git-only deployment architecture**: the intermediate
> S3 artifact store and ZIP-upload path were removed, so skills are now cloned
> straight from their git remote, cached locally, and pinned to a resolved
> commit SHA. This revision realigns every scenario, dependency, and
> reconciliation check with that model. **Further revised 2026-09-04 (v1.3)**
> to add live HTTP coverage through the SilvaEngine Gateway (`/{endpoint_id}/harness_graphql`),
> which surfaced and fixed one blocking defect (§7 INT-010, §Defects).

---

## 1. Document Control

| Field | Value |
|---|---|
| SOP title | Harness Engineering Platform CI Integration SOP |
| Version | 1.3.0 |
| Owner / contact | bibo7 (repo owner) |
| Last updated | 2026-09-04 |
| Business domain | generic (agent skill lifecycle management platform) |
| Target environment | **dev** — local machine, local AWS profile (DynamoDB path only), local PostgreSQL |
| Approval status | **approved** (confirmed by owner 2026-08-26; PostgreSQL backend certified; v1.1 CLI package manager added 2026-08-27; v1.2 git-only architecture realignment 2026-09-04; v1.3 live gateway-HTTP coverage added 2026-09-04) |

## 2. Purpose and Scope

Certify that the Harness Engineering platform can deploy, register, refresh,
retrieve, and execute skills end-to-end across the engine, the database layer,
the local skill runtime cache, the CLI package manager, and the MCP integration
surface (`mcp_daemon_engine` + `mcp_skill_provider`) — so that agents can rely
on `searchSkills`, `getSkill`, and `runCommand` behaving correctly over the
GraphQL contract.

- **In scope:**
  - `harness_engineering_engine` — GraphQL surface (queries + mutations), config loader, handlers (frontmatter, checksums, skill reader/refresh/registration/deployment, command executor, CLI package manager)
  - Persistence backends — DynamoDB (`hsk-skills` + `hsk-cli-packages` tables) **and/or** PostgreSQL (`hsk_skills` + `hsk_cli_packages` + RLS), selected via `db_backend`
  - **Git-only skill sourcing** — `deploySkillPackage` clones a git remote at `git_ref`, resolves the commit SHA, and registers `resolved_commit`; there is no S3 artifact store and no ZIP-upload path
  - Local skill runtime cache under `HSK_SKILL_ROOT` (active skill dir) plus per-version cache under `HSK_SKILL_ROOT/.hsk-versions/<name>/<version>/` backing promote/rollback
  - **CLI package management** — registration, GitHub-based installation, version verification, deployment locks, upgrade flow, audit logging (v1.1)
  - `mcp_skill_provider` — MCP tools (`searchSkills`, `getSkill`, `runCommand`) as hosted by `mcp_daemon_engine`
- **Out of scope:**
  - `mcp_daemon_engine` internals (auth, SSE transport) — assumed certified by its own test cycle
  - Production deployment of the engine itself
- **System(s) under test:** `harness_engineering_engine` (primary), plus its dependency path `mcp_daemon_engine → mcp_skill_provider → harness_engineering_engine GraphQL`

> **Architecture note (v1.2):** Skills are sourced from git only. A skill is
> cloned straight from its remote, validated, checksummed, and cached locally
> so `promoteSkillVersion`/`rollbackSkill` can swap versions instantly. The
> registration row records the resolved commit SHA so any host can refresh
> straight from the same remote pinned to that commit. Re-deploying the same
> `git_ref` is a cheap no-op (`git ls-remote` resolves the ref to a commit
> without cloning, then compares against the registered `resolved_commit`).

## 3. Environment and Access

| Item | Value / source |
|---|---|
| Environment target | dev — local Windows machine |
| Base URLs / endpoints | Engine GraphQL: **in-process** via `Schema.execute()` for the unit/integration suite; **and** live HTTP via the SilvaEngine Gateway at `http://127.0.0.1:8765/{endpoint_id}/harness_graphql` (module manifest: `silvaengine_gateway/silvaengine_gateway/module_routes/harness_engineering_engine.yaml`), confirmed 2026-09-04 (v1.3, INT-010); MCP daemon: local in-process / stdio |
| Credential source | local AWS credentials file (`~/.aws/credentials`) — names only, no inline secrets in this SOP |
| Required env vars | `HSK_SKILL_ROOT`, `HSK_RUN_COMMAND_ENABLED`, `HSK_ALLOW_UNREGISTERED_CHANGES`, `HSK_GIT_SSH_KEY_PATH` (optional), `REGION_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `DB_BACKEND`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_SCHEMA`, `PG_TABLE_PREFIX`, `initialize_tables` — all provided via `harness_engineering_engine/tests/.env` (gitignored; populated from `../silvaengine_gateway/tests/.env`) |
| Data stores | **PostgreSQL** (`db_backend=postgresql`, local `silvaengine` DB, `hsk_skills` + `hsk_cli_packages` tables, RLS) — DynamoDB path not certified in this cycle (marked `assumed`) |
| Skill source | **git remotes only** (HTTPS or SSH). SSH remotes use the system default identity unless `HSK_GIT_SSH_KEY_PATH` forces an alternate key. No S3 bucket and no ZIP upload are involved. |
| Messaging / events | none identified in v1 |
| Access constraints | local machine; local AWS dev credentials (DynamoDB path only); local PostgreSQL on localhost:5432; outbound git access (HTTPS public remotes in tests) |
| Provisioning policy | **verified on 2026-09-04:** PostgreSQL `silvaengine@localhost:5432/silvaengine` reachable; `hsk_skills` + `hsk_cli_packages` tables provisioned via `initialize_tables`. Auto-provision allowed for: local temp dirs, local skill cache, local version cache (`.hsk-versions/`), DB tables via migration. Manual approval required for: package installs, SSH key changes. |

> Names and sources only — no secrets, tokens, or connection strings in this document.
> **Environment validation evidence:**
> - `psycopg2.connect(host=localhost:5432, db=silvaengine)` → OK
> - `information_schema.tables LIKE 'hsk%'` → `['hsk_skills', 'hsk_cli_packages']` (provisioned)
> - `git ls-remote <test remote>` → resolves refs (deploy/refresh dependency)

## 4. Dependency Readiness Requirements

Each dependency must reach: `available → configured → initialized → operational`.

| Dependency | Type | Health check | Required readiness | Owner |
|---|---|---|---|---|
| PostgreSQL (`silvaengine` DB, `hsk_skills` + `hsk_cli_packages` tables, RLS) | infrastructure | connection OK (verified); `initialize_tables` creates/verifies both tables | initialized | this project |
| `silvaengine_dynamodb_base` / `silvaengine_utility` | internal | package import + `Config.initialize` succeeds | operational | SilvaEngine |
| Git client (local `git` + remote access) | infrastructure | `git --version` returns; `git ls-remote` resolves a test remote; `git clone` succeeds for HTTPS test remotes | operational | this project |
| `mcp_daemon_engine` | internal | module loads; `MCPSkillProvider` registered via `loadMcpConfiguration`; local stdio transport | operational | SilvaEngine |
| `mcp_skill_provider` | internal | `MCPSkillProvider` instantiates; `MCP_CONFIGURATION` valid | operational | sibling project |
| `harness_engineering_engine` GraphQL | internal | `ping` query returns via `Schema.execute` | operational | this project |
| `pip` / `importlib.metadata` (CLI package install + verify) | internal | `pip --version` returns; `importlib.metadata` importable | operational | Python stdlib |
| GitHub access (CLI package install path) | external | pip can resolve `git+https://...@ref` for CLI package fixtures (mocked in unit tests) | operational | this project |

## 5. Test Data Requirements

| Asset type | Count | Notes / constraints |
|---|---|---|
| Skill git sources | 3 | synthetic local git remotes (init'd temp repos): (a) minimal valid skill, (b) multi-file skill with `scripts/`, (c) skill with `allowed_commands` — **all deploy tests clone these remotes; no S3, no ZIP** |
| Skill versions per name | 2 | distinct bodies + checksums on different commits, to exercise promote/rollback |
| `allowed_commands` fixtures | 2 entries | structured argv, e.g. `["python", "scripts/build_quote.py", "--spec", "*.json"]` with `timeout_seconds`/`output_limit_bytes` |
| `cli_packages` fixtures | 3 | (a) matching version (no install), (b) missing version (install), (c) outdated version (upgrade) |
| CLI package registration fixtures | 3 | `package_name`, `git_repository_url`, `version`, `git_ref` — mocked pip calls |
| Tenant (partition_key) fixtures | 2 | `endpoint#part` pairs to verify cross-tenant isolation + PostgreSQL RLS |
| `.hsk-skill.json` fixtures | matching | both matching and deliberately stale checksums / stale `resolved_commit` |
| Operator identity | 1 | `updated_by` actor used across mutations |

- **Load order:** config bootstrap (`.env`) → `initialize_tables` (`hsk_skills` + `hsk_cli_packages` + RLS) → init synthetic git remotes → skill registration rows → local skill cache + version cache → CLI package registration → MCP module registration.
- **Data source:** generated synthetic git remotes and fixtures under the tests folder (no production data).

## 6. Execution Order

Dependency-driven order derived from this project's actual dependency graph (engine → storage → git sourcing → CLI packages → MCP layer), not the generic commerce default:

```text
Foundation (config, auth context)
  -> Schema/Storage (PostgreSQL tables)
  -> Skill Registration & Index (scan HSK_SKILL_ROOT)
  -> Deployment (git remote -> clone -> validate -> local cache -> registration)
  -> Redeploy skip (git ls-remote resolves ref -> compare resolved_commit)
  -> Local Refresh & Retrieval (on-demand clone pinned to resolved_commit)
  -> Lifecycle Management (promote, rollback, disable, prune — local version cache)
  -> CLI Package Management (register, install, verify, upgrade, audit)
  -> Guarded Command Execution (allowlist, policy, CLI package ensure)
  -> MCP Integration (searchSkills -> getSkill -> runCommand via mcp_daemon_engine)
```

**Reason for deviation from the default sequence:** this platform is an agent skill
registry, not a commerce system; the commerce sequence does not apply.

## 7. Integration Scenarios

### INT-001 — Register skills from local folders

| Field | Value |
|---|---|
| **ID** | INT-001 |
| **Name** | `registerSkills` scans `HSK_SKILL_ROOT` and upserts the index |
| **Priority** | P1 |
| **Type** | end-to-end (GraphQL mutation → DB) |
| **CI trigger** | on pull request |
| **Preconditions** | `HSK_SKILL_ROOT` set; ≥3 skill folders with valid `SKILL.md` |
| **Dependencies** | engine, DB backend |
| **Test data** | synthetic skill folders incl. one with `allowed_commands` |
| **Steps** | 1. Place skill folders under root. 2. Call `registerSkills(root, prune=true)`. 3. Re-run to verify idempotency. |
| **Expected behavior** | Rows created per skill/version; checksums stored; second run skips unchanged rows |
| **Validation points** | registration rows exist; `content_checksum` matches recompute; duplicate names rejected |
| **Cross-system checks** | DB rows == folders on disk |
| **Status** | ✅ passing |

### INT-002 — Deploy from git remote → local cache → registration

| Field | Value |
|---|---|
| **ID** | INT-002 |
| **Name** | `deploySkillPackage` from a git remote |
| **Priority** | P1 |
| **Type** | end-to-end |
| **CI trigger** | on pull request |
| **Preconditions** | test git remote reachable; engine config complete |
| **Dependencies** | engine, git client, DB, local cache |
| **Test data** | synthetic git remote containing a valid `SKILL.md` (single- or multi-skill repo) |
| **Steps** | 1. Init a local git remote with a skill dir + `SKILL.md`. 2. `deploySkillPackage(source=<remote>, git_ref=...)`. 3. Inspect the registration row + local cache. |
| **Expected behavior** | Remote cloned at `git_ref`; commit SHA captured as `resolved_commit`; `content_checksum` computed from the skill dir; version cached under `.hsk-versions/<name>/<version>/`; **first-ever version auto-activated** (`is_active=true`) and installed into `HSK_SKILL_ROOT/<name>/`; `.hsk-skill.json` written |
| **Validation points** | `resolved_commit` == `git rev-parse` of the ref; `content_checksum` matches recompute over the skill dir (`.git` pruned) |
| **Cross-system checks** | DB row points at `git_repository_url` + `resolved_commit`; local active dir == cloned content (dotfiles excluded) |
| **Status** | ✅ passing |

### INT-003 — Redeploy at same commit is a no-op

| Field | Value |
|---|---|
| **ID** | INT-003 |
| **Name** | `deploySkillPackage` skips when `git_ref` resolves to the already-registered commit |
| **Priority** | P1 |
| **Type** | end-to-end |
| **CI trigger** | on pull request |
| **Preconditions** | INT-002 has deployed a skill at a known commit |
| **Dependencies** | engine, git client, DB |
| **Test data** | the same remote + `git_ref` used in INT-002 |
| **Steps** | 1. Re-issue `deploySkillPackage(source, git_ref)` for the already-deployed skill. 2. Assert no clone and no new registration row. |
| **Expected behavior** | `git ls-remote` resolves the ref to a SHA **without cloning**; the SHA matches the registered `resolved_commit` (same `git_repository_url` + `git_ref`); deploy returns `skipped=[name]` with `deployed=[]`, `failed=[]`. No version cache write, no local dir mutation. |
| **Validation points** | return shape `skipped` populated; no new `skill_uuid`; no clone performed |
| **Cross-system checks** | DB row count unchanged; local cache untouched |
| **Status** | ✅ passing |

### INT-004 — On-demand refresh on retrieval (stale/missing cache)

| Field | Value |
|---|---|
| **ID** | INT-004 |
| **Name** | `skill(name)` refreshes from git when local metadata is stale |
| **Priority** | P1 |
| **Type** | end-to-end |
| **CI trigger** | on pull request |
| **Preconditions** | INT-002 complete for at least one version |
| **Dependencies** | engine, git client, local cache |
| **Test data** | deployed version; locally corrupted or missing `.hsk-skill.json`, or a `resolved_commit` that differs from the registered one |
| **Steps** | 1. Corrupt/delete local metadata (or bump the registered commit). 2. Call `skill(name)`. 3. Inspect cache dir. |
| **Expected behavior** | remote cloned **pinned to the registered `resolved_commit`** (falling back to `git_ref` if the remote rejects fetching by exact SHA); skill dir located, validated, checksummed; local dir atomically replaced; `.hsk-skill.json` rewritten; correct body returned |
| **Validation points** | `local_content_checksum` == registered `content_checksum`; `.hsk-skill.json` `resolved_commit` == registered; previous valid version preserved if the new fetch fails validation |
| **Cross-system checks** | returned body == content at the registered commit |
| **Status** | ✅ passing |

### INT-005 — Promote / rollback lifecycle

| Field | Value |
|---|---|
| **ID** | INT-005 |
| **Name** | `promoteSkillVersion` and `rollbackSkill` switch active version via the local version cache |
| **Priority** | P1 |
| **Type** | end-to-end |
| **CI trigger** | nightly |
| **Preconditions** | ≥2 versions of one skill registered (both previously deployed/cached) |
| **Dependencies** | engine, DB, local version cache |
| **Test data** | two distinct versions with different bodies on different commits |
| **Steps** | 1. Activate v2 (promote). 2. Call `skill(name)` → expect v2 content. 3. Roll back to v1. 4. Call `skill(name)` → expect v1 content after refresh. |
| **Expected behavior** | Promote/rollback swap the live skill directory from `.hsk-versions/<name>/<version>/` **instantly** (no remote fetch when the version is cached); a host that never cached a version falls back to fetching it from git. `rollbackSkill` is a management-state change only; the next retrieval refreshes the local dir if needed. |
| **Validation points** | `is_active` flags; only one active version per name; local dir matches active version; `pruneSkillVersions` discards the cache entry for pruned versions (git history remains the durable record) |
| **Cross-system checks** | DB active row == local cache dir == content at registered commit |
| **Status** | ✅ passing |

### INT-006 — Agent-facing flow through MCP daemon

| Field | Value |
|---|---|
| **ID** | INT-006 |
| **Name** | `searchSkills → getSkill → runCommand` via `mcp_daemon_engine` |
| **Priority** | P1 |
| **Type** | end-to-end (local MCP daemon, stdio/in-process transport) |
| **CI trigger** | pre-release |
| **Preconditions** | `mcp_daemon_engine` running locally with `mcp_skill_provider` registered via `loadMcpConfiguration`; provider's `graphql_modules.harness_engineering` points at the in-process engine dispatch; `HSK_RUN_COMMAND_ENABLED=true` in tests/.env |
| **Dependencies** | mcp_daemon_engine, mcp_skill_provider, harness_engineering_engine |
| **Test data** | skill with an allowlisted trivial command (from INT-002 fixtures) |
| **Steps** | 1. `searchSkills(query)`. 2. `getSkill(name)`. 3. `runCommand(name, argv)` matching the allowlist. |
| **Expected behavior** | Ranked results; full skill body + `stale_index`; command executes only when allowlisted |
| **Validation points** | MCP tool result shapes; tenant identity inherited from daemon context; full catalog never exposed to agent (`skills` is admin-only; `searchSkills` is agent-facing) |
| **Cross-system checks** | MCP results match direct GraphQL results |
| **Status** | ⏭ deferred |

### INT-007 — Guarded command executor policy (security)

| Field | Value |
|---|---|
| **ID** | INT-007 |
| **Name** | `runCommand` allowlist + path traversal + shell rejection |
| **Priority** | P1 |
| **Type** | API / security |
| **CI trigger** | on pull request (blocks merge) |
| **Preconditions** | INT-002 deployed skill with structured allowlist |
| **Dependencies** | engine, command executor |
| **Test data** | benign allowlisted command; hostile inputs |
| **Steps** | 1. Run allowlisted argv → expect success. 2. Run non-allowlisted argv → expect rejection. 3. Inject shell metacharacters (`|`, `;`, `$(...)`) → expect rejection. 4. Attempt path traversal (`../../`) → expect rejection. 5. Exceed timeout → expect timeout=true. 6. Exceed output cap → expect truncated=true. 7. With `HSK_RUN_COMMAND_ENABLED=false` → all calls denied. |
| **Expected behavior** | Deny-by-default; structured argv only (never `shell=True`); no shell; constrained paths; timeout and output caps enforced |
| **Validation points** | audit log entries per execution; exit_code/timed_out/truncated returned accurately |
| **Cross-system checks** | no subprocess spawned unless allowlist matches |
| **Status** | ✅ passing |

### INT-008 — CLI package registration and management (v1.1)

| Field | Value |
|---|---|
| **ID** | INT-008 |
| **Name** | `insertUpdateCliPackage` registers a CLI package; `ensureCliPackage` installs/verifies it |
| **Priority** | P1 |
| **Type** | end-to-end (GraphQL mutation → DB → pip install → verify) |
| **CI trigger** | on pull request |
| **Preconditions** | `hsk_cli_packages` table provisioned; `pip` available |
| **Dependencies** | engine, DB, pip, importlib.metadata |
| **Test data** | synthetic package registrations with mocked pip calls |
| **Steps** | 1. `insertUpdateCliPackage` with `package_name`, `git_repository_url`, `version`, `git_ref`. 2. Re-register to update version → verify `operation=updated`. 3. `ensureCliPackage` with matching installed version → `status=ready` (no install). 4. `ensureCliPackage` with missing installed version → triggers pip install → `status=ready`. 5. `ensureCliPackage` with outdated installed version → triggers uninstall + reinstall → `status=ready`. 6. `ensureCliPackage` with install failure → `status=error`. 7. `ensureCliPackage` with verification failure → `status=error`. |
| **Expected behavior** | Registration upserts by name; `ensure_package` compares installed vs registered version; missing/outdated triggers `pip install git+url@ref`; verification via `importlib.metadata.version()`; process-level lock prevents races; audit log emitted for every operation |
| **Validation points** | registration row exists; `operation` field correct; lock acquired/released; audit log entries; pip install/uninstall called with correct args (`shell=False`) |
| **Cross-system checks** | DB registration row == ensure_package input; installed version == registered version after successful install |
| **Status** | ✅ passing |

### INT-009 — CLI package ensure before runCommand (v1.1)

| Field | Value |
|---|---|
| **ID** | INT-009 |
| **Name** | `runCommand` calls `ensure_package` when skill declares `cli_packages` |
| **Priority** | P2 |
| **Type** | integration (command executor → CLI package manager) |
| **CI trigger** | nightly |
| **Preconditions** | INT-002 deployed skill with `cli_packages` in frontmatter; package registered via INT-008 |
| **Dependencies** | engine, command executor, CLI package manager |
| **Test data** | skill with `cli_packages` entry; mocked `ensure_package` returning ready/error |
| **Steps** | 1. Deploy skill with `cli_packages` frontmatter. 2. `runCommand` with allowlisted argv. 3. Verify `ensure_package` was called for each `cli_packages` entry. 4. Mock `ensure_package` returning error → `runCommand` blocked. |
| **Expected behavior** | CLI packages ensured before command execution; failure blocks command |
| **Validation points** | `ensure_package` called with correct `package_name`; command blocked on package error |
| **Cross-system checks** | command executor → cli_package_manager → pip (mocked) |
| **Status** | ✅ passing (covered by unit tests in `test_command_executor.py` + `test_cli_package.py`) |

### INT-010 — `harness_graphql` via SilvaEngine Gateway (live HTTP)

| Field | Value |
|---|---|
| **ID** | INT-010 |
| **Name** | `deploySkillPackage` → `skill` → redeploy-skip, driven end-to-end over real HTTP through the SilvaEngine Gateway rather than in-process `Schema.execute()` |
| **Priority** | P1 |
| **Type** | end-to-end (live HTTP, real DB, real public git remote) |
| **CI trigger** | pre-release |
| **Preconditions** | SilvaEngine Gateway running locally on `:8765` with `harness_engineering_engine` registered via `module_routes/harness_engineering_engine.yaml`; gateway `tests/.env` provides `HSK_*`, `PG_*`, `ADMIN_STATIC_TOKEN` |
| **Dependencies** | silvaengine_gateway, harness_engineering_engine, PostgreSQL, git, outbound HTTPS to github.com |
| **Test data** | real public repo `https://github.com/ideabosque/autonomous-integration-testing-specialist.git` (this skill's own repo) |
| **Steps** | 1. `POST /{endpoint_id}/harness_graphql` with `query { ping }`. 2. `query { skills { skillList { ... } } }`. 3. `deploySkillPackage(gitRepositoryUrl, gitRef: "main")`. 4. `skill(name)` retrieval. 5. Re-issue `deploySkillPackage` with `skillName` set — expect `skipped`. |
| **Expected behavior** | All five calls return HTTP 200 with correct GraphQL `data`; deploy auto-activates the first version and resolves a real commit SHA; retrieval returns the correct body; the redeploy is a no-op (`skipped`, no reclone) |
| **Validation points** | HTTP status 200 on every call; `resolvedCommit` matches a real commit; `skill(name)` echoes `resolvedCommit`/`gitRef`; second deploy returns `skipped=[name]` |
| **Cross-system checks** | gateway route → `harness_engineering_engine.main:dispatch_graphql` → `Config`-backed PostgreSQL row == HTTP response body |
| **Status** | ✅ passing (2026-09-04, after defect DEF-001 fixed — see below) |

**Defect found and fixed during this run (DEF-001):**

- **Symptom:** every `POST /{endpoint_id}/harness_graphql` call returned `HTTP 500 Internal Server Error` (generic body, no traceback surfaced to the client).
- **Root cause:** `silvaengine_gateway/silvaengine_gateway/module_routes/harness_engineering_engine.yaml` declared `config_init_style: kwargs`, which makes the gateway call `Config.initialize(logger, **module_setting)` (spreading ~106 gateway settings as individual keyword arguments). `harness_engineering_engine.handlers.config.Config.initialize` has the signature `initialize(cls, logger, setting: Dict)` — a single dict parameter, not `**kwargs`. The mismatch raised `TypeError: Config.initialize() got an unexpected keyword argument 'region_name'` at gateway startup. `init_module_configs()` catches that exception and only logs a warning, so the gateway still started and still registered the `/{endpoint_id}/harness_graphql` route — but `Config` was left uninitialized. Every subsequent request then failed inside `dispatch_graphql()` at `HarnessEngineeringEngine(logger, **Config.get_setting())`, since `Config.get_setting()` returned `None`.
- **Evidence:** gateway startup log — `Module 'harness_engineering_engine': Config.initialize() failed: Config.initialize() got an unexpected keyword argument 'region_name'`, followed later by `Module 'harness_engineering_engine' startup hook failed: Config not initialized` (the scheduler's `on_startup` hook failing for the same reason).
- **Fix:** changed `config_init_style` from `kwargs` to `dict` in `module_routes/harness_engineering_engine.yaml` (repo: `silvaengine_gateway`, uncommitted as of this SOP revision — pending owner confirmation to commit). After the fix and a gateway restart, startup logged `Module 'harness_engineering_engine': Config initialized (style=dict, keys=106)` and `Module 'harness_engineering_engine' startup hook completed`; all INT-010 steps then passed.
- **Severity:** blocking (the entire gateway-HTTP path for this module was unusable until fixed) — but scoped to the gateway's own module manifest, not to any code inside `harness_engineering_engine` itself.
- **Blast radius note:** fixing this required restarting the shared local SilvaEngine Gateway process, which also restarts every other engine it hosts (marketing, RFQ, core agent, A2A, etc.) — done with explicit owner approval mid-session.

## 8. Failure and Resilience Scenarios

| Scenario | Injected fault | Expected behavior | Status |
|---|---|---|---|
| missing_data | `skill(name)` for unregistered name | clear not-found error (returns null) | ✅ passing |
| missing_data | `skill(name)` with local cache deleted mid-run | refresh from git (pinned to `resolved_commit`) succeeds or previous valid version preserved | ✅ passing |
| invalid_data | SKILL.md missing frontmatter / missing name / missing description | registration + deployment reject with validation error | ✅ passing |
| invalid_data | `allowed_commands` as shell string instead of argv list | rejected at validation | ✅ passing |
| git_failures | `git clone`/`git ls-remote` fails mid-deploy/refresh | deploy returns `failed`; local dir untouched; previous valid version remains active | ✅ passing |
| database_failures | DB unavailable during `searchSkills` | graceful GraphQL error, no partial data | ✅ passing |
| checksum_mismatch | local content modified without re-registration while `HSK_ALLOW_UNREGISTERED_CHANGES=false` | read rejected / `stale_index=true` surfaced | ✅ passing |
| resolved_commit_integrity | fetched content checksum != registered `content_checksum` for the pinned commit | refresh raises; local dir untouched | ✅ passing |
| auth/tenant | request with a different partition_key | cross-tenant rows invisible (RLS) | ✅ passing |
| cli_package_missing | `ensureCliPackage` for unregistered package | `status=error`, command blocked | ✅ passing |
| cli_package_install_fail | pip install returns non-zero exit code | `status=error`, audit log recorded, command blocked | ✅ passing |
| cli_package_verify_fail | installed version != registered version after install | `status=error`, audit log recorded, command blocked | ✅ passing |
| kill_switch | `HSK_RUN_COMMAND_ENABLED=false` | all `runCommand` calls denied | ✅ passing |

## 9. Data Reconciliation Checks

| Check | Rule | Tolerance | Status |
|---|---|---|---|
| Content integrity | recomputed content checksum (skill dir, `.git`/dotfiles pruned) == `content_checksum` in DB | 0 | ✅ passing |
| Resolved-commit integrity | registered `resolved_commit` == `git rev-parse <git_ref>` of the source remote | 0 | ✅ passing |
| Local consistency | local `.hsk-skill.json` `resolved_commit` == active registered `resolved_commit` | 0 | ✅ passing |
| Count consistency | registered rows for name == distinct deployed commits for name | 0 | ✅ passing |
| Single active version | exactly one `is_active=true` enabled row per (partition, name) | 0 | ✅ passing |
| CLI package version | installed version == registered version after `ensure_package` | 0 | ✅ passing |
| Audit completeness | every `runCommand` execution logged with tenant, argv, exit code | 0 missing | ✅ passing |
| CLI package audit | every install/upgrade/verify operation logged with package, version, status | 0 missing | ✅ passing |
| Rollback purity | `rollbackSkill` mutates no git remotes and writes no local dirs directly (cache swap only) | 0 writes outside DB + cache | ✅ passing |

## 10. Entry and Exit Criteria

**Entry criteria (testing may begin when):**
- This SOP is confirmed by the user.
- Target environment chosen and reachable (PostgreSQL; git remotes for test skills).
- `HSK_*` environment variables configured.
- All P1 dependencies reach `operational` readiness.
- Synthetic test git remotes and fixtures generated and present.
- `hsk_skills` + `hsk_cli_packages` tables provisioned.

**Exit criteria (certification may be issued when):**
- All P1 scenarios (INT-001, INT-002, INT-003, INT-004, INT-005, INT-007, INT-008) pass.
- Coverage ≥ 80% of GraphQL operations on the in-scope surface.
- No blocking or critical defects open.
- Data reconciliation checks (Section 9) clean.
- Failure/resilience scenarios (Section 8) all produce expected behavior.

## 11. CI Trigger and Cadence

| Trigger | Scope run | Required to pass |
|---|---|---|
| On pull request | Existing unit suite (55 tests) + INT-001 + INT-002 + INT-003 + INT-004 + INT-007 + INT-008 | yes — blocks merge |
| Nightly | + INT-005 + INT-009 | report only |
| Pre-release | full suite incl. INT-006 + INT-010 + all failure & reconciliation checks | yes — blocks release |

## 12. Reporting and Certification Expectations

- **Report format:** markdown
- **Required certification decision:** one of `Integration Certified`, `Ready for UAT`, `Ready for Production`, `Ready with Conditions`, `Not Ready`
- **Distribution:** repo owner; reports written to `docs/test_results/`
- Report location: `docs/test_results/integration_certification_report_20260904.md` (latest)

## 13. Sign-off

| Role | Name | Date | Decision |
|---|---|---|---|
| Test owner | bibo7 | 2026-09-04 | Ready with Conditions (PostgreSQL path certified; INT-006, DynamoDB deferred) |

---

## Resolved items

1. ~~Owner/contact~~ — resolved: bibo7 (repo owner).
2. ~~Target environment~~ — resolved: dev (local machine, local AWS profile, local PostgreSQL). Verified 2026-08-26.
3. ~~Backend(s) to certify~~ — resolved: **PostgreSQL** (per local dev stack; DynamoDB path deferred, marked assumed).
4. ~~S3 artifact bucket~~ — **obsolete (v1.2):** the intermediate S3 artifact store was removed. Skills are sourced from git only; there is no `HSK_SKILL_ARTIFACT_BUCKET`, no `s3_version_id`, and no ZIP artifact to checksum.
5. ~~GitHub deploy (INT-003)~~ — **superseded (v1.2):** git deploy is now the only path and is certified as INT-002. INT-003 was repurposed to cover the redeploy-skip no-op (the cheap `git ls-remote` → `resolved_commit` comparison).
6. ~~MCP daemon (INT-006)~~ — local in-process `mcp_daemon_engine` with `mcp_skill_provider` registered.
7. ~~Report distribution~~ — resolved: repo owner only; reports written to `docs/test_results/`.

## Change history

- **v1.1 (2026-08-27)** — CLI package manager addition:
  - INT-008 — CLI package registration and management (register, install, verify, upgrade, audit)
  - INT-009 — CLI package ensure before `runCommand` (command executor wiring)
  - CLI package manager: `register_cli_package`, `ensure_package` with pip install/uninstall, version verification via `importlib.metadata.version()`, process-level deployment locks, audit logging
  - New model: `CliPackageModel` (DynamoDB + PostgreSQL) with RLS
  - New GraphQL surface: `cliPackages` query, `cliPackage` query, `insertUpdateCliPackage` / `deleteCliPackage` / `ensureCliPackage` mutations
  - New migration: `0002_create_cli_packages.py`
  - New test file: `test_cli_package.py`
- **v1.2 (2026-09-04)** — git-only architecture realignment:
  - Removed the S3 artifact store and ZIP-upload path from scope, env, dependencies, test data, scenarios, and reconciliation checks
  - INT-002 rewritten as git-remote → local-cache → registration; INT-003 repurposed as the redeploy-skip no-op
  - INT-004/INT-005 rewritten to refresh/swap from git pinned to `resolved_commit` and the local `.hsk-versions/` cache
  - Added `resolved_commit` integrity checks (§8, §9) and the `.hsk-versions/<name>/<version>/` version cache as a first-class dependency
  - env var list trimmed: `HSK_SKILL_ARTIFACT_BUCKET`/`HSK_SKILL_ARTIFACT_PREFIX` removed; `HSK_GIT_SSH_KEY_PATH` added
  - Test count corrected to 55 (3 checksums + 8 cli_package + 8 command_executor + 5 config + 8 frontmatter + 7 integration + 5 queries_skill + 8 resilience + 3 skill_deployment)
- **v1.3 (2026-09-04)** — live SilvaEngine Gateway HTTP coverage:
  - Added INT-010: `deploySkillPackage` → `skill` → redeploy-skip driven over real HTTP through the gateway (`/{endpoint_id}/harness_graphql`), using the real public repo `github.com/ideabosque/autonomous-integration-testing-specialist` as test data
  - Found and fixed DEF-001: `module_routes/harness_engineering_engine.yaml` (repo `silvaengine_gateway`) declared `config_init_style: kwargs`, incompatible with this project's `Config.initialize(cls, logger, setting: Dict)` signature — every gateway request to `harness_graphql` returned HTTP 500 until fixed to `config_init_style: dict`
  - Section 3 (Base URLs) updated to record both the in-process and live-gateway endpoints as certified