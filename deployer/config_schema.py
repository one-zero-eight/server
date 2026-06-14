from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class SettingBaseModel(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")


class DeployEnvironmentConfig(SettingBaseModel):
    host: str | None = None
    docker_deploy_dir: Path | None = None
    static_dir: Path | None = None


class Settings(SettingBaseModel):
    schema_: str | None = Field(None, alias="$schema")
    webhook_secret: str
    repositories: dict[str, dict[str, DeployEnvironmentConfig]]

    @classmethod
    def from_yaml(cls, path: Path) -> "Settings":
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}
        return cls.model_validate(data)

    @classmethod
    def save_schema(cls, path: Path) -> None:
        with path.open("w", encoding="utf-8") as file:
            schema = {"$schema": "https://json-schema.org/draft-07/schema", **cls.model_json_schema()}
            yaml.dump(schema, file, sort_keys=False)
