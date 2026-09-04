#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import logging
from typing import Any, Dict, List

from graphene import Schema

from silvaengine_utility import Graphql

from .handlers.config import Config
from .schema import Mutations, Query, type_class


# Hook function applied to deployment
def deploy() -> List:
    return [
        {
            "service": "Harness Engineering",
            "class": "HarnessEngineeringEngine",
            "functions": {
                "harness_graphql": {
                    "is_static": False,
                    "label": "Harness Engineering GraphQL",
                    "query": [
                        {"action": "ping", "label": "Ping"},
                        {"action": "skills", "label": "Skill Catalog"},
                        {"action": "searchSkills", "label": "Search Skills"},
                        {"action": "skill", "label": "Get Skill"},
                        {"action": "cliPackages", "label": "CLI Package Catalog"},
                        {"action": "cliPackage", "label": "Get CLI Package"},
                    ],
                    "mutation": [
                        {"action": "insertUpdateSkill", "label": "Insert Update Skill"},
                        {"action": "deleteSkill", "label": "Delete Skill"},
                        {"action": "deploySkillPackage", "label": "Deploy Skill Package"},
                        {"action": "refreshLocalSkills", "label": "Refresh Local Skills"},
                        {"action": "rollbackSkill", "label": "Rollback Skill"},
                        {"action": "promoteSkillVersion", "label": "Promote Skill Version"},
                        {"action": "disableSkill", "label": "Disable Skill"},
                        {"action": "pruneSkillVersions", "label": "Prune Skill Versions"},
                        {"action": "registerSkills", "label": "Register Skills"},
                        {"action": "runCommand", "label": "Run Command"},
                        {"action": "insertUpdateCliPackage", "label": "Insert Update CLI Package"},
                        {"action": "deleteCliPackage", "label": "Delete CLI Package"},
                        {"action": "ensureCliPackage", "label": "Ensure CLI Package"},
                    ],
                    "type": "RequestResponse",
                    "support_methods": ["POST"],
                    "is_auth_required": False,
                    "is_graphql": True,
                    "settings": "harness_engineering",
                    "disabled_in_resources": True,
                },
            },
        }
    ]


class HarnessEngineeringEngine(Graphql):
    def __init__(self, logger: logging.Logger, **setting: Dict[str, Any]) -> None:
        Graphql.__init__(self, logger, **setting)
        Config.initialize(logger, setting)
        self.logger = logger
        self.setting = setting

    def _apply_partition_defaults(self, params: Dict[str, Any]) -> None:
        endpoint_id = params.get("endpoint_id", self.setting.get("endpoint_id"))
        part_id = params.get("metadata", {}).get(
            "part_id",
            params.get("part_id", self.setting.get("part_id")),
        )

        if params.get("context") is None:
            params["context"] = {}

        if "endpoint_id" not in params["context"]:
            params["context"]["endpoint_id"] = endpoint_id
        if "part_id" not in params["context"]:
            params["context"]["part_id"] = part_id
        if "connection_id" not in params:
            params["connection_id"] = self.setting.get("connection_id")

        if "partition_key" not in params["context"]:
            if not endpoint_id or not part_id:
                self.logger.error(
                    f"Missing endpoint_id or part_id: endpoint_id={endpoint_id}, part_id={part_id}"
                )
                raise ValueError(
                    "Both 'endpoint_id' and 'part_id' are required to generate 'partition_key'."
                )
            else:
                params["context"]["partition_key"] = f"{endpoint_id}#{part_id}"

    def harness_graphql(self, **params: Dict[str, Any]) -> Any:
        self._apply_partition_defaults(params)

        partition_key = params.get("context", {}).get("partition_key")
        if partition_key and Config.DB_BACKEND == "postgresql":
            Config._set_rls_context(partition_key)

        schema = Schema(
            query=Query,
            mutation=Mutations,
            types=type_class(),
        )
        return self.execute(schema, **params)

    # Backward-compatible alias
    hsk_graphql = harness_graphql

    @staticmethod
    def build_graphql_schema() -> Schema:
        return Schema(
            query=Query,
            mutation=Mutations,
            types=type_class(),
        )


# ---------------------------------------------------------------------------
# Module-level dispatch functions for gateway integration
# ---------------------------------------------------------------------------


def dispatch_graphql(**params: Any) -> Any:
    """Execute a GraphQL query/mutation against the Harness Engineering Engine.

    Requires Config.initialize() to have been called (done by gateway startup).
    """
    from .handlers.config import Config

    logger = Config.get_logger()
    instance = HarnessEngineeringEngine(logger, **Config.get_setting())
    db_session = Config.db_session
    try:
        return instance.harness_graphql(**params)
    except Exception:
        if db_session is not None:
            db_session.rollback()
        raise
    finally:
        if db_session is not None:
            db_session.remove()
