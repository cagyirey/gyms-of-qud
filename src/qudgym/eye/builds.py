"""Human-curated presets; native exports remain inert data, not a game creation API."""
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from qudgym.native_exports import ExportModel, NativeExportSource, export_summary, read_export, read_json

Ref = Annotated[str, Field(min_length=1, max_length=160)]
Text = Annotated[str, Field(max_length=8192)]


class BuildPreset(ExportModel):
    schema_version: Literal["qud-build/1", "qud-build/2"] = "qud-build/1"
    id: Ref
    revision: Ref
    # Declaration for the source preset, NOT detected installed-runtime compatibility.
    game_build: Ref
    # null means enabled mods/load order are unconfirmed; [] explicitly means none.
    mods: tuple[Ref, ...] | None = ()
    creation_code: Text | None = None
    native_export: NativeExportSource | None = None
    notes: Text = ""

    @model_validator(mode="after")
    def supplied_source(self):
        if (self.creation_code is None) == (self.native_export is None):
            raise ValueError("supply exactly one creation_code or native_export")
        if self.creation_code is not None and not self.creation_code.strip():
            raise ValueError("creation_code must be supplied by the player")
        if self.native_export is not None and self.schema_version != "qud-build/2":
            raise ValueError("native export sources require qud-build/2")
        return self


class BuildLibrary(ExportModel):
    schema_version: Literal["qud-build-library/1", "qud-build-library/2"] = "qud-build-library/1"
    presets: tuple[BuildPreset, ...] = ()

    @model_validator(mode="after")
    def unique_ids(self):
        if len({p.id for p in self.presets}) != len(self.presets):
            raise ValueError("duplicate preset IDs")
        if self.schema_version == "qud-build-library/1" and any(
            p.schema_version != "qud-build/1" for p in self.presets
        ):
            raise ValueError("version 2 presets require qud-build-library/2")
        return self

    def by_id(self, preset_id: str) -> BuildPreset:
        hits = [p for p in self.presets if p.id == preset_id]
        if len(hits) != 1:
            raise ValueError("preset ID must resolve uniquely")
        return hits[0]


def load_library(path: str | Path) -> BuildLibrary:
    _, data = read_json(Path(path))
    return BuildLibrary.model_validate(data)


def inspect_sources(library: BuildLibrary, source_root: Path) -> list[dict]:
    """Resolve only when an explicit root is supplied. Nothing is sent to Qud."""
    reports = []
    for preset in library.presets:
        report = {"id": preset.id, "revision": preset.revision,
                  "declared_preset_game_build": preset.game_build,
                  "enabled_mods_in_order": preset.mods,
                  "game_legality_checked": False, "character_instantiated": False}
        if preset.native_export:
            export = read_export(preset.native_export, source_root)
            if export.gameversion != preset.game_build:
                raise ValueError(f"{preset.id}: preset declaration and native export version disagree")
            report.update(export_summary(export))
            report["source_sha256"] = preset.native_export.sha256
            report["source_kind"] = "native_export"
        else:
            report["source_kind"] = "creation_code"
        reports.append(report)
    return reports
