# -*- coding: utf-8 -*-
"""Initialize PostgreSQL tables for harness_engineering_engine."""
from __future__ import print_function

__author__ = "bibow"

import logging


def initialize_tables(logger: logging.Logger, db_session) -> None:
    """Create PostgreSQL tables if they do not exist."""
    logger.info("Initializing PostgreSQL tables...")
    from ...utils.rls import create_rls_policies
    from .skill import SkillModel
    from .cli_package import CliPackageModel
    from .command_run import CommandRunModel

    engine = db_session.bind
    SkillModel.__table__.create(engine, checkfirst=True)
    logger.info("PostgreSQL table 'hsk_skills' created (if not present).")
    CliPackageModel.__table__.create(engine, checkfirst=True)
    logger.info("PostgreSQL table 'hsk_cli_packages' created (if not present).")
    CommandRunModel.__table__.create(engine, checkfirst=True)
    logger.info("PostgreSQL table 'hsk_command_runs' created (if not present).")

    create_rls_policies(engine)
    logger.info("RLS policies applied.")


def drop_tables(logger: logging.Logger, db_session) -> None:
    """Drop PostgreSQL tables (dangerous — use in tests only)."""
    logger.warning("Dropping PostgreSQL tables...")
    from .skill import SkillModel

    engine = db_session.bind
    SkillModel.__table__.drop(engine, checkfirst=True)
    logger.info("PostgreSQL table 'hsk_skills' dropped.")
