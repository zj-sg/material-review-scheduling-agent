"""Read trusted local material data without changing the input files."""

import json
from pathlib import Path
from typing import Protocol

from pydantic import Field, model_validator

from src.schemas.common import ContractModel, NonBlankStr
from src.schemas.material import MaterialPatch, MaterialRecord


def is_missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def merge_missing_fields(material: MaterialRecord, patch: MaterialPatch) -> MaterialRecord:
    values = material.model_dump()
    for field, value in patch.model_dump(exclude_unset=True).items():
        if is_missing(values.get(field)) and not is_missing(value):
            values[field] = value
    return MaterialRecord.model_validate(values)


class MaterialCatalog(ContractModel):
    materials: list[MaterialRecord]
    supplemental_materials: dict[NonBlankStr, MaterialPatch] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_material_ids(self) -> "MaterialCatalog":
        identifiers = [material.material_id for material in self.materials]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("material_id values must be unique in the material source")
        return self


class MaterialSource(Protocol):
    @property
    def reference(self) -> str: ...

    def read(self, material_id: str) -> MaterialRecord | None: ...

    def supplement(self, material_id: str) -> MaterialPatch: ...


class JsonMaterialService:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    @property
    def reference(self) -> str:
        return f"local-json:{self.path.as_posix()}"

    def _load_catalog(self) -> MaterialCatalog:
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("material source must be a JSON object")
        if "initial_state" in raw:
            state = raw["initial_state"]
            if not isinstance(state, dict):
                raise ValueError("initial_state must be an object")
            raw = {
                "materials": state.get("materials"),
                "supplemental_materials": state.get("supplemental_materials", {}),
            }
        return MaterialCatalog.model_validate(raw)

    def read(self, material_id: str) -> MaterialRecord | None:
        catalog = self._load_catalog()
        return next(
            (material for material in catalog.materials if material.material_id == material_id),
            None,
        )

    def supplement(self, material_id: str) -> MaterialPatch:
        return self._load_catalog().supplemental_materials.get(material_id, MaterialPatch())
