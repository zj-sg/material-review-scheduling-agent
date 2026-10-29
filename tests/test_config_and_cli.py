import json

import pytest
from pydantic import ValidationError

from src.config import PROJECT_ROOT, Settings
from src.main import main
from conftest import ROOT, read_example


def test_default_configuration_needs_no_model_key_and_creates_no_database(tmp_path):
    database = tmp_path / "not-created" / "drafts.sqlite3"
    settings = Settings(_env_file=None, database_path=database)
    assert settings.timezone == "Asia/Shanghai"
    assert settings.examples_dir == PROJECT_ROOT / "examples"
    assert not database.parent.exists()


def test_environment_overrides_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MATERIAL_AGENT_TIMEZONE=Asia/Shanghai\nMATERIAL_AGENT_DATABASE_PATH=local/drafts.sqlite3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MATERIAL_AGENT_DATABASE_PATH", "session/drafts.sqlite3")
    settings = Settings(_env_file=env_file)
    assert settings.timezone == "Asia/Shanghai"
    assert settings.database_path == PROJECT_ROOT / "session" / "drafts.sqlite3"


def test_relative_configuration_paths_are_independent_of_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, database_path="data/drafts.sqlite3", examples_dir="examples")
    assert settings.database_path == PROJECT_ROOT / "data" / "drafts.sqlite3"
    assert settings.examples_dir == PROJECT_ROOT / "examples"


@pytest.mark.parametrize("field,value", [("timezone", "Bad/Zone"), ("database_path", " "), ("examples_dir", "")])
def test_invalid_configuration_is_explicit(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_dotenv_example_is_loadable_without_secrets():
    settings = Settings(_env_file=ROOT / ".env.example")
    assert settings.model is None
    assert settings.api_key is None
    assert "api_key" not in settings.model_dump()


def test_cli_validates_required_and_extended_scenarios(capsys):
    assert main(["validate-examples", "--directory", str(ROOT / "examples")]) == 0
    output = capsys.readouterr().out
    assert "7 类场景、9 个请求步骤" in output
    assert "未执行真实审核或保存" in output


def test_cli_rejects_missing_directory(tmp_path, capsys):
    assert main(["validate-examples", "--directory", str(tmp_path / "missing")]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_cli_reports_request_field_location(tmp_path, capsys):
    request = read_example("01_normal")["steps"][0]["request"]
    del request["request_id"]
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    assert main(["validate-request", str(path)]) == 1
    error = capsys.readouterr().err
    assert "request_id" in error
    assert "Field required" in error


def test_cli_rejects_invalid_json(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    assert main(["validate-request", str(path)]) == 1
    assert "JSON" in capsys.readouterr().err
