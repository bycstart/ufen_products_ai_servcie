import asyncio
from dataclasses import dataclass, field
from atguigu.agent.multi_agent.budget import RunBudget
from atguigu.agent.harness.tools.snapshot import ToolCallSnapshot
from atguigu.agent.multi_agent.definition import DomainResult


@dataclass
class RunExecution:
    """每个请求新建；仅在该 Run 的主控、专家和纠错之间共享。"""

    budget: RunBudget = field(default_factory=RunBudget)
    evidence: dict[str, list['ToolCallSnapshot']] = field(default_factory=dict)
    results: list['DomainResult'] = field(default_factory=list)
    cache: dict[tuple[str, str], 'DomainResult'] = field(default_factory=dict)
    failed_queries: dict[str, dict] = field(default_factory=dict)
    finalizing: bool = False
    parallel_limit: int = 3
    pending_delegations: dict[tuple[str, str], asyncio.Task] = field(default_factory=dict, repr=False)
    semaphore: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self):
        if self.parallel_limit < 1:
            raise ValueError('子智能体并发上限必须大于零')
        self.semaphore = asyncio.Semaphore(self.parallel_limit)

    async def cancel_pending(self):
        """请求结束前回收在途任务，避免结果返回后仍有子任务写入。"""
        self.finalizing = True
        tasks = tuple(self.pending_delegations.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

