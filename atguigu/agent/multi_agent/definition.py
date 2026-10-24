from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atguigu.agent.llm.output import PageActionRequest


class DomainStatus(StrEnum):
    SUCCESS = 'SUCCESS'
    NEEDS_CLARIFICATION = 'NEEDS_CLARIFICATION'
    BUSINESS_FAILED = 'BUSINESS_FAILED'
    SERVICE_FAILED = 'SERVICE_FAILED'


class DomainFinding(BaseModel):
    """一条结论只能引用一次成功查询及其中一个业务对象。"""

    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=400)
    evidence_id: str = Field(min_length=1, description='工具响应中的 evidence_id，禁止编造')
    resource_id: str | None = Field(description='必须显式提供：业务结论填写工具返回的对象编号；政策引用填写null')


class DomainAgentOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: DomainStatus
    findings: list[DomainFinding] = Field(default_factory=list, max_length=4)
    clarification: str | None = Field(default=None, max_length=300)
    page_action_request: PageActionRequest | None = None

    @model_validator(mode='after')
    def check_status(self):
        if self.status == DomainStatus.NEEDS_CLARIFICATION and not self.clarification:
            raise ValueError('澄清必须提供具体问题')
        if self.status != DomainStatus.SUCCESS and self.page_action_request is not None:
            raise ValueError('只有成功结果可以提出页面动作')
        return self


class DomainResult(BaseModel):
    """服务端生成的领域结果；模型不能直接构造证据集合。"""

    task_id: str
    agent_code: str
    status: DomainStatus
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    failed_tools: list[str] = Field(default_factory=list)
    page_action_request: PageActionRequest | None = None
