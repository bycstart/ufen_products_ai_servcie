import json
import logging
import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from pydantic import TypeAdapter, ValidationError

from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.harness.tools.output import ToolResult, ToolFailureType, ToolFailureCode
from atguigu.app.repositories.tool import ToolCallRepository
from atguigu.infrastructure.db import session_factory
from atguigu.models.models import AgentToolCall
from atguigu.agent.harness.tools.snapshot import ToolCallSnapshot
from atguigu.agent.multi_agent.budget import BudgetExceeded

ToolOperation = Callable[[], Awaitable[Any]]
logger = logging.getLogger(__name__)


class ToolExecutor:
    """统一记录、执行并校验 Agent 业务工具调用"""

    def __init__(self):
        self.session_factory = session_factory

    async def execute(
            self,
            runtime_context: AgentRuntimeContext,
            tool_call_id: str,
            tool_name: str,
            arguments: dict[str, Any],
            operation: ToolOperation,
            output_schema: Any
    ) -> str:
        """独立只读查询并行执行；预算扣减无 await，证据按子任务隔离。"""
        return await self._execute(runtime_context, tool_call_id, tool_name, arguments, operation, output_schema)

    async def _execute(
            self,
            runtime_context: AgentRuntimeContext,
            tool_call_id: str,
            tool_name: str,
            arguments: dict[str, Any],
            operation: ToolOperation,
            output_schema: Any
    ) -> str:
        """执行一次业务工具调用并返回 Agent 可读取的 JSON"""
        state = runtime_context.execution
        query_key = json.dumps([tool_name, arguments], sort_keys=True, ensure_ascii=False)
        if state is not None:
            if not runtime_context.task_id or tool_name not in runtime_context.allowed_tools:
                raise ValueError('当前 Agent 无权调用该业务工具')
            state.budget.consume_tool(runtime_context.task_id)
            # 存储标识隔离子任务，原始模型调用 ID 仍由 ToolNode 用于消息配对。
            tool_call_id = f'{runtime_context.task_id}:{hashlib.sha256(tool_call_id.encode()).hexdigest()}'
        # 1. 保存工具名称和调用参数
        await self._create_tool_call(
            runtime_context,
            tool_call_id,
            tool_name,
            arguments
        )

        # 2. 执行业务请求并统计调用耗时
        started_at = perf_counter()
        cached_result = state.failed_queries.get(query_key) if state is not None else None
        try:
            if cached_result is not None:
                response_data = cached_result
            else:
                response_data = await operation()
        except (BudgetExceeded, asyncio.CancelledError):
            raise
        except Exception:
            # 3. 将调用异常转换成安全的失败结果
            logger.warning("业务工具调用失败 [%s] run_id=%s", tool_name, runtime_context.run_id)
            tool_result = ToolResult[Any](
                success=False,
                code=ToolFailureCode.TOOL_CALL_FAILED,
                message="暂时无法取得可靠的业务数据",
                failure_type=ToolFailureType.SERVICE_CALL
            )
        else:
            # 缓存是已校验的内部结果，保留服务失败、契约失败等原始分类。
            tool_result = (ToolResult[Any].model_validate(cached_result) if cached_result is not None
                           else self._validate_result(response_data, output_schema))

        # 4. 保存最终结果、成功状态和调用耗时
        latency_ms = int((perf_counter() - started_at) * 1000)
        await self._save_tool_result(
            runtime_context.run_id,
            tool_call_id,
            tool_result,
            latency_ms
        )
        payload = tool_result.model_dump(mode='json')
        if state is not None:
            state.evidence.setdefault(runtime_context.task_id, []).append(ToolCallSnapshot(
                tool_name=tool_name, arguments=dict(arguments), result=payload,
                success=tool_result.success, failure_type=tool_result.failure_type,
                tool_call_id=tool_call_id,
            ))
            if not tool_result.success:
                state.failed_queries[query_key] = payload
        payload = {**payload, 'evidence_id': tool_call_id}
        # 5. 序列化成模型可以读取的 JSON 文本
        return json.dumps(
            payload,
            ensure_ascii=False
        )

    async def _create_tool_call(
            self,
            runtime_context: AgentRuntimeContext,
            tool_call_id: str,
            tool_name: str,
            arguments: dict[str, Any]
    ) -> None:
        """在执行请求前保存工具名称和调用参数"""
        async with self.session_factory() as session:
            repository = ToolCallRepository(session)

            # 1. 创建初始工具调用记录
            repository.add(
                AgentToolCall(
                    run_id=runtime_context.run_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments
                )
            )
            # 2. 提交
            await session.commit()

    async def _save_tool_result(
            self,
            run_id: str,
            tool_call_id: str,
            tool_result: ToolResult[Any],
            latency_ms: int
    ):
        """保存工具执行结果、成功状态和耗时"""
        async with self.session_factory() as session:
            repository = ToolCallRepository(session)

            # 1. 查询工具调用记录
            tool_call = await repository.find_by_run_and_call_id(
                run_id,
                tool_call_id
            )
            # 2. 修改工具记录信息
            tool_call.result = tool_result.model_dump(mode="json")
            tool_call.success = tool_result.success
            tool_call.latency_ms = latency_ms

            # 3. 提交
            await session.commit()

    @staticmethod
    def _validate_result(
            response_data: Any,
            output_schema: Any
    ) -> ToolResult[Any]:
        """校验统一结果结构和成功结果中的业务数据"""
        try:
            # 1. 校验所有工具共同使用的外层结果结构
            tool_result = ToolResult[Any].model_validate(
                response_data
            )
            # 2. 业务失败结果不再校验 data
            if not tool_result.success:
                return tool_result.model_copy(
                    update={
                        "failure_type": ToolFailureType.BUSINESS
                    }
                )

            # 3. 按当前工具的数据模型校验并保留内部业务数据
            data_adapter = TypeAdapter(output_schema)
            validated_data = data_adapter.validate_python(
                tool_result.data
            )
            return tool_result.model_copy(
                update={
                    "data": data_adapter.dump_python(
                        validated_data,
                        mode="json"
                    ),
                    "failure_type": None
                }
            )
        except ValidationError:
            # 4. 将不符合数据约定的响应转换成失败结果
            return ToolResult[Any](
                success=False,
                code=ToolFailureCode.INVALID_TOOL_RESULT,
                message="业务工具返回的数据不符合约定",
                failure_type=ToolFailureType.CONTRACT
            )
