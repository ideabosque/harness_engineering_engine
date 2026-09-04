# -*- coding: utf-8 -*-
"""Centralized configuration for harness_engineering_engine.

Manages shared settings, AWS clients, and DB backend selection. Follows the
same conventions as ``rfq_engine.handlers.config.Config`` so that gateway and
deployment integration behaves identically.
"""
from __future__ import print_function

__author__ = "bibow"

import logging
import os
from typing import Any, Dict, List, Optional


class Config:
    """
    Centralized Configuration Class
    Manages shared configuration variables across the application.
    """

    _initialized: bool = False
    _logger: Optional[logging.Logger] = None
    _setting: Dict[str, Any] = {}

    # Backend selection: "dynamodb" (default) or "postgresql"
    DB_BACKEND: str = "dynamodb"

    # PostgreSQL session (only initialized when DB_BACKEND == "postgresql")
    db_session = None

    # PostgreSQL table prefix — prepended to all PG table and index names
    # to avoid collisions in shared databases.  Set from ``pg_table_prefix``
    # setting by ``_initialize_db_session``.  Empty string = no prefix.
    PG_TABLE_PREFIX: str = "hsk_"

    # AWS clients
    aws_lambda = None
    aws_sqs = None

    # Cache Configuration
    CACHE_TTL = 1800  # 30 minutes default TTL
    CACHE_ENABLED = True

    # Cache name patterns for different modules.
    CACHE_NAMES = {
        "models": "harness_engineering_engine.models.dynamodb",
        "queries": "harness_engineering_engine.queries",
    }

    # ------------------------------------------------------------------
    # Harness-engineering–specific settings
    # ------------------------------------------------------------------
    # Skill runtime cache root.  May come from the setting dict or the
    # HSK_SKILL_ROOT environment variable.  Resolved at ``initialize()``.
    SKILL_ROOT: str = ""

    # Path to an alternate SSH private key for git-over-SSH skill sources.
    # Empty means "use the system default identity" (ssh-agent / ~/.ssh/config).
    GIT_SSH_KEY_PATH: str = ""

    # Local metadata filename stored in each installed skill directory.
    SKILL_LOCAL_METADATA_FILE: str = ".hsk-skill.json"

    # Whether startup should compare local metadata against the registration
    # table and S3 artifacts.
    SKILL_REFRESH_ON_STARTUP: bool = False

    # Allow ``skill(name)`` to return stale content when the local checksum
    # differs from the registered checksum.
    ALLOW_UNREGISTERED_CHANGES: bool = False

    # Guarded command executor settings.
    RUN_COMMAND_ENABLED: bool = False
    RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS: int = 30
    RUN_COMMAND_OUTPUT_LIMIT_BYTES: int = 20_000
    RUN_COMMAND_WORKSPACE_ROOT: str = ""

    # Dry-run mode: resolve and validate commands without executing them.
    DRY_RUN: bool = False

    # ------------------------------------------------------------------
    # Cache entity metadata (DynamoDB only today).
    # ------------------------------------------------------------------
    CACHE_ENTITY_CONFIG_DYNAMODB: Dict[str, Dict[str, Any]] = {
        "skill": {
            "module": "harness_engineering_engine.models.dynamodb.skill",
            "model_class": "SkillModel",
            "getter": "get_skill",
            "list_resolver": "harness_engineering_engine.queries.skill.resolve_skill_list",
            "cache_keys": ["context:partition_key", "key:skill_uuid"],
        },
        "cli_package": {
            "module": "harness_engineering_engine.models.dynamodb.cli_package",
            "model_class": "CliPackageModel",
            "getter": "get_cli_package",
            "list_resolver": "harness_engineering_engine.queries.cli_package.resolve_cli_package_list",
            "cache_keys": ["context:partition_key", "key:cli_package_uuid"],
        },
    }

    # PostgreSQL cache config — empty until PG repos opt into caching.
    CACHE_ENTITY_CONFIG_POSTGRESQL: Dict[str, Dict[str, Any]] = {}

    CACHE_RELATIONSHIPS_DYNAMODB: Dict[str, List[Dict[str, Any]]] = {}
    CACHE_RELATIONSHIPS_POSTGRESQL: Dict[str, List[Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    @classmethod
    def get_cache_entity_config(cls) -> Dict[str, Dict[str, Any]]:
        """Return cache metadata for the active ``DB_BACKEND``."""
        if cls.DB_BACKEND == "postgresql":
            return cls.CACHE_ENTITY_CONFIG_POSTGRESQL
        return cls.CACHE_ENTITY_CONFIG_DYNAMODB

    @classmethod
    def get_cache_relationships(cls) -> Dict[str, List[Dict[str, Any]]]:
        """Return cascade-invalidation relationships for the active backend."""
        if cls.DB_BACKEND == "postgresql":
            return cls.CACHE_RELATIONSHIPS_POSTGRESQL
        return cls.CACHE_RELATIONSHIPS_DYNAMODB

    @classmethod
    def initialize(cls, logger: logging.Logger, setting: Dict[str, Any]) -> None:
        """Initialize configuration setting.

        Backend selection is driven by ``setting["db_backend"]``.
        """
        try:
            cls._logger = logger
            cls._setting = dict(setting)
            cls._set_parameters(setting)

            cls.DB_BACKEND = str(setting.get("db_backend", "dynamodb")).lower()

            if cls.DB_BACKEND == "dynamodb":
                cls._initialize_aws_services(setting)
                cls._initialize_dynamodb_meta(setting)
            elif cls.DB_BACKEND == "postgresql":
                cls._initialize_optional_aws_services(setting)
                cls._initialize_db_session(setting)
            else:
                raise ValueError(f"Unknown db_backend: {cls.DB_BACKEND}")

            if setting.get("initialize_tables"):
                cls._initialize_tables(logger)
            cls._initialized = True
            logger.info(
                f"Configuration initialized successfully (db_backend={cls.DB_BACKEND})."
            )
        except Exception as e:
            logger.exception("Failed to initialize configuration.")
            raise e

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @classmethod
    def _set_parameters(cls, setting: Dict[str, Any]) -> None:
        """Set application-level parameters from the setting dict."""
        cls.source_email = setting.get("source_email")

        if "cache_enabled" in setting:
            cls.CACHE_ENABLED = setting.get("cache_enabled", True)

        # Harness-engineering settings — prefer env vars, fall back to setting.
        cls.SKILL_ROOT = os.environ.get(
            "HSK_SKILL_ROOT", setting.get("hsk_skill_root", "")
        )
        cls.GIT_SSH_KEY_PATH = os.environ.get(
            "HSK_GIT_SSH_KEY_PATH",
            setting.get("hsk_git_ssh_key_path", ""),
        )
        cls.SKILL_LOCAL_METADATA_FILE = os.environ.get(
            "HSK_SKILL_LOCAL_METADATA_FILE",
            setting.get("hsk_skill_local_metadata_file", ".hsk-skill.json"),
        )
        cls.SKILL_REFRESH_ON_STARTUP = os.environ.get(
            "HSK_SKILL_REFRESH_ON_STARTUP",
            setting.get("hsk_skill_refresh_on_startup", False),
        )
        cls.ALLOW_UNREGISTERED_CHANGES = os.environ.get(
            "HSK_ALLOW_UNREGISTERED_CHANGES",
            setting.get("hsk_allow_unregistered_changes", False),
        )
        cls.RUN_COMMAND_ENABLED = os.environ.get(
            "HSK_RUN_COMMAND_ENABLED",
            setting.get("hsk_run_command_enabled", False),
        )
        cls.RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS = int(
            os.environ.get(
                "HSK_RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS",
                setting.get("hsk_run_command_default_timeout_seconds", 30),
            )
        )
        cls.RUN_COMMAND_OUTPUT_LIMIT_BYTES = int(
            os.environ.get(
                "HSK_RUN_COMMAND_OUTPUT_LIMIT_BYTES",
                setting.get("hsk_run_command_output_limit_bytes", 20_000),
            )
        )
        cls.RUN_COMMAND_WORKSPACE_ROOT = os.environ.get(
            "HSK_RUN_COMMAND_WORKSPACE_ROOT",
            setting.get("hsk_run_command_workspace_root", ""),
        )
        cls.DRY_RUN = os.environ.get(
            "HSK_DRY_RUN", setting.get("hsk_dry_run", False)
        )

    # ------------------------------------------------------------------
    # AWS service initialization
    # ------------------------------------------------------------------

    @classmethod
    def _initialize_aws_services(cls, setting: Dict[str, Any]) -> None:
        """Initialize AWS services (Lambda, SQS) used by the base engine."""
        import boto3

        if all(
            setting.get(k)
            for k in ["region_name", "aws_access_key_id", "aws_secret_access_key"]
        ):
            aws_credentials = {
                "region_name": setting["region_name"],
                "aws_access_key_id": setting["aws_access_key_id"],
                "aws_secret_access_key": setting["aws_secret_access_key"],
            }
        else:
            aws_credentials = {}

        cls.aws_lambda = boto3.client("lambda", **aws_credentials)
        cls.aws_sqs = boto3.resource("sqs", **aws_credentials)

    @classmethod
    def _initialize_dynamodb_meta(cls, setting: Dict[str, Any]) -> None:
        """Initialize PynamoDB BaseModel.Meta credentials from setting."""
        from silvaengine_dynamodb_base import BaseModel

        if (
            setting.get("region_name")
            and setting.get("aws_access_key_id")
            and setting.get("aws_secret_access_key")
        ):
            BaseModel.Meta.region = setting.get("region_name")
            BaseModel.Meta.aws_access_key_id = setting.get("aws_access_key_id")
            BaseModel.Meta.aws_secret_access_key = setting.get("aws_secret_access_key")

    @classmethod
    def _initialize_optional_aws_services(cls, setting: Dict[str, Any]) -> None:
        """Initialize AWS services only if credentials are present (PG mode)."""
        import boto3

        if all(
            setting.get(k)
            for k in ["region_name", "aws_access_key_id", "aws_secret_access_key"]
        ):
            aws_credentials = {
                "region_name": setting["region_name"],
                "aws_access_key_id": setting["aws_access_key_id"],
                "aws_secret_access_key": setting["aws_secret_access_key"],
            }
            cls.aws_lambda = boto3.client("lambda", **aws_credentials)
            cls.aws_sqs = boto3.resource("sqs", **aws_credentials)
        else:
            cls.aws_lambda = None
            cls.aws_sqs = None

    # ------------------------------------------------------------------
    # PostgreSQL session
    # ------------------------------------------------------------------

    @classmethod
    def _initialize_db_session(cls, setting: Dict[str, Any]) -> None:
        """Initialize the PostgreSQL database session using SQLAlchemy."""
        from urllib.parse import quote_plus

        from sqlalchemy import create_engine
        from sqlalchemy.orm import scoped_session, sessionmaker

        from ..models.postgresql.base import Base

        cls.PG_TABLE_PREFIX = str(setting.get("pg_table_prefix", "hsk_")).strip()
        Base.table_prefix = cls.PG_TABLE_PREFIX
        if cls._logger:
            cls._logger.info(
                f"PostgreSQL table prefix set to '{cls.PG_TABLE_PREFIX}'."
            )

        password = quote_plus(setting["db_password"])
        connection_string = (
            f"postgresql+psycopg2://{setting['db_user']}:{password}"
            f"@{setting['db_host']}:{setting['db_port']}/{setting['db_schema']}"
        )

        engine = create_engine(
            connection_string,
            pool_recycle=7200,
            pool_size=30,
            max_overflow=20,
            pool_timeout=60,
            pool_pre_ping=True,
            echo=False,
        )

        cls.db_session = scoped_session(
            sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )

    @classmethod
    def _initialize_tables(cls, logger: logging.Logger) -> None:
        """Initialize database tables by calling the backend-appropriate method."""
        if cls.DB_BACKEND == "dynamodb":
            from ..models.dynamodb.utils import initialize_tables

            initialize_tables(logger)
        elif cls.DB_BACKEND == "postgresql":
            from ..models.postgresql.utils import initialize_tables as pg_init

            pg_init(logger, cls.db_session)

    @classmethod
    def _set_rls_context(cls, partition_key: str) -> None:
        """Set the RLS tenant context for the current PostgreSQL session."""
        if cls.DB_BACKEND == "postgresql" and cls.db_session and partition_key:
            from ..utils.rls import set_rls_context

            set_rls_context(cls.db_session, partition_key)

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    @classmethod
    def get_cache_name(cls, module_type: str, model_name: str) -> str:
        """Generate standardized cache names."""
        base_name = cls.CACHE_NAMES.get(module_type, f"harness_engineering_engine.{module_type}")
        return f"{base_name}.{model_name}"

    @classmethod
    def get_cache_ttl(cls) -> int:
        """Get the configured cache TTL."""
        return cls.CACHE_TTL

    @classmethod
    def is_cache_enabled(cls) -> bool:
        """Check if caching is enabled."""
        return cls.CACHE_ENABLED

    @classmethod
    def get_setting(cls) -> Dict[str, Any]:
        """Return the setting dict stored at initialization time."""
        if not cls._initialized:
            raise RuntimeError("Config not initialized")
        return cls._setting

    @classmethod
    def get_logger(cls) -> logging.Logger:
        """Return the logger stored at initialization time."""
        if cls._logger:
            return cls._logger
        return logging.getLogger()
