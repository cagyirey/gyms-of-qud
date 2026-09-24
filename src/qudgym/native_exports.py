"""Read native character exports as data. Never resolve or instantiate CLR types."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

LIMIT = 1024 * 1024
Text = Annotated[str, Field(min_length=1, max_length=512, strict=True)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PREFIX = "XRL.CharacterBuilds.Qud."
KNOWN = {
    "QudGenotypeModule": ("Genotype",),
    "QudSubtypeModule": ("Subtype",),
    "QudAttributesModule": ("PointsPurchased", "apSpent", "apRemaining", "baseAp"),
    "QudMutationsModule": ("mp", "selections"),
    "QudCyberneticsModule": ("lp", "selections"),
    "QudChooseStartingLocationModule": ("StartingLocation",),
}


class ExportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class NativeExportSource(ExportModel):
    path: Text
    sha256: Digest

    @model_validator(mode="after")
    def relative_source(self):
        parts = self.path.split("/")
        if ("\\" in self.path or ":" in self.path or "\x00" in self.path
                or any(p in ("", ".", "..") for p in parts)
                or PurePosixPath(self.path).is_absolute()
                or not self.path.endswith(".json")):
            raise ValueError("native export path must be a relative POSIX .json path without traversal")
        return self


class ExportModule(ExportModel):
    moduleType: Text
    data: dict[str, JsonValue]

    @property
    def type_name(self) -> str:
        return self.moduleType.split(",", 1)[0].strip()


class NativeExport(ExportModel):
    gameversion: Text
    buildversion: Text
    modules: tuple[ExportModule, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_modules(self):
        names = [m.type_name for m in self.modules]
        if len(names) != len(set(names)):
            raise ValueError("duplicate native module types")
        return self


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("non-finite JSON number")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def read_json(path: Path) -> tuple[bytes, dict]:
    with path.open("rb") as file:
        raw = file.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("JSON file exceeds 1 MiB")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_nonfinite, parse_float=_float)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid or excessively nested JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return raw, data


def read_export(source: NativeExportSource, root: Path) -> NativeExport:
    root = root.resolve(strict=True)
    path = (root / source.path).resolve(strict=True)
    if not root.is_dir() or not path.is_relative_to(root):
        raise ValueError("native export resolves outside the explicit source root")
    raw, data = read_json(path)
    if hashlib.sha256(raw).hexdigest() != source.sha256:
        raise ValueError("native export hash changed; review it and update its revision/hash")
    return NativeExport.model_validate(data)


def export_summary(export: NativeExport) -> dict:
    """Preserve purchase data and module spelling; do not infer final stats or legality."""
    interpreted, unknown = {}, []
    for module in export.modules:
        name = module.type_name
        short = name.removeprefix(PREFIX)
        if name.startswith(PREFIX) and short in KNOWN:
            interpreted[short] = {k: module.data[k] for k in KNOWN[short] if k in module.data}
        else:
            unknown.append(name)
    return {
        "declared_export_game_version": export.gameversion,
        "declared_build_format_version": export.buildversion,
        "selections": interpreted,
        "uninterpreted_modules": unknown,
        "game_legality_checked": False,
        "character_instantiated": False,
    }


class AssemblyEntry(ExportModel):
    name: Text
    bytes: int = Field(gt=0, strict=True)
    sha256: Digest

    @model_validator(mode="after")
    def simple_name(self):
        if any(c in self.name for c in ("/", "\\", ":", "\x00")) or self.name in (".", ".."):
            raise ValueError("assembly entry requires a filename, not a path")
        return self


class InstallManifest(ExportModel):
    manifest_version: Literal["0.1"]
    game_version: Text | None = None
    platform: dict[str, str]
    assemblies: tuple[AssemblyEntry, ...] = Field(min_length=1, max_length=64)
    mods: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=512)
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_assemblies(self):
        names = [a.name for a in self.assemblies]
        if len(set(names)) != len(names):
            raise ValueError("duplicate assembly entries")
        return self

    def game_assembly(self) -> AssemblyEntry:
        matches = [a for a in self.assemblies if a.name == "Assembly-CSharp.dll"]
        if len(matches) != 1:
            raise ValueError("manifest requires exactly one Assembly-CSharp.dll entry")
        return matches[0]


def find_managed(root: Path) -> Path:
    """Explicit install, app bundle, or Managed directory; never scan a whole disk."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("select the game, app bundle, or Managed directory")
    candidates = [root, root / "Contents/Resources/Data/Managed"]
    candidates += sorted(root.glob("*_Data/Managed"))
    candidates += sorted(root.glob("*.app/Contents/Resources/Data/Managed"))
    found = set()
    for candidate in candidates:
        path = (candidate / "Assembly-CSharp.dll").resolve()
        if path.is_file():
            if not path.is_relative_to(root):
                raise ValueError("game assembly resolves outside the selected install")
            found.add(path.parent)
    if len(found) != 1:
        raise ValueError("select a directory containing exactly one managed Qud install")
    return found.pop()
