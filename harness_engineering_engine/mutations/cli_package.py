#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Boolean, Field, Mutation, String

from ..models.repositories import get_repo
from ..types.cli_package import CliPackageType


class InsertUpdateCliPackage(Mutation):
    cli_package = Field(CliPackageType)

    class Arguments:
        cli_package_uuid = String(required=False)
        package_name = String(required=True)
        git_repository_url = String(required=True)
        version = String(required=True)
        git_ref = String(required=False)
        description = String(required=False)
        status = String(required=False)
        enabled = Boolean(required=False)
        updated_by = String(required=True)

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "InsertUpdateCliPackage":
        try:
            cli_package = get_repo("cli_package").insert_update(info, **kwargs)
        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            raise e

        return InsertUpdateCliPackage(cli_package=cli_package)


class DeleteCliPackage(Mutation):
    ok = Boolean()

    class Arguments:
        cli_package_uuid = String(required=True)

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "DeleteCliPackage":
        try:
            ok = get_repo("cli_package").delete(info, **kwargs)
        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            raise e

        return DeleteCliPackage(ok=ok)


class EnsureCliPackage(Mutation):
    """Ensure a registered CLI package is installed and verified.

    Called by the command executor before running a CLI package command.
    Installs from the registered GitHub ref if missing, upgrades if outdated.
    """

    class Arguments:
        package_name = String(required=True)

    package_name = String()
    version = String()
    status = String()
    error = String()

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "EnsureCliPackage":
        try:
            from ..handlers.cli_package_manager import ensure_package

            result = ensure_package(info, kwargs["package_name"])
            return EnsureCliPackage(
                package_name=result.get("package_name"),
                version=result.get("version"),
                status=result.get("status", "error"),
                error=result.get("error"),
            )
        except Exception as e:
            info.context.get("logger").error(traceback.format_exc())
            return EnsureCliPackage(
                package_name=kwargs.get("package_name"),
                status="error",
                error=str(e),
            )