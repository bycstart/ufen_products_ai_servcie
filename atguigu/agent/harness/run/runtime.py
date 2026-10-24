from dataclasses import dataclass, field, replace
from uuid import uuid4

from atguigu.agent.multi_agent.state import RunExecution


@dataclass(frozen=True)
class AgentRuntimeContext:
    """提供给 Agent 和业务工具的单次运行信息"""

    run_id: str
    conversation_id: str
    user_id: str
    access_token: str = field(repr=False)
    execution: RunExecution | None = field(default=None, repr=False, compare=False)
    task_id: str | None = None
    agent_code: str = 'supervisor'
    allowed_tools: tuple[str, ...] = ()

    def for_task(self, agent_code: str, allowed_tools: tuple[str, ...] = ()) -> 'AgentRuntimeContext':
        """子任务继承可信身份与共享预算，同时获得独立的任务编号。"""
        return replace(self, task_id=uuid4().hex, agent_code=agent_code, allowed_tools=allowed_tools)
