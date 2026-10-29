"""Rule checking is a tool with an explicit failure path."""

from src.schemas.material import MaterialRecord
from src.schemas.request import ScheduleRequest
from src.schemas.result import ErrorDetail, ReviewResult
from src.schemas.review import ToolResult
from src.schemas.rules import RuleConfig
from src.services.rule_service import RuleService


def check_rules(
    material: MaterialRecord, request: ScheduleRequest, rules: RuleConfig,
    service: RuleService | None = None,
) -> ToolResult[ReviewResult]:
    try:
        result = (service if service is not None else RuleService()).check(material, request, rules)
        checked = ReviewResult.model_validate(
            result.model_dump() if isinstance(result, ReviewResult) else result
        )
        return ToolResult[ReviewResult](
            status="ok", data=checked, source=f"rules:{rules.version}", error=None,
        )
    except Exception as exc:
        return ToolResult[ReviewResult](
            status="error", data=None, source=None,
            error=ErrorDetail(
                code="TOOL_ERROR", message=f"规则检查失败（{type(exc).__name__}），不能视为审核通过。",
            ),
        )
