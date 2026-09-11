#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Integration tests for the command_run repository (§18 G-6).

Exercises the dual-backend ``command_run`` entity end-to-end against the
real test database configured in ``tests/.env`` — the same pattern used by
``test_cli_package.py``. Covers:

  - insert_update creates a row on first write (launch)
  - insert_update merges fields into an existing row (progress/final update)
  - get returns the normalized row
  - list(completed_before=...) finds stale completed rows for cleanup
  - delete removes a row
  - scheduler.tick_prune_command_runs prunes only rows past retention
"""
from __future__ import print_function

__author__ = "bibow"

import json
import logging
import uuid
from pathlib import Path

import pendulum
import pytest
from sqlalchemy import text

from harness_engineering_engine.handlers.config import Config
from harness_engineering_engine.models.repositories import get_repo

from . import conftest as _conftest  # noqa: F401


class FakeInfo:
    def __init__(self, partition_key="hsk-itest#p1"):
        self.context = {
            "logger": logging.getLogger(),
            "partition_key": partition_key,
            "endpoint_id": "hsk-itest",
            "part_id": "p1",
        }


@pytest.fixture(scope="function")
def itest():
    Config._initialized = False
    Config.DB_BACKEND = "dynamodb"
    Config.initialize(logging.getLogger(), _conftest.build_setting_from_env())
    with Config.db_session() as s:
        s.execute(text("DELETE FROM hsk_command_runs WHERE partition_key LIKE 'hsk-itest%'"))
        s.commit()
    yield {"endpoint_id": "hsk-itest", "part_id": "p1"}
    Config.db_session.remove()


class TestCommandRunRepository:
    def test_insert_update_creates_row(self, itest):
        run_id = str(uuid.uuid4())
        repo = get_repo("command_run")

        row = repo.insert_update(
            FakeInfo(),
            run_uuid=run_id,
            skill_name="demand-forecasting",
            argv=json.dumps(["python", "run.py"]),
            status="running",
            started_at=pendulum.now("UTC"),
            updated_by="itest",
        )

        assert row["run_uuid"] == run_id
        assert row["skill_name"] == "demand-forecasting"
        assert row["status"] == "running"

    def test_get_returns_normalized_row(self, itest):
        run_id = str(uuid.uuid4())
        repo = get_repo("command_run")
        repo.insert_update(
            FakeInfo(),
            run_uuid=run_id,
            skill_name="demand-forecasting",
            argv=json.dumps(["python", "run.py"]),
            status="running",
            started_at=pendulum.now("UTC"),
            updated_by="itest",
        )

        fetched = repo.get(partition_key="hsk-itest#p1", run_uuid=run_id)
        assert fetched is not None
        assert fetched["run_uuid"] == run_id
        assert fetched["status"] == "running"

    def test_update_merges_progress_then_final_status(self, itest):
        run_id = str(uuid.uuid4())
        repo = get_repo("command_run")
        repo.insert_update(
            FakeInfo(),
            run_uuid=run_id,
            skill_name="demand-forecasting",
            argv=json.dumps(["python", "run.py"]),
            status="running",
            started_at=pendulum.now("UTC"),
            updated_by="itest",
        )

        repo.insert_update(
            FakeInfo(),
            run_uuid=run_id,
            stdout="partial output so far",
            updated_by="itest",
        )
        mid = repo.get(partition_key="hsk-itest#p1", run_uuid=run_id)
        assert mid["stdout"] == "partial output so far"
        assert mid["status"] == "running"  # untouched by the progress-only write

        repo.insert_update(
            FakeInfo(),
            run_uuid=run_id,
            status="completed",
            exit_code="0",
            stdout="final output",
            completed_at=pendulum.now("UTC"),
            updated_by="itest",
        )
        final = repo.get(partition_key="hsk-itest#p1", run_uuid=run_id)
        assert final["status"] == "completed"
        assert final["exit_code"] == "0"
        assert final["stdout"] == "final output"

    def test_list_finds_stale_completed_rows(self, itest):
        run_id = str(uuid.uuid4())
        repo = get_repo("command_run")
        old_completed_at = pendulum.now("UTC").subtract(hours=2)
        repo.insert_update(
            FakeInfo(),
            run_uuid=run_id,
            skill_name="demand-forecasting",
            argv=json.dumps(["python", "run.py"]),
            status="completed",
            exit_code="0",
            started_at=old_completed_at,
            completed_at=old_completed_at,
            updated_by="itest",
        )

        cutoff = pendulum.now("UTC").subtract(hours=1)
        stale = repo.list(FakeInfo(), completed_before=cutoff, limit=500)
        stale_ids = {row["run_uuid"] for row in stale}
        assert run_id in stale_ids

    def test_delete_removes_row(self, itest):
        run_id = str(uuid.uuid4())
        repo = get_repo("command_run")
        repo.insert_update(
            FakeInfo(),
            run_uuid=run_id,
            skill_name="demand-forecasting",
            argv=json.dumps(["python", "run.py"]),
            status="running",
            started_at=pendulum.now("UTC"),
            updated_by="itest",
        )

        assert repo.delete(FakeInfo(), partition_key="hsk-itest#p1", run_uuid=run_id) is True
        assert repo.get(partition_key="hsk-itest#p1", run_uuid=run_id) is None


class TestPruneCommandRunsScheduler:
    def test_tick_prunes_only_stale_rows(self, itest):
        from harness_engineering_engine import scheduler as _scheduler_mod

        repo = get_repo("command_run")

        stale_id = str(uuid.uuid4())
        fresh_id = str(uuid.uuid4())
        old_completed_at = pendulum.now("UTC").subtract(hours=2)
        recent_completed_at = pendulum.now("UTC")

        repo.insert_update(
            FakeInfo(),
            run_uuid=stale_id,
            skill_name="demand-forecasting",
            argv=json.dumps(["python", "run.py"]),
            status="completed",
            exit_code="0",
            started_at=old_completed_at,
            completed_at=old_completed_at,
            updated_by="itest",
        )
        repo.insert_update(
            FakeInfo(),
            run_uuid=fresh_id,
            skill_name="demand-forecasting",
            argv=json.dumps(["python", "run.py"]),
            status="completed",
            exit_code="0",
            started_at=recent_completed_at,
            completed_at=recent_completed_at,
            updated_by="itest",
        )

        setting = Config.get_setting()
        setting["partition_key"] = "hsk-itest#p1"
        setting["hsk_scheduler_command_run_retention_seconds"] = "3600"

        _scheduler_mod.tick_prune_command_runs()

        assert repo.get(partition_key="hsk-itest#p1", run_uuid=stale_id) is None
        assert repo.get(partition_key="hsk-itest#p1", run_uuid=fresh_id) is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
