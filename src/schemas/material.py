"""Incomplete source records and complete materials have different contracts."""

from typing import Literal

from pydantic import StrictStr, field_validator, model_validator

from .common import ContractModel, NonBlankStr

AuthorizationStatus = Literal["authorized", "unknown", "denied"]


class MaterialPatch(ContractModel):
    """Trusted supplemental fields; absence is retained for the review service."""

    title: StrictStr | None = None
    content: StrictStr | None = None
    channel: StrictStr | None = None
    authorization_status: AuthorizationStatus | None = None
    authorization_evidence: StrictStr | None = None

    @field_validator("authorization_status", mode="before")
    @classmethod
    def blank_authorization_is_missing(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class MaterialRecord(MaterialPatch):
    """A readable source record may still lack business-required metadata."""

    material_id: NonBlankStr


class Material(ContractModel):
    """Complete metadata; authorization and content risks still require review."""

    material_id: NonBlankStr
    title: NonBlankStr
    content: NonBlankStr
    channel: NonBlankStr
    authorization_status: AuthorizationStatus
    authorization_evidence: NonBlankStr | None = None

    @model_validator(mode="after")
    def check_evidence(self) -> "Material":
        if self.authorization_status == "authorized" and self.authorization_evidence is None:
            raise ValueError("authorized material requires authorization_evidence")
        return self
