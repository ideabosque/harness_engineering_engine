#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import logging
from typing import Any, Dict

from graphene import ResolveInfo
from silvaengine_utility import method_cache

from ..handlers.config import Config
from ..handlers.skill_reader import skill as read_skill
from ..models.repositories import get_repo
from ..types.skill import SkillListType, SkillType


def resolve_skill(info: ResolveInfo, **kwargs: Dict[str, Any]) -> SkillType | None:
    """Resolve one skill.

    ``name`` is the agent-facing path: it resolves the active enabled
    version, refreshes the local cache from S3 if it is missing or stale,
    and returns the SKILL.md body alongside metadata (see
    ``handlers.skill_reader.skill``). ``skill_uuid`` is the admin/management
    path: a plain row lookup with no body and no refresh, matching the
    ``skills`` catalog query.
    """
    name = kwargs.get("name")
    if name:
        try:
            data = read_skill(info, name=name)
        except (ValueError, FileNotFoundError) as e:
            logger = info.context.get("logger") or logging.getLogger(__name__)
            logger.info(f"skill(name='{name}') not resolvable: {e}")
            return None
        return SkillType(**data)

    return get_repo("skill").resolve_single(info, **kwargs)


@method_cache(
    ttl=Config.get_cache_ttl(),
    cache_name=Config.get_cache_name("queries", "skill"),
    cache_enabled=Config.is_cache_enabled,
)
def resolve_skill_list(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> SkillListType:
    return get_repo("skill").list(info, **kwargs)


def resolve_search_skills(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> SkillListType:
    """Lexical search over name and description, ranked in Python for v1."""
    query = kwargs.get("query", "").strip()
    if not query:
        return SkillListType(skill_list=[], total=0)

    limit = kwargs.get("limit", 10)

    # Fetch enabled skills — we do client-side ranking for v1
    all_skills = get_repo("skill").list(info, enabled=True, limit=1000)
    items = all_skills.skill_list if hasattr(all_skills, "skill_list") else []

    exact_matches = []
    prefix_matches = []
    description_matches = []

    query_lower = query.lower()

    for skill in items:
        name = skill.name or ""
        desc = skill.description or ""

        if name.lower() == query_lower:
            exact_matches.append(skill)
        elif name.lower().startswith(query_lower):
            prefix_matches.append(skill)
        elif query_lower in desc.lower():
            description_matches.append(skill)

    ranked = exact_matches + prefix_matches + description_matches
    ranked = ranked[:limit]

    return SkillListType(skill_list=ranked, total=len(ranked))
