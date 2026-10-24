"""模型设置写入的版本契约,阶段面板与收藏设置共用。"""

from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictInt

from kb import llm


class SettingsWriteRequest(BaseModel):
    revision: StrictInt | None = Field(default=None, ge=0)
    operation_id: str | None = Field(default=None, min_length=1, max_length=128)


def write_settings(body: SettingsWriteRequest, action):
    revision = body.revision if body.revision is not None else llm.settings_revision()
    try:
        result = action(expected_revision=revision, operation_id=body.operation_id)
    except llm.SettingsConflict as error:
        raise HTTPException(409, str(error)) from error
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return result, revision + 1


def operation_status(data: dict, operation_id: str) -> dict:
    revision = (data.get("settings_operations") or {}).get(operation_id)
    return {"id": operation_id, "status": "committed" if revision is not None else "unconfirmed",
            "revision": revision}
