import asyncio
from dataclasses import replace
from typing import Any, Final

from langchain_core.callbacks import UsageMetadataCallbackHandler
from pydantic import ValidationError

from atguigu.agent.harness.errors import (
    AgentExecutionError, AgentOutputValidationError, TerminalErrorCode, CorrectableErrorCode,
)
from atguigu.agent.harness.rules.correction import OutputCorrectionRules
from atguigu.agent.harness.run.context import ContextCompiler
from atguigu.agent.harness.run.metrics import ModelTimingCallback
from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.harness.validator.action import PageActionValidator
from atguigu.agent.harness.validator.output import AgentOutputValidator, ValidatedAgentOutput
from atguigu.agent.llm.adapter import ModelAdapter
from atguigu.agent.llm.output import AgentOutput, ReplyType
from atguigu.agent.multi_agent.budget import BudgetExceeded, RunBudget
from atguigu.agent.multi_agent.state import RunExecution
from atguigu.agent.multi_agent.validation import compose_results
from atguigu.app.schemas.run import AgentRunRequest
from atguigu.common.config import get_settings


class AgentExecutor:
    """为一次请求提供共享预算、专家执行、有限纠错和可信结果汇总。"""

    MAX_CORRECTION_ATTEMPTS: Final[int] = 2

    def __init__(self, agent: Any, output_validator: AgentOutputValidator):
        self.agent = agent
        self.context_compiler = ContextCompiler()
        self.output_validator = output_validator

    async def execute(self, request: AgentRunRequest, runtime_context: AgentRuntimeContext) -> ValidatedAgentOutput:
        settings = get_settings()
        if runtime_context.execution is None:
            runtime_context = replace(runtime_context, execution=RunExecution(budget=RunBudget(
                delegation_limit=settings.multi_agent_delegation_limit,
                task_tool_limit=settings.multi_agent_task_tool_limit,
                total_tool_limit=settings.multi_agent_total_tool_limit,
                supervisor_model_limit=settings.multi_agent_supervisor_model_limit,
                specialist_model_limit=settings.multi_agent_specialist_model_limit,
                timeout_seconds=settings.multi_agent_timeout_seconds,
            ), parallel_limit=settings.multi_agent_parallel_limit))
        messages = self.context_compiler.compile_messages(request)
        usage = UsageMetadataCallbackHandler()
        timing = ModelTimingCallback()
        state = runtime_context.execution
        try:
            try:
                async with asyncio.timeout(state.budget.remaining_seconds):
                    result = await self._execute_with_correction(messages, runtime_context, [usage, timing])
            finally:
                # 退出执行期限后再回收任务，清理过程不受同一个超时二次取消。
                await state.cancel_pending()
        except (BudgetExceeded, TimeoutError):
            result = self._compose(state, '本次查询达到执行限制，未完成部分无法确认，请缩小查询范围。')
        except AgentExecutionError:
            if not state.results:
                raise
            result = self._compose(state, '部分任务未完成，以下仅包含已确认的结果。')
        finally:
            # 失败也保留真实调用指标，不能把一次失败的模型请求显示为零次。
            self.model_metrics = timing.summary()
            self.token_usage = self._summarize_token_usage(usage)
        return result.model_copy(update={'token_usage': self.token_usage,
                                         'domain_results': list(state.results),
                                         'model_metrics': self.model_metrics})

    async def _execute_with_correction(self, messages, context, callbacks):
        current = messages
        for attempt in range(self.MAX_CORRECTION_ATTEMPTS):
            raw = await self._invoke_agent(current, context, callbacks)
            # 子任务各自校验，汇总时不使用整个 Run 的证据重新改写事实。
            if context.execution.results:
                try:
                    supervisor_output = self._normalize_agent_output(raw)
                except AgentOutputValidationError:
                    supervisor_output = None
                return self._compose(context.execution, supervisor_output=supervisor_output)
            try:
                output = self._normalize_agent_output(raw)
                return await self.output_validator.validate(output, context.run_id)
            except AgentOutputValidationError as exc:
                if attempt + 1 == self.MAX_CORRECTION_ATTEMPTS or not OutputCorrectionRules.can_correct(exc):
                    raise AgentExecutionError(exc.code, str(exc)) from exc
                context.execution.finalizing = True
                current = [*raw.get('messages', current), OutputCorrectionRules.build_feedback(exc)]

    async def _invoke_agent(self, messages, context, callbacks):
        try:
            return await self.agent.ainvoke(
                {'messages': messages}, context=context,
                config={'callbacks': callbacks, 'recursion_limit': 100,
                        'metadata': {'agent_code': 'supervisor', 'agent_role': 'supervisor',
                                     'agent_name': '主控智能体'}},
            )
        except (BudgetExceeded, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise AgentExecutionError(TerminalErrorCode.MODEL_CALL_FAILED, '模型调用失败') from exc

    @staticmethod
    def _normalize_agent_output(raw):
        try:
            return ModelAdapter.normalize_output(raw)
        except ValidationError as exc:
            raise AgentOutputValidationError(CorrectableErrorCode.MODEL_OUTPUT_INVALID,
                                             '模型返回的结构化结果不符合约定') from exc

    @staticmethod
    def _compose(state: RunExecution, notice: str | None = None,
                 supervisor_output: AgentOutput | None = None) -> ValidatedAgentOutput:
        output = compose_results(state.results, notice)
        if supervisor_output is not None and supervisor_output.reply_type == ReplyType.REQUEST_HANDOFF:
            # 保留主控的转人工意图，业务正文仍使用已验证的专家结果。
            output = AgentOutput(
                reply_type=ReplyType.REQUEST_HANDOFF,
                reply_content=output.reply_content,
                handoff_request=supervisor_output.handoff_request,
            )
        calls = tuple(call for result in state.results for call in state.evidence.get(result.task_id, ()))
        page_action = PageActionValidator().validate(output.page_action_request, calls)
        return ValidatedAgentOutput(**output.model_dump(), page_action=page_action)

    @staticmethod
    def _summarize_token_usage(usage):
        values = list(usage.usage_metadata.values())
        return {key: sum(item.get(key, 0) for item in values) for key in ('input_tokens', 'output_tokens')}
