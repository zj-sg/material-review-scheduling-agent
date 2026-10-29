"""Tool boundaries convert source failures into explicit receipts."""

from pydantic import TypeAdapter

from src.schemas.common import NonBlankStr
from src.schemas.material import MaterialPatch, MaterialRecord
from src.schemas.result import ErrorDetail
from src.schemas.review import ToolResult
from src.schemas.rules import MaterialField
from src.services.material_service import MaterialSource, is_missing


def read_material(material_id: str, service: MaterialSource) -> ToolResult[MaterialRecord]:
    try:
        TypeAdapter(NonBlankStr).validate_python(material_id)
        reference = service.reference + f"#materials/{material_id}"
        raw = service.read(material_id)
        if raw is None:
            return ToolResult[MaterialRecord](
                status="not_found", data=None, source=reference,
                error=ErrorDetail(code="MATERIAL_NOT_FOUND", message=f"素材 {material_id} 不存在。"),
            )
        material = MaterialRecord.model_validate(
            raw.model_dump(exclude_unset=True) if isinstance(raw, MaterialRecord) else raw
        )
        if material.material_id != material_id:
            raise ValueError("source returned a different material")
        return ToolResult[MaterialRecord](status="ok", data=material, source=reference, error=None)
    except Exception as exc:
        return ToolResult[MaterialRecord](
            status="error", data=None, source=None,
            error=ErrorDetail(
                code="TOOL_ERROR", message=f"素材读取失败（{type(exc).__name__}），未得出审核结论。",
            ),
        )


def supplement_material(
    material_id: str, missing_fields: list[MaterialField], service: MaterialSource,
) -> ToolResult[MaterialPatch]:
    try:
        TypeAdapter(NonBlankStr).validate_python(material_id)
        fields = TypeAdapter(list[MaterialField]).validate_python(missing_fields)
        if not fields or "material_id" in fields or len(set(fields)) != len(fields):
            raise ValueError("supplement fields must be nonempty, unique metadata fields")
        reference = service.reference + f"#supplemental_materials/{material_id}"
        raw = service.supplement(material_id)
        patch = MaterialPatch.model_validate(
            raw.model_dump(exclude_unset=True) if isinstance(raw, MaterialPatch) else raw
        )
        values = {
            field: value for field, value in patch.model_dump(exclude_unset=True).items()
            if field in fields and not is_missing(value)
        }
        if not values:
            return ToolResult[MaterialPatch](status="empty", data=None, source=reference, error=None)
        return ToolResult[MaterialPatch](
            status="ok", data=MaterialPatch.model_validate(values), source=reference, error=None,
        )
    except Exception as exc:
        return ToolResult[MaterialPatch](
            status="error", data=None, source=None,
            error=ErrorDetail(
                code="TOOL_ERROR", message=f"素材补查失败（{type(exc).__name__}），未将缺失信息视为已补齐。",
            ),
        )
