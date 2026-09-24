"""Metadata for a human-curated preset library; no guessed Qud creation API."""
from pathlib import Path
from typing import Literal
from pydantic import model_validator
from .contracts import EyeModel, Ref, Text


class BuildPreset(EyeModel):
    schema_version: Literal["qud-build/1"] = "qud-build/1"
    id: Ref
    revision: Ref
    game_build: Ref
    # Exact enabled mod IDs in load order; reported by the owner, not discovered here.
    mods: tuple[Ref, ...] = ()
    creation_code: Text
    notes: Text = ""

    @model_validator(mode="after")
    def supplied_code(self):
        if not self.creation_code.strip():
            raise ValueError("creation_code must be supplied by the player")
        return self


class BuildLibrary(EyeModel):
    schema_version: Literal["qud-build-library/1"] = "qud-build-library/1"
    presets: tuple[BuildPreset, ...] = ()

    @model_validator(mode="after")
    def unique_ids(self):
        if len({p.id for p in self.presets}) != len(self.presets):
            raise ValueError("duplicate preset IDs")
        return self

    def by_id(self, preset_id: str) -> BuildPreset:
        hits = [p for p in self.presets if p.id == preset_id]
        if len(hits) != 1:
            raise ValueError("preset ID must resolve uniquely")
        return hits[0]


def load_library(path: str | Path) -> BuildLibrary:
    with Path(path).open("rb") as f:
        raw = f.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("build library exceeds 1 MiB")
    library = BuildLibrary.model_validate_json(raw)
    return library
