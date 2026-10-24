import asyncio
import logging
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import HumanMessage

from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.multi_agent.budget import BudgetExceeded
from atguigu.agent.multi_agent.definition import DomainAgentOutput, DomainResult, DomainStatus
from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
from atguigu.agent.multi_agent.validation import validate_domain_output

logger = logging.getLogger(__name__)


class DelegationService:
    """把领域专家包装为主控工具，统一处理身份、预算与结果校验。"""

    def __init__(self, agents: dict[str, Any]):
        self.agents = agents

    async def delegate(self, code: str, task: str, context: AgentRuntimeContext, config: dict) -> DomainResult:
        if context.execution is None:
            raise RuntimeError('缺少请求执行上下文')
        state = context.execution
        key = (code, task.strip())
        state.budget.check_deadline()
        if key in state.cache:
            return state.cache[key]
        if state.finalizing:
            raise BudgetExceeded('finalization_only')
        # 检查与登记之间没有 await，同一事件循环不会重复创建相同委派。
        pending = state.pending_delegations.get(key)
        if pending is None:
            pending = asyncio.create_task(self._run_pending(code, task, context, config, key))
            state.pending_delegations[key] = pending
        # 一个重复调用被取消，不应取消其他调用仍在等待的共享任务。
        return await asyncio.shield(pending)

    async def _run_pending(self, code, task, context, config, key):
        state = context.execution
        try:
            # 执行期限由根请求统一控制，避免多个超时打断同一任务的清理。
            async with state.semaphore:
                return await self._delegate(code, task, context, config)
        finally:
            state.pending_delegations.pop(key, None)

    async def _delegate(self, code: str, task: str, context: AgentRuntimeContext, config: dict) -> DomainResult:
        state = context.execution
        state.budget.check_deadline()
        key = (code, task.strip())
        if key in state.cache:
            return state.cache[key]
        if state.finalizing:
            raise BudgetExceeded('finalization_only')
        state.budget.consume_delegation()
        child = context.for_task(code, DOMAIN_SPECS[code].tools)
        state.evidence[child.task_id] = []
        logger.info('subagent_start run_id=%s task_id=%s agent=%s', context.run_id, child.task_id, code)
        stage = 'model'
        try:
            raw = await self.agents[code].ainvoke(
                {'messages': [HumanMessage(content=task.strip()[:6000])]},
                context=child,
                # 不把父图 configurable/checkpoint 信息传给独立专家。
                config={'callbacks': config.get('callbacks', []), 'recursion_limit': 100,
                        # 独立生成专家标签，覆盖父图主控标签，不继承父图检查点。
                        'metadata': {'agent_code': code, 'agent_role': 'specialist',
                                     'agent_name': f'{DOMAIN_SPECS[code].label}子智能体',
                                     'task_id': child.task_id}},
            )
            stage = 'output_parse'
            output = DomainAgentOutput.model_validate(raw['structured_response'])
            stage = 'validation'
            result = validate_domain_output(output, tuple(state.evidence[child.task_id]), code, child.task_id)
        except asyncio.CancelledError:
            raise
        except (BudgetExceeded, TimeoutError):
            result = DomainResult(task_id=child.task_id, agent_code=code, status=DomainStatus.SERVICE_FAILED,
                                  summary='本领域查询达到执行限制，未完成部分无法确认。')
        except Exception as exc:
            # 只记录标识，避免模型服务异常原文泄漏认证信息。
            # 验证阶段的普通 ValueError 仅包含服务端固定原因，不记录模型/供应商异常原文。
            reason = str(exc) if stage == 'validation' and type(exc) is ValueError else ''
            logger.warning('subagent_failed run_id=%s task_id=%s agent=%s stage=%s error_type=%s reason=%s',
                           context.run_id, child.task_id, code, stage, type(exc).__name__, reason)
            result = DomainResult(task_id=child.task_id, agent_code=code, status=DomainStatus.SERVICE_FAILED,
                                  summary='本领域暂时无法取得可靠结果，请稍后重试。')
        state.results.append(result)
        state.cache[key] = result
        logger.info('subagent_end run_id=%s task_id=%s agent=%s status=%s',
                    context.run_id, child.task_id, code, result.status)
        return result

    def tools(self):
        def build(code):
            spec = DOMAIN_SPECS[code]

            @tool(f'consult_{code}', description=f'委派{spec.label}专家：{spec.description}')
            async def consult(task: str, runtime: ToolRuntime[AgentRuntimeContext]) -> str:
                result = await self.delegate(code, task, runtime.context, runtime.config)
                return result.model_dump_json()

            return consult

        return [build(code) for code in DOMAIN_SPECS]
