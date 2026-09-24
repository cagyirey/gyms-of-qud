"""Strict, redacted compatibility-evidence contracts.

These models validate metadata only. They do not detect a game build, prove that
an API exists, or turn a report into a live capability.
"""
from __future__ import annotations

import json
from pathlib import PurePath
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=160)]
ShortText = Annotated[str, Field(min_length=1, max_length=512)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _safe_component(value: str) -> str:
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("compatibility names must be single path components")
    return value


def _redacted_text(value: str) -> str:
    if PurePath(value).is_absolute() or value.startswith(("~", "\\\\")):
        raise ValueError("compatibility evidence must not contain absolute paths")
    if "/Users/" in value or "/home/" in value or "Application Support" in value:
        raise ValueError("compatibility evidence must not contain private paths")
    return value


class CompatModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PlatformInfo(CompatModel):
    system: Annotated[str, Field(max_length=64)]
    release: Annotated[str, Field(max_length=128)]
    machine: Annotated[str, Field(max_length=64)]


class AssemblyDigest(CompatModel):
    name: Identifier
    bytes: Annotated[int, Field(ge=0, strict=True)]
    sha256: Sha256

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        return _safe_component(value)


class ModDigest(CompatModel):
    directory_name: Identifier
    ID: str | int | float | bool | None = None
    Title: str | int | float | bool | None = None
    Version: str | int | float | bool | None = None
    id: str | int | float | bool | None = None
    title: str | int | float | bool | None = None
    version: str | int | float | bool | None = None
    manifest_status: Literal["unreadable"] | None = None

    @field_validator("directory_name")
    @classmethod
    def safe_directory(cls, value: str) -> str:
        return _safe_component(value)

    @model_validator(mode="after")
    def redacted_metadata(self) -> ModDigest:
        for name in ("ID", "Title", "Version", "id", "title", "version"):
            value = getattr(self, name)
            if isinstance(value, str):
                _redacted_text(value)
        return self


class InstallManifest(CompatModel):
    manifest_version: Literal["0.1"]
    game_version: str | None = Field(default=None, max_length=128)
    platform: PlatformInfo
    assemblies: Annotated[tuple[AssemblyDigest, ...], Field(max_length=64)]
    mods: Annotated[tuple[ModDigest, ...], Field(max_length=256)]
    notes: Annotated[tuple[ShortText, ...], Field(max_length=32)] = ()

    @field_validator("game_version")
    @classmethod
    def safe_game_version(cls, value: str | None) -> str | None:
        return None if value is None else _redacted_text(value)

    @field_validator("notes")
    @classmethod
    def safe_notes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_redacted_text(value) for value in values)

    @model_validator(mode="after")
    def unique_entries(self) -> InstallManifest:
        assembly_names = [entry.name for entry in self.assemblies]
        mod_names = [entry.directory_name for entry in self.mods]
        if len(set(assembly_names)) != len(assembly_names):
            raise ValueError("assembly names must be unique")
        if len(set(mod_names)) != len(mod_names):
            raise ValueError("mod directory names must be unique")
        return self


class ModReference(CompatModel):
    mod_id: Identifier
    load_order: Annotated[int, Field(ge=0, strict=True)]
    version: str | None = Field(default=None, max_length=128)

    @field_validator("mod_id")
    @classmethod
    def safe_mod_id(cls, value: str) -> str:
        return _safe_component(value)

    @field_validator("version")
    @classmethod
    def safe_version(cls, value: str | None) -> str | None:
        return None if value is None else _redacted_text(value)


class HookEvidence(CompatModel):
    name: Identifier
    available: bool
    thread_id: Annotated[int | None, Field(default=None, ge=0, strict=True)]
    detail: ShortText = ""

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        return _safe_component(value)

    @field_validator("detail")
    @classmethod
    def safe_detail(cls, value: str) -> str:
        return _redacted_text(value)


class DiagnosticReport(CompatModel):
    schema_version: Literal["qudgym-compat/1"]
    game_build: Annotated[str, Field(min_length=1, max_length=128)]
    profile: Literal["dedicated-test"]
    enabled_mods: Annotated[tuple[ModReference, ...], Field(max_length=256)] = ()
    hooks: Annotated[tuple[HookEvidence, ...], Field(max_length=128)] = ()
    notes: Annotated[tuple[ShortText, ...], Field(max_length=32)] = ()

    @field_validator("game_build", "notes")
    @classmethod
    def safe_text(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        if isinstance(value, tuple):
            return tuple(_redacted_text(item) for item in value)
        return _redacted_text(value)

    @model_validator(mode="after")
    def unique_evidence(self) -> DiagnosticReport:
        mod_ids = [entry.mod_id for entry in self.enabled_mods]
        load_orders = [entry.load_order for entry in self.enabled_mods]
        hook_names = [entry.name for entry in self.hooks]
        if len(set(mod_ids)) != len(mod_ids):
            raise ValueError("enabled mod IDs must be unique")
        if len(set(load_orders)) != len(load_orders):
            raise ValueError("mod load orders must be unique")
        if len(set(hook_names)) != len(hook_names):
            raise ValueError("hook evidence names must be unique")
        return self


CompatibilityDocument: TypeAlias = InstallManifest | DiagnosticReport


def parse_document(raw: bytes, *, kind: Literal["auto", "manifest", "diagnostic"] = "auto") -> CompatibilityDocument:
    if len(raw) > 1_048_576:
        raise ValueError("compatibility document exceeds 1 MiB")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("compatibility document must be UTF-8 JSON") from exc
    if kind == "manifest":
        return InstallManifest.model_validate(value)
    if kind == "diagnostic":
        return DiagnosticReport.model_validate(value)
    if isinstance(value, dict) and value.get("manifest_version") == "0.1":
        return InstallManifest.model_validate(value)
    return DiagnosticReport.model_validate(value)


def document_summary(document: CompatibilityDocument) -> dict[str, object]:
    if isinstance(document, InstallManifest):
        return {
            "kind": "install-manifest",
            "manifest_version": document.manifest_version,
            "game_version": document.game_version,
            "assembly_count": len(document.assemblies),
            "mod_count": len(document.mods),
        }
    return {
        "kind": "diagnostic-report",
        "schema_version": document.schema_version,
        "game_build": document.game_build,
        "profile": document.profile,
        "hook_count": len(document.hooks),
        "enabled_mod_count": len(document.enabled_mods),
    }
