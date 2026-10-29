"""Local settings. Constructing settings never creates files or opens a database."""

from pathlib import Path

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schemas.common import NonBlankStr, TimezoneName

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MATERIAL_AGENT_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    timezone: TimezoneName = "Asia/Shanghai"
    database_path: Path = PROJECT_ROOT / "data" / "drafts.sqlite3"
    examples_dir: Path = PROJECT_ROOT / "examples"
    api_data_path: Path = PROJECT_ROOT / "data" / "api-data.json"
    api_offline: bool = False
    max_model_calls: int = Field(default=12, ge=1, le=100)
    model: NonBlankStr | None = None
    base_url: AnyHttpUrl | None = None
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @field_validator("api_key")
    @classmethod
    def nonempty_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            return None
        return value

    @field_validator("database_path", "examples_dir", "api_data_path", mode="before")
    @classmethod
    def resolve_project_path(cls, value: object) -> Path:
        if not isinstance(value, (str, Path)):
            raise ValueError("path must be a string or Path")
        if isinstance(value, str) and not value.strip():
            raise ValueError("path must not be blank")
        path = Path(value)
        return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def load_settings() -> Settings:
    return Settings()
