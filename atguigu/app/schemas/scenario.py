"""客服场景测试的请求与用例定义。"""
from pydantic import BaseModel, Field
from typing import Any


class ScenarioCase(BaseModel):
    case_id: str
    category: str
    question: str
    expected_tools: tuple[str, ...]
    expected_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    expected_reply_types: tuple[str, ...] = ('ANSWER',)
    allowed_failure_codes: tuple[str, ...] = ()


class ScenarioTestRequest(BaseModel):
    case_id: str = Field(min_length=1, max_length=20, description='GET /cases 返回的用例编号')
