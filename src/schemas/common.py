"""Shared validation primitives; source text is never silently rewritten."""

import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, BeforeValidator,
    ConfigDict, Field, StrictInt, StrictStr,
)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


NonBlankStr = Annotated[StrictStr, AfterValidator(non_blank)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]


def datetime_input(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        return value
    raise ValueError("use an RFC 3339 datetime with timezone, for example 2026-11-02T18:00:00+08:00")


Timestamp = Annotated[AwareDatetime, BeforeValidator(datetime_input)]


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


TimezoneName = Literal["Asia/Shanghai"]
BUSINESS_TIMEZONE = timezone(timedelta(hours=8))
