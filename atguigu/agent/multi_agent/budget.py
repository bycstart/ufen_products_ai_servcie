"""请求级执行预算；计数过程不等待，异步任务在同一事件循环内共享。"""

import time
from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    """稳定的执行控制信号，不包含任务正文或模型服务敏感信息。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class RunBudget:
    """主控、专家和纠错共享同一份预算；不同请求各自创建。"""

    delegation_limit: int = 6
    task_tool_limit: int = 8
    total_tool_limit: int = 24
    supervisor_model_limit: int = 8
    specialist_model_limit: int = 10
    timeout_seconds: float = 120
    _deadline: float = field(init=False, repr=False)
    _delegations: int = field(default=0, init=False, repr=False)
    _total_tools: int = field(default=0, init=False, repr=False)
    _task_tools: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _supervisor_models: int = field(default=0, init=False, repr=False)
    _specialist_models: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        self._deadline = time.monotonic() + self.timeout_seconds

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def check_deadline(self) -> None:
        if time.monotonic() >= self._deadline:
            raise BudgetExceeded('run_timeout')

    def consume_delegation(self) -> None:
        self.check_deadline()
        if self._delegations >= self.delegation_limit:
            raise BudgetExceeded('delegation_budget_exceeded')
        self._delegations += 1

    def consume_tool(self, task_id: str) -> None:
        self.check_deadline()
        if self._total_tools >= self.total_tool_limit:
            raise BudgetExceeded('total_tool_budget_exceeded')
        task_count = self._task_tools.get(task_id, 0)
        if task_count >= self.task_tool_limit:
            raise BudgetExceeded('task_tool_budget_exceeded')
        self._task_tools[task_id] = task_count + 1
        self._total_tools += 1

    def consume_model(self, scope_id: str, *, supervisor: bool = False) -> None:
        self.check_deadline()
        if supervisor:
            if self._supervisor_models >= self.supervisor_model_limit:
                raise BudgetExceeded('supervisor_model_budget_exceeded')
            self._supervisor_models += 1
            return
        count = self._specialist_models.get(scope_id, 0)
        if count >= self.specialist_model_limit:
            raise BudgetExceeded('specialist_model_budget_exceeded')
        self._specialist_models[scope_id] = count + 1
