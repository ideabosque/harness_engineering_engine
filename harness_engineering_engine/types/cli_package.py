#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from graphene import Boolean, DateTime, List, ObjectType, String
from silvaengine_dynamodb_base import ListObjectType


class CliPackageType(ObjectType):
    partition_key = String()
    endpoint_id = String()
    part_id = String()
    cli_package_uuid = String()

    package_name = String()
    git_repository_url = String()
    version = String()
    git_ref = String()
    description = String()

    status = String()
    enabled = Boolean()

    created_at = DateTime()
    updated_by = String()
    updated_at = DateTime()


class CliPackageListType(ListObjectType):
    cli_package_list = List(CliPackageType)