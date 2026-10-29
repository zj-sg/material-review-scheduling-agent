"""Explicit review rules; material text is treated only as data."""

from src.schemas.material import MaterialRecord
from src.schemas.request import ScheduleRequest
from src.schemas.result import ReviewResult, Risk
from src.schemas.rules import MaterialField, RuleConfig

from .material_service import is_missing

CORE_REQUIRED_FIELDS: tuple[MaterialField, ...] = (
    "material_id", "title", "content", "channel", "authorization_status",
)


class RuleService:
    def check(
        self, material: MaterialRecord, request: ScheduleRequest, rules: RuleConfig,
    ) -> ReviewResult:
        if material.material_id != request.material_id:
            raise ValueError("material does not belong to this request")
        required_fields = list(dict.fromkeys((*CORE_REQUIRED_FIELDS, *rules.required_material_fields)))
        missing: list[MaterialField] = [
            field for field in required_fields
            if is_missing(getattr(material, field))
        ]
        if material.authorization_status == "authorized" and is_missing(material.authorization_evidence):
            if "authorization_evidence" not in missing:
                missing.append("authorization_evidence")

        risks: list[Risk] = []
        if material.authorization_status == "denied":
            risks.append(Risk(
                rule_id="R_AUTH_BLOCK", level="block", field="authorization_status",
                reason="素材明确未授权，阻断后续排期。",
            ))
        elif material.authorization_status == "unknown":
            risks.append(Risk(
                rule_id="R_AUTH_REVIEW", level="review", field="authorization_status",
                reason="素材授权状态待核实，需要可信授权证据。",
            ))
        if material.content is not None:
            for phrase in rules.blocked_phrases:
                if phrase in material.content:
                    risks.append(Risk(
                        rule_id="R_TEXT_BLOCK", level="block", field="content",
                        reason=f"命中禁止表述：{phrase}。",
                    ))
        channel_reasons = []
        if request.channel not in rules.allowed_channels:
            channel_reasons.append(f"请求渠道 {request.channel} 不在允许列表中")
        if not is_missing(material.channel) and material.channel != request.channel:
            channel_reasons.append(f"素材渠道 {material.channel} 与请求渠道 {request.channel} 不一致")
        if channel_reasons:
            risks.append(Risk(
                rule_id="R_CHANNEL", level="block", field="channel",
                reason="；".join(channel_reasons) + "。",
            ))

        if any(risk.level == "block" for risk in risks):
            status = "blocked"
        elif missing:
            status = "needs_info"
        elif risks:
            status = "needs_review"
        else:
            status = "passed"
        return ReviewResult(status=status, missing_fields=missing, risks=risks)
