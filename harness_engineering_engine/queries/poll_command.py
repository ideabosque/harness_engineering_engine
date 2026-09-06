#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Query resolver for pollCommand — async command status polling."""
from __future__ import print_function

__author__ = "bibow"

import logging
from typing import Any, Dict

from graphene import ResolveInfo

from ..handlers.async_command_executor import poll_command
from ..types.poll_command import PollCommandType


def resolve_poll_command(info: ResolveInfo, **kwargs: Dict[str, Any]) -> PollCommandType | None:
    """Poll the status of a background command launched by runCommand(background=true).

    Returns a PollCommandType with status, stdout, stderr, exit_code, timed_out, truncated.
    Status is "not_found" if the run_id is unknown (expired or never existed).
    """
    logger = info.context.get("logger") or logging.getLogger(__name__)
    run_id = kwargs.get("run_id")

    if not run_id:
        return PollCommandType(
            run_id="",
            status="not_found",
            stdout="",
            stderr="",
            exit_code=None,
            timed_out=False,
            truncated=False,
        )

    result = poll_command(run_id, logger)
    return PollCommandType(**result)