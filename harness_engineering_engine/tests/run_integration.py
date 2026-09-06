#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Standalone runner for MCP×Harness integration tests with per-call logging.

Executes all INT-001..INT-012 scenarios, captures every GraphQL call's
query/variables/response to a JSON log, and prints a summary.
"""
from __future__ import print_function

__author__ = "bibow"

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env
from dotenv import load_dotenv
env_path = PROJECT_ROOT / "harness_engineering_engine" / "tests" / ".env"
load_dotenv(env_path, override=False)

from graphene import Schema
from harness_engineering_engine.handlers.config import Config
from harness_engineering_engine.schema import Mutations, Query, type_class

# Initialize config
setting = dict(os.environ)
for lower, upper in [
    ("db_backend", "DB_BACKEND"), ("db_host", "DB_HOST"), ("db_port", "DB_PORT"),
    ("db_user", "DB_USER"), ("db_password", "DB_PASSWORD"), ("db_schema", "DB_SCHEMA"),
    ("region_name", "REGION_NAME"),
    ("aws_access_key_id", "AWS_ACCESS_KEY_ID"),
    ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY"),
]:
    if lower not in setting and upper in setting:
        setting[lower] = setting[upper]
Config._initialized = False
Config.initialize(logging.getLogger(), setting)

CALL_LOG = []
RESULTS = []


def log_call(scenario, query, variables, result, elapsed_ms, passed=True, error=None):
    entry = {
        "scenario": scenario,
        "query": query.strip()[:500],
        "variables": variables,
        "result": result,
        "elapsed_ms": round(elapsed_ms, 1),
        "passed": passed,
        "error": error,
    }
    CALL_LOG.append(entry)


def run_git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def make_git_skill_repo(root, name, body, allowed_commands=None,
                        cli_packages=None, references=None):
    remote = root / f"{name}-remote"
    remote.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q"], cwd=remote)
    run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote)
    commit_skill_version(remote, name, body, allowed_commands, cli_packages, references)
    return remote


def commit_skill_version(remote, name, body, allowed_commands=None,
                          cli_packages=None, references=None):
    commands_yaml = "allowed_commands: []"
    if allowed_commands:
        commands_yaml = "allowed_commands:\n" + "\n".join(
            f"  - argv: {json.dumps(ac)}" for ac in allowed_commands
        )
    packages_yaml = ""
    if cli_packages:
        packages_yaml = "cli_packages:\n" + "\n".join(
            f"  - name: {pkg}" for pkg in cli_packages
        )
    refs_yaml = ""
    if references:
        refs_yaml = "references:\n" + "\n".join(f"  - {ref}" for ref in references)

    skill_dir = remote / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill {name}.\n"
        f"{commands_yaml}\n{packages_yaml}\n{refs_yaml}\n---\n\n{body}\n"
    )
    run_git(["add", "-A"], cwd=remote)
    run_git(["-c", "user.email=test@example.com", "-c", "user.name=test",
             "commit", "-q", "-m", f"update {name}"], cwd=remote)


def gql(query, endpoint_id, part_id, variables=None):
    schema = Schema(query=Query, mutation=Mutations, types=type_class())
    context = {
        "logger": logging.getLogger(),
        "partition_key": f"{endpoint_id}#{part_id}",
        "endpoint_id": endpoint_id,
        "part_id": part_id,
    }
    if Config.DB_BACKEND == "postgresql" and Config.db_session:
        from sqlalchemy import text
        Config.db_session.execute(
            text("SET app.tenant_id = :tenant"),
            {"tenant": context["partition_key"]},
        )
    t0 = time.perf_counter()
    result = schema.execute(query, variable_values=variables, context_value=context)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if result.errors:
        return None, str(result.errors), elapsed_ms
    return result.data, None, elapsed_ms


def deploy_skill(remote, name, endpoint_id, part_id, git_ref="main"):
    query = """
        mutation DeploySkill($gitRepositoryUrl: String!, $skillName: String, $gitRef: String) {
            deploySkillPackage(gitRepositoryUrl: $gitRepositoryUrl, skillName: $skillName, gitRef: $gitRef) {
                ok deployed failed skipped
            }
        }
    """
    data, err, ms = gql(query, endpoint_id, part_id, {
        "gitRepositoryUrl": str(remote), "skillName": name, "gitRef": git_ref
    })
    return data, err, ms


def fresh_state():
    """Create fresh isolated test state."""
    Config._initialized = False
    Config.DB_BACKEND = "dynamodb"
    Config.initialize(logging.getLogger(), setting)
    tmp = Path(tempfile.mkdtemp(prefix="hsk_runner_"))
    Config.SKILL_ROOT = str(tmp)
    from sqlalchemy import text
    with Config.db_session() as s:
        s.execute(text("DELETE FROM hsk_skills WHERE partition_key LIKE 'hsk-itest%'"))
        s.commit()
    return tmp, "hsk-itest", "p1"


def cleanup(tmp):
    shutil.rmtree(tmp, ignore_errors=True)
    Config.db_session.remove()


def run_scenario(scenario_id, scenario_name, fn):
    """Run a scenario with fresh state and capture results."""
    tmp, eid, pid = fresh_state()
    state = {"tmp": tmp, "endpoint_id": eid, "part_id": pid}
    t0 = time.perf_counter()
    try:
        fn(state, eid, pid)
        elapsed = time.perf_counter() - t0
        RESULTS.append({"id": scenario_id, "name": scenario_name, "status": "PASS", "elapsed_s": round(elapsed, 2)})
        print(f"  {scenario_id}: PASS — {scenario_name} ({elapsed:.1f}s)")
    except Exception as e:
        elapsed = time.perf_counter() - t0
        RESULTS.append({"id": scenario_id, "name": scenario_name, "status": "FAIL", "elapsed_s": round(elapsed, 2), "error": str(e)[:300]})
        print(f"  {scenario_id}: FAIL — {scenario_name} ({elapsed:.1f}s) — {str(e)[:200]}")
    finally:
        cleanup(tmp)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def int_001(state, eid, pid):
    tmp = state["tmp"]
    for name, body in [("python-linter", "Lint Python code"), ("python-formatter", "Format Python code"), ("video-renderer", "Render videos")]:
        remote = make_git_skill_repo(tmp, name, body)
        data, err, ms = deploy_skill(remote, name, eid, pid)
        assert err is None, f"deploy failed: {err}"
    data, err, ms = gql('query Search($query: String!) { searchSkills(query: $query) { total skillList { name description } } }', eid, pid, {"query": "python"})
    log_call("INT-001", 'query searchSkills', {"query": "python"}, data, ms)
    assert err is None, err
    names = [s["name"] for s in data["searchSkills"]["skillList"]]
    assert "python-linter" in names, f"python-linter not in {names}"
    assert "python-formatter" in names, f"python-formatter not in {names}"
    python_idx = [i for i, s in enumerate(data["searchSkills"]["skillList"]) if "python" in s["name"]]
    video_idx = [i for i, s in enumerate(data["searchSkills"]["skillList"]) if "video" in s["name"]]
    if python_idx and video_idx:
        assert min(python_idx) < min(video_idx), f"python should rank above video: {names}"


def int_002(state, eid, pid):
    tmp = state["tmp"]
    body = "This is the SKILL.md body."
    remote = make_git_skill_repo(tmp, "test-skill", body, allowed_commands=[["python", "--version"]], cli_packages=["python"])
    data, err, ms = deploy_skill(remote, "test-skill", eid, pid)
    assert err is None, err
    data, err, ms = gql('query GetSkill($name: String!) { skill(name: $name) { name body allowedCommands cliPackages status } }', eid, pid, {"name": "test-skill"})
    log_call("INT-002", 'query skill', {"name": "test-skill"}, data, ms)
    assert err is None, err
    assert data["skill"] is not None
    assert "SKILL.md body" in data["skill"]["body"]
    assert data["skill"]["allowedCommands"] is not None
    assert data["skill"]["cliPackages"] is not None
    assert data["skill"].get("status") != "refreshing"


def int_003(state, eid, pid):
    tmp = state["tmp"]
    remote = make_git_skill_repo(tmp, "refresh-skill", "Original body v1.")
    data, err, ms = deploy_skill(remote, "refresh-skill", eid, pid)
    assert err is None, err
    meta_file = tmp / "refresh-skill" / ".hsk-skill.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text())
        meta["local_content_checksum"] = "stale_checksum"
        meta_file.write_text(json.dumps(meta))
    data1, err, ms = gql('query GetSkill($name: String!) { skill(name: $name) { name body status } }', eid, pid, {"name": "refresh-skill"})
    log_call("INT-003-call1", 'query skill (stale)', {"name": "refresh-skill"}, data1, ms)
    assert err is None, err
    assert data1["skill"] is not None
    has_body = data1["skill"].get("body") is not None
    has_status = data1["skill"].get("status") == "refreshing"
    assert has_body or has_status, f"Expected body or refreshing, got: {data1['skill']}"
    time.sleep(3)
    data2, err, ms = gql('query GetSkill($name: String!) { skill(name: $name) { name body status } }', eid, pid, {"name": "refresh-skill"})
    log_call("INT-003-call2", 'query skill (after refresh)', {"name": "refresh-skill"}, data2, ms)
    assert err is None, err
    assert data2["skill"].get("body") is not None
    assert data2["skill"].get("status") != "refreshing"


def int_004(state, eid, pid):
    tmp = state["tmp"]
    remote = make_git_skill_repo(tmp, "echo-skill", "Python version skill.", allowed_commands=[["python", "--version"]])
    data, err, ms = deploy_skill(remote, "echo-skill", eid, pid)
    assert err is None, err
    data, err, ms = gql('mutation RunCmd($name: String!, $argv: [String!]!) { runCommand(name: $name, argv: $argv) { stdout stderr exitCode truncated outputTruncatedBytes } }', eid, pid, {"name": "echo-skill", "argv": ["python", "--version"]})
    log_call("INT-004", 'mutation runCommand (sync)', {"name": "echo-skill", "argv": ["python", "--version"]}, data, ms)
    assert err is None, err
    combined = (data["runCommand"]["stdout"] or "") + (data["runCommand"]["stderr"] or "")
    assert "Python" in combined, f"expected Python: {combined}"
    assert data["runCommand"]["exitCode"] == "0"
    assert data["runCommand"]["truncated"] is False
    assert data["runCommand"]["outputTruncatedBytes"] == 0


def int_005(state, eid, pid):
    tmp = state["tmp"]
    remote = make_git_skill_repo(tmp, "locked-skill", "Limited allowlist.", allowed_commands=[["python", "--version"]])
    data, err, ms = deploy_skill(remote, "locked-skill", eid, pid)
    assert err is None, err
    data, err, ms = gql('mutation RunCmd($name: String!, $argv: [String!]!) { runCommand(name: $name, argv: $argv) { stdout exitCode } }', eid, pid, {"name": "locked-skill", "argv": ["python", "-c", "print('hack')"]})
    log_call("INT-005", 'mutation runCommand (rejected)', {"name": "locked-skill", "argv": ["python", "-c", "print('hack')"]}, {"error": err}, ms)
    assert err is not None, "Should have been rejected"


def int_006(state, eid, pid):
    tmp = state["tmp"]
    script = tmp / "bg_script.py"
    script.write_text("import time\ntime.sleep(2)\nprint('done')\n")
    remote = make_git_skill_repo(tmp, "bg-skill", "Background skill.", allowed_commands=[["python", str(script)]])
    data, err, ms = deploy_skill(remote, "bg-skill", eid, pid)
    assert err is None, err
    t0 = time.perf_counter()
    data, err, ms = gql('mutation RunCmd($name: String!, $argv: [String!]!, $background: Boolean) { runCommand(name: $name, argv: $argv, background: $background) { runId stdout } }', eid, pid, {"name": "bg-skill", "argv": ["python", str(script)], "background": True})
    elapsed = time.perf_counter() - t0
    log_call("INT-006", 'mutation runCommand (background)', {"name": "bg-skill", "argv": ["python", str(script)], "background": True}, data, ms)
    assert err is None, err
    assert data["runCommand"]["runId"] is not None, "runId should be returned"
    assert elapsed < 2.0, f"Should return immediately, took {elapsed:.1f}s"


def int_007(state, eid, pid):
    tmp = state["tmp"]
    script = tmp / "poll_script.py"
    script.write_text("import time\ntime.sleep(1)\nprint('done')\n")
    remote = make_git_skill_repo(tmp, "poll-skill", "Poll skill.", allowed_commands=[["python", str(script)]])
    data, err, ms = deploy_skill(remote, "poll-skill", eid, pid)
    assert err is None, err
    data, err, ms = gql('mutation RunCmd($name: String!, $argv: [String!]!, $background: Boolean) { runCommand(name: $name, argv: $argv, background: $background) { runId stdout } }', eid, pid, {"name": "poll-skill", "argv": ["python", str(script)], "background": True})
    assert err is None, err
    run_id = data["runCommand"]["runId"]
    assert run_id is not None
    data, err, ms = gql('query Poll($run_id: String!) { pollCommand(run_id: $run_id) { runId status stdout exitCode } }', eid, pid, {"run_id": run_id})
    log_call("INT-007-poll1", 'query pollCommand (running)', {"run_id": run_id}, data, ms)
    assert err is None, err
    assert data["pollCommand"]["status"] in ("running", "completed")
    time.sleep(2)
    data, err, ms = gql('query Poll($run_id: String!) { pollCommand(run_id: $run_id) { runId status stdout exitCode outputTruncatedBytes } }', eid, pid, {"run_id": run_id})
    log_call("INT-007-poll2", 'query pollCommand (completed)', {"run_id": run_id}, data, ms)
    assert err is None, err
    assert data["pollCommand"]["status"] == "completed", f"expected completed, got {data['pollCommand']['status']}"
    assert "done" in (data["pollCommand"]["stdout"] or "")
    assert data["pollCommand"]["exitCode"] == "0"


def int_008(state, eid, pid):
    data, err, ms = gql('query Poll($run_id: String!) { pollCommand(run_id: $run_id) { runId status } }', eid, pid, {"run_id": "nonexistent-uuid-12345"})
    log_call("INT-008", 'query pollCommand (unknown)', {"run_id": "nonexistent-uuid-12345"}, data, ms)
    assert err is None, err
    assert data["pollCommand"]["status"] == "not_found"


def int_009(state, eid, pid):
    tmp = state["tmp"]
    remote = make_git_skill_repo(tmp, "meta-skill", "Python skill.", allowed_commands=[["python", "--version"]])
    data, err, ms = deploy_skill(remote, "meta-skill", eid, pid)
    assert err is None, err
    data, err, ms = gql('mutation RunCmd($name: String!, $argv: [String!]!) { runCommand(name: $name, argv: $argv) { stdout } }', eid, pid, {"name": "meta-skill", "argv": ["python", "--version; rm -rf /"]})
    log_call("INT-009", 'mutation runCommand (metachar)', {"name": "meta-skill", "argv": ["python", "--version; rm -rf /"]}, {"error": err}, ms)
    assert err is not None, "Should have been rejected"


def int_010(state, eid, pid):
    tmp = state["tmp"]
    remote = make_git_skill_repo(tmp, "kill-skill", "Python skill.", allowed_commands=[["python", "--version"]])
    data, err, ms = deploy_skill(remote, "kill-skill", eid, pid)
    assert err is None, err
    original = Config.RUN_COMMAND_ENABLED
    Config.RUN_COMMAND_ENABLED = False
    try:
        data, err, ms = gql('mutation RunCmd($name: String!, $argv: [String!]!) { runCommand(name: $name, argv: $argv) { stdout } }', eid, pid, {"name": "kill-skill", "argv": ["python", "--version"]})
        log_call("INT-010", 'mutation runCommand (kill switch)', {"name": "kill-skill", "argv": ["python", "--version"]}, {"error": err}, ms)
        assert err is not None, "Should have been rejected by kill switch"
    finally:
        Config.RUN_COMMAND_ENABLED = original


def int_011(state, eid, pid):
    data, err, ms = gql('query GetSkill($name: String!) { skill(name: $name) { name body } }', eid, pid, {"name": "nonexistent-skill-xyz"})
    log_call("INT-011", 'query skill (not found)', {"name": "nonexistent-skill-xyz"}, data, ms)
    assert err is None, err
    assert data["skill"] is None, f"expected None, got: {data['skill']}"


def int_012(state, eid, pid):
    tmp = state["tmp"]
    big_script = tmp / "big_output_script.py"
    big_script.write_text("print('x' * 50000)\n")
    remote = make_git_skill_repo(tmp, "big-output-skill", "Big output skill.", allowed_commands=[["python", str(big_script)]])
    data, err, ms = deploy_skill(remote, "big-output-skill", eid, pid)
    assert err is None, err
    data, err, ms = gql('mutation RunCmd($name: String!, $argv: [String!]!) { runCommand(name: $name, argv: $argv) { stdout stderr exitCode truncated outputTruncatedBytes } }', eid, pid, {"name": "big-output-skill", "argv": ["python", str(big_script)]})
    log_call("INT-012", 'mutation runCommand (big output)', {"name": "big-output-skill", "argv": ["python", str(big_script)]}, data, ms)
    assert err is None, err
    assert data["runCommand"]["truncated"] is True, "should be truncated"
    assert data["runCommand"]["outputTruncatedBytes"] > 0
    stdout_len = len(data["runCommand"]["stdout"].encode("utf-8"))
    assert stdout_len <= Config.RUN_COMMAND_OUTPUT_LIMIT_BYTES


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("MCP Skill Provider × Harness Engineering Engine")
    print("Integration Test Runner — Per-Call Logging")
    print("=" * 70)

    scenarios = [
        ("INT-001", "Fuzzy search returns ranked skills", int_001),
        ("INT-002", "Get skill returns body + metadata", int_002),
        ("INT-003", "Stale cache triggers non-blocking refresh", int_003),
        ("INT-004", "Sync command execution", int_004),
        ("INT-005", "Command not in allowlist rejected", int_005),
        ("INT-006", "Background command returns run_id immediately", int_006),
        ("INT-007", "Poll command running → completed", int_007),
        ("INT-008", "Poll unknown run_id → not_found", int_008),
        ("INT-009", "Shell metacharacters rejected", int_009),
        ("INT-010", "Kill switch rejects commands", int_010),
        ("INT-011", "Get nonexistent skill → null", int_011),
        ("INT-012", "Output truncation volume indicator", int_012),
    ]

    for sid, sname, fn in scenarios:
        run_scenario(sid, sname, fn)

    # Summary
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    print(f"\n{'=' * 70}")
    print(f"Results: {passed} passed, {failed} failed, {len(RESULTS)} total")
    print(f"{'=' * 70}")

    # Write per-call log
    log_path = PROJECT_ROOT / "docs" / "test_results" / "call_log_integrated.jsonl"
    with open(log_path, "w") as f:
        for entry in CALL_LOG:
            f.write(json.dumps(entry) + "\n")
    print(f"Per-call log: {log_path}")

    # Write results summary
    results_path = PROJECT_ROOT / "docs" / "test_results" / "results_summary.json"
    with open(results_path, "w") as f:
        json.dump({"scenarios": RESULTS, "total_calls": len(CALL_LOG),
                    "passed": passed, "failed": failed}, f, indent=2)
    print(f"Results summary: {results_path}")

    sys.exit(0 if failed == 0 else 1)