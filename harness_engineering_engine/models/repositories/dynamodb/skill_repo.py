# -*- coding: utf-8 -*-
"""DynamoDB repository for Skill entity."""
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, Optional

from ...repositories.base import EntityRepository
from ._base import _normalize

from ...dynamodb import skill as _skill_mod


class SkillRepository(EntityRepository):
    """DynamoDB repository for Skill entity."""

    @property
    def entity_type(self) -> str:
        return "skill"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        skill_uuid = keys.get("skill_uuid")
        if not partition_key or not skill_uuid:
            return None
        count = _skill_mod.get_skill_count(partition_key, skill_uuid)
        if count == 0:
            return None
        return _normalize(_skill_mod.get_skill(partition_key, skill_uuid))

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        skill_uuid = keys.get("skill_uuid")
        if not partition_key or not skill_uuid:
            return 0
        return _skill_mod.get_skill_count(partition_key, skill_uuid)

    def list(self, info: Any, **filters: Any) -> Any:
        return _skill_mod.resolve_skill_list(info, **filters)

    def insert_update(self, info: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return _skill_mod.insert_update_skill(info, **kwargs)

    def delete(self, info: Any, **kwargs: Any) -> bool:
        return _skill_mod.delete_skill(info, **kwargs)

    def get_type(self, info: Any, skill: Any) -> Any:
        """Return SkillType from a PynamoDB model instance."""
        return _skill_mod.get_skill_type(info, skill)

    def resolve_single(self, info: Any, **kwargs: Any) -> Any:
        """Resolve a single skill (supports name lookup)."""
        return _skill_mod.resolve_skill(info, **kwargs)


__all__ = ["SkillRepository"]
