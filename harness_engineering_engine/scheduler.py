# -*- coding: utf-8 -*-
"""Background scheduler for Harness Engineering periodic jobs.

This module starts an APScheduler BackgroundScheduler that periodically
invokes the handler tick functions:

- ``tick_prune_versions`` — disable old skill versions and reclaim local disk

Lifecycle:
- ``start_scheduler()``  — called by the gateway's on_startup hook
- ``stop_scheduler()``   — called by the gateway's on_shutdown hook

The scheduler reads tick intervals from ``Config.get_setting()`` so they
can be tuned per-tenant at deployment time.
"""
from __future__ import print_function

__author__ = "bibow"

import logging
from typing import Any, Callable, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .handlers.config import Config

logger = logging.getLogger(__name__)

# ── Module-level scheduler singleton ──────────────────────────────────────

_scheduler: Optional[BackgroundScheduler] = None

DEFAULT_PRUNE_INTERVAL = 86400  # 1 day

def _setting(key: str, default: Any = None) -> Any:
    """Read a setting from Config.get_setting(), with a fallback default."""
    s = Config.get_setting() or {}
    return s.get(key, default)


def _context() -> Dict[str, Any]:
    """Build a minimal context dict with partition_key from Config settings.

    The scheduler runs outside any HTTP request, so there is no gateway
    ``Part-Id`` header to derive the tenant from. The tenant must be
    supplied at deployment time via settings (``partition_key`` directly,
    or ``endpoint_id`` + ``part_id`` which are combined the same way the
    gateway does for request-scoped calls).
    """
    setting = Config.get_setting() or {}
    partition_key = setting.get("partition_key") or ""
    endpoint_id = setting.get("endpoint_id") or ""
    part_id = setting.get("part_id") or ""
    if not partition_key and endpoint_id and part_id:
        partition_key = f"{endpoint_id}#{part_id}"
    return {
        "partition_key": partition_key,
        "endpoint_id": endpoint_id,
        "part_id": part_id,
        "setting": setting,
        "logger": logger
    }


def _parse_partition_keys(setting: Dict[str, Any]) -> List[str]:
    """Parse the scheduler tenant list from settings.

    Precedence (first non-empty wins):
    1. ``partition_keys`` — comma/whitespace-separated ``endpoint_id#part_id``
       keys. This is the multi-tenant form.
    2. ``partition_key`` — single-tenant direct form.
    3. ``endpoint_id`` + ``part_id`` — combined as ``{endpoint_id}#{part_id}``.

    Blank entries are dropped; duplicates preserved order, deduplicated.
    Returns an empty list when nothing is configured — ticks then no-op.
    """
    raw_keys = setting.get("partition_keys") or ""
    if isinstance(raw_keys, (list, tuple)):
        keys = raw_keys
    else:
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]

    if not keys:
        pk = setting.get("partition_key")
        if pk:
            keys = [pk]
        else:
            ep = setting.get("endpoint_id")
            pid = setting.get("part_id")
            if ep and pid:
                keys = [f"{ep}#{pid}"]

    seen = set()
    deduped = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    return deduped


def _for_each_tenant(fn: Callable[[Dict[str, Any]], None]) -> None:
    """Execute ``fn`` once per configured tenant partition."""
    ctx_base = _context()
    setting = ctx_base.get("setting", {})
    keys = _parse_partition_keys(setting)
    if not keys:
        return

    # In PostgreSQL mode, the DB operations require the RLS context to be
    # set correctly per tenant. The Config class provides this.
    for pk in keys:
        ctx = dict(ctx_base)
        ctx["partition_key"] = pk
        
        if Config.DB_BACKEND == "postgresql":
            Config._set_rls_context(pk)
            
        try:
            fn(ctx)
        finally:
            if Config.DB_BACKEND == "postgresql" and Config.db_session:
                Config.db_session.remove()


def tick_prune_versions() -> None:
    """Scan all skills for a tenant and prune versions keeping the last N."""
    from .models.repositories import get_repo
    from .handlers.skill_version_cache import remove_version
    from .handlers.skill_path import resolve_skill_root

    def run(ctx: Dict[str, Any]) -> None:
        try:
            class DummyInfo:
                def __init__(self, context):
                    self.context = context
            
            info = DummyInfo(ctx)
            repo = get_repo("skill")
            
            # List all enabled skills for the tenant
            all_skills = repo.list(info, enabled=True)
            if not all_skills or not hasattr(all_skills, "skill_list"):
                return
                
            keep = int(_setting("hsk_scheduler_prune_keep", 3))
            updated_by = "scheduler"
            
            # Group by skill name
            grouped = {}
            for row in all_skills.skill_list:
                grouped.setdefault(row.name, []).append(row)
                
            skill_root = resolve_skill_root()
            pruned_count = 0
            
            for name, versions in grouped.items():
                if len(versions) <= keep:
                    continue
                # Sort descending by updated_at
                versions = sorted(versions, key=lambda r: r.updated_at, reverse=True)
                for row in versions[keep:]:
                    repo.insert_update(
                        info,
                        skill_uuid=row.skill_uuid,
                        enabled=False,
                        updated_by=updated_by,
                    )
                    remove_version(skill_root, row.name, row.version)
                    pruned_count += 1
                    
            if pruned_count > 0:
                logger.info(
                    f"tick_prune_versions [{ctx.get('partition_key')}]: "
                    f"pruned {pruned_count} old skill version(s)"
                )
        except Exception as e:
            logger.error(f"Error in tick_prune_versions for tenant {ctx.get('partition_key')}: {e}")

    try:
        _for_each_tenant(run)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"tick_prune_versions failed: {exc}")


# ── Lifecycle ─────────────────────────────────────────────────────────────

def start_scheduler() -> None:
    """Start the background scheduler. Called by the gateway on_startup hook.

    Safe to call multiple times — if a scheduler is already running, this
    is a no-op.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        logger.info("Harness Engineering Engine scheduler already running — skipping start")
        return

    logger.info("Starting Harness Engineering Engine background scheduler...")
    _scheduler = BackgroundScheduler(
        job_defaults={
            "coalesce": True,       # Only one pending run of the same job
            "max_instances": 1,     # No concurrent runs of the same tick
            "misfire_grace_time": 30,  # Tolerate 30s clock skew / pause
        },
    )

    # Prune old skill versions
    _scheduler.add_job(
        tick_prune_versions,
        trigger=IntervalTrigger(
            seconds=int(_setting(
                "hsk_scheduler_prune_interval",
                DEFAULT_PRUNE_INTERVAL,
            ))
        ),
        id="tick_prune_versions",
        name="Prune old skill versions",
    )

    _scheduler.start()


def stop_scheduler() -> None:
    """Stop the background scheduler. Called by the gateway on_shutdown hook."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        logger.info("Stopping Harness Engineering Engine background scheduler...")
        _scheduler.shutdown(wait=False)
        _scheduler = None


__all__ = [
    "start_scheduler",
    "stop_scheduler",
    "tick_prune_versions",
]
