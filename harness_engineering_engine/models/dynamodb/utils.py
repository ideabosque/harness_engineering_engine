# -*- coding: utf-8 -*-
"""Initialize DynamoDB tables for harness_engineering_engine."""
from __future__ import print_function

__author__ = "bibow"

import logging

from .skill import SkillModel
from .cli_package import CliPackageModel


def initialize_tables(logger: logging.Logger) -> None:
    """Create DynamoDB tables if they do not exist."""
    logger.info("Initializing DynamoDB tables...")
    if not SkillModel.exists():
        SkillModel.create_table(wait=True, billing_mode="PAY_PER_REQUEST")
        logger.info("Table 'hsk-skills' created.")
    if not CliPackageModel.exists():
        CliPackageModel.create_table(wait=True, billing_mode="PAY_PER_REQUEST")
        logger.info("Table 'hsk-cli-packages' created.")
    logger.info("DynamoDB tables initialized.")
