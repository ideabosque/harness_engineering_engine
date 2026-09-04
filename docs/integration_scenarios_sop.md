# Continuous Integration Scenarios SOP — Harness Engineering Engine

> **Status: APPROVED — certified 2026-08-27.** This SOP was pre-filled by the
> Autonomous Integration Testing Specialist from project discovery, confirmed
> by the owner, executed through Phase 13, and updated for the v1.1 CLI package
> manager addition.

---

## 1. Document Control

| Field | Value |
|---|---|
| SOP title | Harness Engineering Platform CI Integration SOP |
| Version | 1.1.0 |
| Owner / contact | bibo7 (repo owner) |
| Last updated | 2026-08-27 |
| Business domain | generic (agent skill lifecycle management platform) |
| Target environment | **dev** — local machine, local AWS profile, dev S3 bucket, local PostgreSQL |
| Approval status | **approved** (confirmed by owner 2026-08-26; scope: PostgreSQL backend only; v1.1 CLI package manager added 2026-08-27) |

## 2. Purpose and Scope

Certify that the Harness Engineering platform can deploy, register, refresh,
retrieve, and execute skills end-to-end across the engine, the database layer,
S3 artifact storage, the CLI package manager, and the MCP integration surface
(`mcp_daemon_engine` + `mcp_skill_provider`) — so that agents can rely on
`search_skills`, `get_skill`, and `run_command` behaving correctly over the
GraphQL contract.

- **In scope:**
  - `harness_engineering_engine` — GraphQL surface (queries + mutations), config loader, handlers (frontmatter, checksums, skill reader/refresh/registration/deployment, command executor, **CLI package manager**)
  - Persistence backends — DynamoDB (`hsk-skills` + `hsk-cli-packages` tables) **and/or** PostgreSQL (`hsk_skills` + `hsk_cli_packages` + RLS), selected via `db_backend`
  - S3 versioned skill artifacts (`HSK_SKILL_ARTIFACT_BUCKET`)
  - Local skill runtime cache under `HSK_SKILL_ROOT`
  - **CLI package management** — registration, GitHub-based installation, version verification, deployment locks, upgrade flow, audit logging (v1.1)
  - `mcp_skill_provider` — MCP tools (`search_skills`, `get_skill`, `run_command`) as hosted by `mcp_daemon_engine`
- **Out of scope:**
  - `mcp_daemon_engine` internals (auth, SSE transport) — assumed certified by its own test cycle
  - Production deployment of the engine itself
- **System(s) under test:** `harness_engineering_engine` (primary), plus its dependency path `mcp_daemon_engine → mcp_skill_provider → harness_engineering_engine GraphQL`

## 3. Environment and Access

| Item | Value / source |
|---|---|
| Environment target | dev — local Windows machine |
| Base URLs / endpoints | Engine GraphQL: **in-process** via `Schema.execute()` against the local dev stack (no gateway/HTTP round-trip for integration tests); MCP daemon: local in-process / stdio |
| Credential source | local AWS credentials file (`~/.aws/credentials`) — names only, no inline secrets in this SOP |
| Required env vars | `HSK_SKILL_ROOT`, `HSK_SKILL_ARTIFACT_BUCKET`, `HSK_SKILL_ARTIFACT_PREFIX`, `HSK_RUN_COMMAND_ENABLED`, `HSK_ALLOW_UNREGISTERED_CHANGES`, `REGION_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `DB_BACKEND`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_SCHEMA`, `PG_TABLE_PREFIX`, `initialize_tables` — all provided via `harness_engineering_engine/tests/.env` (gitignored; populated from `../silvaengine_gateway/tests/.env`) |
| Data stores | **PostgreSQL** (`db_backend=postgresql`, local `silvaengine` DB, `hsk_skills` + `hsk_cli_packages` tables, RLS) — DynamoDB path not certified in this cycle (marked `assumed`) |
| Messaging / events | none identified in v1 |
| Access constraints | local machine; local AWS dev credentials; local PostgreSQL on localhost:5432 |
| Provisioning policy | **verified on 2026-08-27:** S3 bucket `hsk-skill-artifacts-dev` exists and is accessible (versioning not enabled — see §9 note); PostgreSQL `silvaengine@localhost:5432/silvaengine` reachable; `hsk_skills` + `hsk_cli_packages` tables provisioned via `initialize_tables`. Auto-provision allowed for: local temp dirs, local skill cache, DB tables via migration. Manual approval required for: S3 bucket changes, package installs. |

> Names and sources only — no secrets, tokens, or connection strings in this document.
> **Environment validation evidence:**
> - `s3.head_bucket('hsk-skill-artifacts-dev')` → OK
> - `s3.get_bucket_versioning` → not enabled (SOP note: `s3_version_id` assertions must tolerate `None`)
> - `psycopg2.connect(host=localhost:5432, db=silvaengine)` → OK
> - `information_schema.tables LIKE 'hsk%'` → `['hsk_skills', 'hsk_cli_packages']` (provisioned)

## 4. Dependency Readiness Requirements

Each dependency must reach: `available → configured → initialized → operational`.

| Dependency | Type | Health check | Required readiness | Owner |
|---|---|---|---|---|
| PostgreSQL (`silvaengine` DB, `hsk_skills` + `hsk_cli_packages` tables, RLS) | infrastructure | connection OK (verified); `initialize_tables` creates/verifies both tables | initialized | this project |
| S3 artifact bucket (`hsk-skill-artifacts-dev`) | infrastructure | `head_bucket` + put/get of a small object in test prefix (bucket verified 2026-08-26) | operational | SilvaEngine |
| `silvaengine_dynamodb_base` / `silvaengine_utility` | internal | package import + `Config.initialize` succeeds | operational | SilvaEngine |
| `mcp_daemon_engine` | internal | module loads; `MCPSkillProvider` registered via `loadMcpConfiguration`; local stdio transport | operational | SilvaEngine |
| `mcp_skill_provider` | internal | `MCPSkillProvider` instantiates; `MCP_CONFIGURATION` valid | operational | sibling project |
| `harness_engineering_engine` GraphQL | internal | `ping` query returns via `Schema.execute` | operational | this project |
| `pip` / `importlib.metadata` (CLI package install + verify) | internal | `pip --version` returns; `importlib.metadata` importable | operational | Python stdlib |
| Git/GitHub access (deploy-from-URL path) | external | zip-based parity tests only in this cycle — GitHub deploy deferred to pre-release | operational | deferred |

## 5. Test Data Requirements

| Asset type | Count | Notes / constraints |
|---|---|---|
| Skill packages (ZIP) | 3, stored under `harness_engineering_engine/tests/` (or generated at runtime) | synthetic: (a) minimal valid skill, (b) multi-file skill with `scripts/`, (c) skill with `allowed_commands` — **deployment tests use these ZIP fixtures; no GitHub download in this cycle** |
| Skill versions per name | 2 | distinct bodies + checksums, to exercise promote/rollback |
| `allowed_commands` fixtures | 2 entries | e.g. `["python", "scripts/build_quote.py", "--spec", "*.json"]` with `timeout_seconds`/`output_limit_bytes` |
| `cli_packages` fixtures | 3 | (a) matching version (no install), (b) missing version (install), (c) outdated version (upgrade) |
| CLI package registration fixtures | 3 | `package_name`, `github_repository_url`, `version`, `git_ref` — mocked pip calls |
| Tenant (partition_key) fixtures | 2 | `endpoint#part` pairs to verify cross-tenant isolation + PostgreSQL RLS |
| `.hsk-skill.json` fixtures | matching | both matching and deliberately stale checksums |
| Operator identity | 1 | `updated_by` actor used across mutations |

- **Load order:** config bootstrap (`.env`) → `initialize_tables` (`hsk_skills` + `hsk_cli_packages` + RLS) → S3 test prefix → skill registration rows → local skill cache → CLI package registration → MCP module registration.
- **Data source:** generated synthetic ZIP fixtures under the tests folder (no production data).

## 6. Execution Order

Dependency-driven order derived from this project's actual dependency graph (engine → storage → CLI packages → MCP layer), not the generic commerce default:

```text
Foundation (config, auth context)
  -> Schema/Storage (PostgreSQL tables, S3 bucket)
  -> Skill Registration & Index
  -> Deployment (ZIP -> S3 -> registration)
  -> Local Refresh & Retrieval (on-demand cache)
  -> Lifecycle Management (promote, rollback, disable, prune)
  -> CLI Package Management (register, install, verify, upgrade, audit)
  -> Guarded Command Execution (allowlist, policy, CLI package ensure)
  -> MCP Integration (search_skills -> get_skill -> run_command via mcp_daemon_engine)
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
| **Validation points** | registration rows exist; content_checksums match recompute; duplicate names rejected |
| **Cross-system checks** | DB rows == folders on disk |
| **Status** | ✅ passing |

### INT-002 — Deploy ZIP package → S3 → registration

| Field | Value |
|---|---|
| **ID** | INT-002 |
| **Name** | `deploySkillPackage` from ZIP |
| **Priority** | P1 |
| **Type** | end-to-end |
| **CI trigger** | nightly |
| **Preconditions** | S3 bucket reachable; engine config complete |
| **Dependencies** | engine, S3, DB |
| **Test data** | generated ZIP with valid SKILL.md |
| **Steps** | 1. Generate ZIP. 2. `deploySkillPackage(source=zip)`. 3. Inspect S3 key + DB row. |
| **Expected behavior** | Artifact stored at `{prefix}/{name}/{version}/{checksum}.zip`; row has checksums, status=uploaded/registered; first-ever version is auto-activated |
| **Validation points** | artifact_checksum == sha256 of ZIP; s3_version_id captured when versioning on |
| **Cross-system checks** | S3 object exists; DB row points at it |
| **Status** | ✅ passing |

### INT-003 — Deploy from GitHub URL

| Field | Value |
|---|---|
| **ID** | INT-003 |
| **Name** | `deploySkillPackage` from GitHub |
| **Priority** | P3 — **deferred to pre-release / manual run**; this cycle certifies the ZIP deploy path only (INT-002) |
| **Type** | end-to-end |
| **CI trigger** | pre-release |
| **Preconditions** | network access to GitHub; test repo available |
| **Dependencies** | engine, GitHub, S3, DB |
| **Test data** | test GitHub repo URL + ref (configured at run time) |
| **Steps** | 1. `deploySkillPackage(source=github url, git_ref=...)`. 2. Verify normalization, checksums, registration. |
| **Expected behavior** | Repo at ref packaged identically to ZIP path |
| **Validation points** | deterministic content_checksum; identical layout between ZIP and GitHub deploy |
| **Cross-system checks** | DB row has source_type=github + source_ref |
| **Status** | ⏭ deferred |

### INT-004 — On-demand refresh on retrieval (stale/missing cache)

| Field | Value |
|---|---|
| **ID** | INT-004 |
| **Name** | `skill(name)` refreshes from S3 when local metadata is stale |
| **Priority** | P1 |
| **Type** | end-to-end |
| **CI trigger** | on pull request |
| **Preconditions** | INT-002 complete for at least one version |
| **Dependencies** | engine, S3, local cache |
| **Test data** | deployed version; locally corrupted or missing `.hsk-skill.json` |
| **Steps** | 1. Corrupt/delete local metadata. 2. Call `skill(name)`. 3. Inspect cache dir. |
| **Expected behavior** | artifact downloaded, unpacked, validated, atomically replaced, metadata rewritten; correct body returned |
| **Validation points** | local_content_checksum == registered checksum; `.hsk-skill.json` rewritten; previous valid version preserved if new one fails validation |
| **Cross-system checks** | returned body == artifact contents |
| **Status** | ✅ passing |

### INT-005 — Promote / rollback lifecycle

| Field | Value |
|---|---|
| **ID** | INT-005 |
| **Name** | `promoteSkillVersion` and `rollbackSkill` switch active version |
| **Priority** | P1 |
| **Type** | end-to-end |
| **CI trigger** | nightly |
| **Preconditions** | ≥2 versions of one skill registered |
| **Dependencies** | engine, DB, S3, local cache |
| **Test data** | two distinct versions with different bodies |
| **Steps** | 1. Activate v2 (promote). 2. Call `skill(name)` → expect v2 content. 3. Roll back to v1. 4. Call `skill(name)` → expect v1 content after refresh. |
| **Expected behavior** | Rollback is a management-state change only; next retrieval refreshes local cache |
| **Validation points** | `is_active` flags; only one active version per name; local dir matches active version after refresh |
| **Cross-system checks** | DB active row == local cache == S3 artifact |
| **Status** | ✅ passing |

### INT-006 — Agent-facing flow through MCP daemon

| Field | Value |
|---|---|
| **ID** | INT-006 |
| **Name** | `search_skills → get_skill → run_command` via `mcp_daemon_engine` |
| **Priority** | P1 |
| **Type** | end-to-end (local MCP daemon, stdio/in-process transport) |
| **CI trigger** | pre-release |
| **Preconditions** | `mcp_daemon_engine` running locally with `mcp_skill_provider` registered via `loadMcpConfiguration`; provider's `graphql_modules.harness_engineering` points at the in-process engine dispatch; `HSK_RUN_COMMAND_ENABLED=true` in tests/.env |
| **Dependencies** | mcp_daemon_engine, mcp_skill_provider, harness_engineering_engine |
| **Test data** | skill with an allowlisted trivial command (from INT-002 fixtures) |
| **Steps** | 1. `search_skills(query)`. 2. `get_skill(name)`. 3. `run_command(name, argv)` matching the allowlist. |
| **Expected behavior** | Ranked results; full skill body + `stale_index`; command executes only when allowlisted |
| **Validation points** | MCP tool result shapes; tenant identity inherited from daemon context; full catalog never exposed to agent |
| **Cross-system checks** | MCP results match direct GraphQL results |
| **Status** | ⏭ deferred |

### INT-007 — Guarded command executor policy (security)

| Field | Value |
|---|---|
| **ID** | INT-007 |
| **Name** | `run_command` allowlist + path traversal + shell rejection |
| **Priority** | P1 |
| **Type** | API / security |
| **CI trigger** | on pull request (blocks merge) |
| **Preconditions** | INT-002 deployed skill with structured allowlist |
| **Dependencies** | engine, command executor |
| **Test data** | benign allowlisted command; hostile inputs |
| **Steps** | 1. Run allowlisted argv → expect success. 2. Run non-allowlisted argv → expect rejection. 3. Inject shell metacharacters (`|`, `;`, `$(...)`) → expect rejection. 4. Attempt path traversal (`../../`) → expect rejection. 5. Exceed timeout → expect timeout=true. 6. Exceed output cap → expect truncated=true. 7. With `HSK_RUN_COMMAND_ENABLED=false` → all calls denied. |
| **Expected behavior** | Deny-by-default; structured argv only; no shell; constrained paths; timeout and output caps enforced |
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
| **Steps** | 1. `insertUpdateCliPackage` with `package_name`, `github_repository_url`, `version`, `git_ref`. 2. Re-register to update version → verify `operation=updated`. 3. `ensureCliPackage` with matching installed version → `status=ready` (no install). 4. `ensureCliPackage` with missing installed version → triggers pip install → `status=ready`. 5. `ensureCliPackage` with outdated installed version → triggers uninstall + reinstall → `status=ready`. 6. `ensureCliPackage` with install failure → `status=error`. 7. `ensureCliPackage` with verification failure → `status=error`. |
| **Expected behavior** | Registration upserts by name; ensure_package compares installed vs registered version; missing/outdated triggers pip install from `git+url@ref`; verification via `importlib.metadata.version()`; process-level lock prevents races; audit log emitted for every operation |
| **Validation points** | registration row exists; `operation` field correct; lock acquired/released; audit log entries; pip install/uninstall called with correct args (`shell=False`) |
| **Cross-system checks** | DB registration row == ensure_package input; installed version == registered version after successful install |
| **Status** | ✅ passing |

### INT-009 — CLI package ensure before run_command (v1.1)

| Field | Value |
|---|---|
| **ID** | INT-009 |
| **Name** | `run_command` calls `ensure_package` when skill declares `cli_packages` |
| **Priority** | P2 |
| **Type** | integration (command executor → CLI package manager) |
| **CI trigger** | nightly |
| **Preconditions** | INT-002 deployed skill with `cli_packages` in frontmatter; package registered via INT-008 |
| **Dependencies** | engine, command executor, CLI package manager |
| **Test data** | skill with `cli_packages` entry; mocked `ensure_package` returning ready/error |
| **Steps** | 1. Deploy skill with `cli_packages` frontmatter. 2. `run_command` with allowlisted argv. 3. Verify `ensure_package` was called for each `cli_packages` entry. 4. Mock `ensure_package` returning error → `run_command` blocked. |
| **Expected behavior** | CLI packages ensured before command execution; failure blocks command |
| **Validation points** | `ensure_package` called with correct `package_name`; command blocked on package error |
| **Cross-system checks** | command executor → cli_package_manager → pip (mocked) |
| **Status** | ✅ passing (covered by unit tests in `test_command_executor.py` + `test_cli_package.py`) |

## 8. Failure and Resilience Scenarios

| Scenario | Injected fault | Expected behavior | Status |
|---|---|---|---|
| missing_data | `skill(name)` for unregistered name | clear not-found error (returns null) | ✅ passing |
| missing_data | `skill(name)` with local cache deleted mid-run | refresh from S3 succeeds or previous valid version preserved | ✅ passing |
| invalid_data | SKILL.md missing frontmatter / missing name / missing description | registration + deployment reject with validation error | ✅ passing |
| invalid_data | `allowed_commands` as shell string instead of argv list | rejected at validation | ✅ passing |
| api_failures | S3 download fails mid-refresh | local dir untouched; previous valid version remains active | ✅ passing |
| database_failures | DB unavailable during `searchSkills` | graceful GraphQL error, no partial data | ✅ passing |
| checksum_mismatch | local content modified without re-registration while `HSK_ALLOW_UNREGISTERED_CHANGES=false` | read rejected / `stale_index=true` surfaced | ✅ passing |
| auth/tenant | request with a different partition_key | cross-tenant rows invisible (RLS) | ✅ passing |
| cli_package_missing | `ensureCliPackage` for unregistered package | `status=error`, command blocked | ✅ passing |
| cli_package_install_fail | pip install returns non-zero exit code | `status=error`, audit log recorded, command blocked | ✅ passing |
| cli_package_verify_fail | installed version != registered version after install | `status=error`, audit log recorded, command blocked | ✅ passing |
| kill_switch | `HSK_RUN_COMMAND_ENABLED=false` | all `run_command` calls denied | ✅ passing |

## 9. Data Reconciliation Checks

| Check | Rule | Tolerance | Status |
|---|---|---|---|
| Artifact integrity | `sha256(zip artifact) == artifact_checksum` in DB | 0 | ✅ passing |
| Content integrity | recomputed content checksum == `content_checksum` in DB | 0 | ✅ passing |
| Local consistency | local `.hsk-skill.json` version == active registered version | 0 | ✅ passing |
| Count consistency | deployed artifacts for name == registered rows for name | 0 | ✅ passing |
| Single active version | exactly one `is_active=true` enabled row per (partition, name) | 0 | ✅ passing |
| CLI package version | installed version == registered version after `ensure_package` | 0 | ✅ passing |
| Audit completeness | every `run_command` execution logged with tenant, argv, exit code | 0 missing | ✅ passing |
| CLI package audit | every install/upgrade/verify operation logged with package, version, status | 0 missing | ✅ passing |
| Rollback purity | `rollbackSkill` mutates no S3 artifacts and no local dirs directly | 0 writes outside DB | ✅ passing |

## 10. Entry and Exit Criteria

**Entry criteria (testing may begin when):**
- This SOP is confirmed by the user.
- Target environment chosen and reachable (PostgreSQL + dev S3 bucket).
- `HSK_*` environment variables configured.
- All P1 dependencies reach `operational` readiness.
- Synthetic test data generated and present.
- `hsk_skills` + `hsk_cli_packages` tables provisioned.

**Exit criteria (certification may be issued when):**
- All P1 scenarios (INT-001, INT-002, INT-004, INT-005, INT-007, INT-008) pass.
- Coverage ≥ 80% of GraphQL operations on the in-scope surface.
- No blocking or critical defects open.
- Data reconciliation checks (Section 9) clean.
- Failure/resilience scenarios (Section 8) all produce expected behavior.

## 11. CI Trigger and Cadence

| Trigger | Scope run | Required to pass |
|---|---|---|
| On pull request | Existing unit suite (56 tests) + INT-001 + INT-004 + INT-007 + INT-008 | yes — blocks merge |
| Nightly | + INT-002 + INT-005 + INT-009 | report only |
| Pre-release | full suite incl. INT-003 + INT-006 + all failure & reconciliation checks | yes — blocks release |

## 12. Reporting and Certification Expectations

- **Report format:** markdown
- **Required certification decision:** one of `Integration Certified`, `Ready for UAT`, `Ready for Production`, `Ready with Conditions`, `Not Ready`
- **Distribution:** repo owner; reports written to `docs/test_results/`
- Report location: `docs/test_results/integration_certification_report_20260827.md` (latest)

## 13. Sign-off

| Role | Name | Date | Decision |
|---|---|---|---|
| Test owner | bibo7 | 2026-08-27 | Ready with Conditions (PostgreSQL path certified; INT-003, INT-006, DynamoDB deferred) |

---

## Resolved items

1. ~~Owner/contact~~ — resolved: bibo7 (repo owner).
2. ~~Target environment~~ — resolved: dev (local machine, local AWS profile, dev S3 bucket, local PostgreSQL). Verified 2026-08-26.
3. ~~Backend(s) to certify~~ — resolved: **PostgreSQL** (per local dev stack; DynamoDB path deferred, marked assumed).
4. ~~S3 bucket~~ — resolved: `hsk-skill-artifacts-dev` (exists; versioning **not** enabled — SOP notes that `s3_version_id` may be `None` and assertions must tolerate that).
5. ~~GitHub deploy (INT-003)~~ — deferred to pre-release/manual; this cycle certifies the ZIP deploy path (INT-002) with local ZIP fixtures under `harness_engineering_engine/tests/`.
6. ~~MCP daemon (INT-006)~~ — local in-process `mcp_daemon_engine` with `mcp_skill_provider` registered.
7. ~~Report distribution~~ — resolved: repo owner only; reports written to `docs/test_results/`.

## v1.1 additions (2026-08-27)

- **INT-008** — CLI package registration and management (register, install, verify, upgrade, audit)
- **INT-009** — CLI package ensure before `run_command` (command executor wiring)
- CLI package manager: `register_cli_package`, `ensure_package` with pip install/uninstall, version verification via `importlib.metadata.version()`, process-level deployment locks, audit logging
- New model: `CliPackageModel` (DynamoDB + PostgreSQL) with RLS
- New GraphQL surface: `cliPackages` query, `cliPackage` query, `insertUpdateCliPackage` / `deleteCliPackage` / `ensureCliPackage` mutations
- New migration: `0002_create_cli_packages.py`
- New test file: `test_cli_package.py` (8 tests)
- Test count: 56 (33 unit + 7 integration + 8 resilience + 8 CLI package)