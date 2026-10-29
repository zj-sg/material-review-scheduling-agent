"""Run-scoped human updates, invalidated when the underlying material changes."""

import hashlib
import json
from copy import deepcopy

from src.schemas.material import MaterialRecord
from .material_service import merge_missing_fields


def material_signature(material):
    value = material.model_dump(mode="json") if material is not None else None
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class RunMaterialSource:
    def __init__(self, source, material_id, updates=None, signature=None, human_actions=None):
        self.source, self.material_id = source, material_id
        self.updates = deepcopy(updates or {})
        self.signature = signature
        self.human_actions = deepcopy(human_actions or [])
        self.last_material = None
        self.base_signature = None
        self.updates_ignored = False

    @property
    def reference(self):
        return self.source.reference + ("|run-human-input" if self.updates else "")

    def read(self, material_id):
        if material_id != self.material_id:
            raise ValueError("run material source cannot access a different material")
        material = self.source.read(material_id)
        self.base_signature = material_signature(material)
        if self.updates and self.base_signature != self.signature:
            self.updates_ignored = True
        if self.updates_ignored:
            for action in self.human_actions:
                if action.get("status") in {"submitted", "applied"}:
                    action["status"] = "invalidated_source_changed"
        elif material is not None and self.updates:
            material = MaterialRecord.model_validate({**material.model_dump(), **self.updates})
            for action in self.human_actions:
                if action.get("status") == "submitted":
                    action["status"] = "applied"
        self.last_material = material
        return material

    def supplement(self, material_id):
        if material_id != self.material_id:
            raise ValueError("run material source cannot access a different material")
        patch = self.source.supplement(material_id)
        if self.last_material is not None:
            self.last_material = merge_missing_fields(self.last_material, patch)
        return patch
