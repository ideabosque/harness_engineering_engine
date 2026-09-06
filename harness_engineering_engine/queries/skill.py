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
    """Fuzzy search over name and description, ranked in Python for v1.

    Uses rapidfuzz token-set ratio for fuzzy matching against both name and
    description. Exact name matches always rank first, followed by prefix
    matches, then fuzzy matches sorted by composite score (name weighted 2×
    over description). Falls back to the old lexical approach if rapidfuzz
    is unavailable.
    """
    query = kwargs.get("query", "").strip()
    if not query:
        return SkillListType(skill_list=[], total=0)

    limit = kwargs.get("limit", 10)

    # Fetch enabled skills — we do client-side ranking for v1
    all_skills = get_repo("skill").list(info, enabled=True, limit=1000)
    items = all_skills.skill_list if hasattr(all_skills, "skill_list") else []

    query_lower = query.lower()

    # ------------------------------------------------------------------
    # Tier 1: exact name and prefix matches (highest priority)
    # ------------------------------------------------------------------
    exact_matches = []
    prefix_matches = []
    remaining = []

    for skill in items:
        name = (skill.name or "").lower()
        if name == query_lower:
            exact_matches.append(skill)
        elif name.startswith(query_lower):
            prefix_matches.append(skill)
        else:
            remaining.append(skill)

    # ------------------------------------------------------------------
    # Tier 2: fuzzy matches via rapidfuzz
    # ------------------------------------------------------------------
    fuzzy_matches = []
    try:
        from rapidfuzz import fuzz

        for skill in remaining:
            name = skill.name or ""
            desc = skill.description or ""

            # token_set_ratio handles word-order differences and partial
            # token overlap well (e.g. "video slideshow" matches "slideshow
            # video production"). Score is 0-100.
            name_score = fuzz.token_set_ratio(query_lower, name.lower())
            desc_score = fuzz.token_set_ratio(query_lower, desc.lower())

            # Composite: name weighted 2× over description. A skill that
            # matches only in description needs a decent score to surface.
            composite = (name_score * 2 + desc_score) / 3

            # Threshold: only include skills with at least some relevance.
            # 40 is a lenient cutoff — token_set_ratio returns 0 for no
            # token overlap at all, and ~50+ for partial matches.
            if composite >= 40:
                fuzzy_matches.append((composite, skill))

        # Sort by composite score descending
        fuzzy_matches.sort(key=lambda x: x[0], reverse=True)
        fuzzy_skills = [s for _, s in fuzzy_matches]
    except ImportError:
        # Fallback: old lexical substring match
        fuzzy_skills = [
            skill
            for skill in remaining
            if query_lower in (skill.description or "").lower()
        ]

    ranked = (exact_matches + prefix_matches + fuzzy_skills)[:limit]

    return SkillListType(skill_list=ranked, total=len(ranked))
