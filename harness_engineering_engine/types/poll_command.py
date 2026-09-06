#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from graphene import Boolean, Int, ObjectType, String


class PollCommandType(ObjectType):
    """Result type for the pollCommand query — async command status."""

    run_id = String()
    status = String()  # "running", "completed", "timed_out", "not_found"
    stdout = String()
    stderr = String()
    exit_code = String()
    timed_out = Boolean()
    truncated = Boolean()
    output_truncated_bytes = Int()