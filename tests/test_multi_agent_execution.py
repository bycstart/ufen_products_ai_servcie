import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.multi_agent.state import RunExecution


class ToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_records_scoped_evidence_and_keeps_model_call_id_external(self):
        from atguigu.agent.harness.tools.executor import ToolExecutor
        from atguigu.agent.harness.tools.output import ProductStockData
        executor = ToolExecutor()
        executor._create_tool_call = AsyncMock()
        executor._save_tool_result = AsyncMock()
        state = RunExecution()
        ctx = AgentRuntimeContext('r', 'c', 'u', 'secret', execution=state).for_task(
            'product', ('get_product_stock',))
        operation = AsyncMock(return_value={'success': True, 'code': 'OK', 'message': 'OK',
            'data': {'product_id': 'PRODUCT_001', 'stock': 2, 'status': 'on_sale'}})
        result = json.loads(await executor.execute(ctx, 'same-model-id', 'get_product_stock',
            {'product_id': 'PRODUCT_001'}, operation, ProductStockData))
        self.assertIn('evidence_id', result)
        self.assertNotEqual(result['evidence_id'], 'same-model-id')
        self.assertEqual(state.evidence[ctx.task_id][0].tool_call_id, result['evidence_id'])
        self.assertEqual(executor._save_tool_result.call_args.args[1], result['evidence_id'])

    async def test_forbidden_tool_does_not_call_backend(self):
        from atguigu.agent.harness.tools.executor import ToolExecutor
        executor = ToolExecutor()
        executor._create_tool_call = AsyncMock()
        executor._save_tool_result = AsyncMock()
        ctx = AgentRuntimeContext('r', 'c', 'u', 's', execution=RunExecution()).for_task('policy', ('search_knowledge',))
        operation = AsyncMock()
        with self.assertRaises(ValueError):
            await executor.execute(ctx, 'id', 'get_order', {}, operation, dict)
        operation.assert_not_awaited()

    async def test_failed_query_not_reissued_in_another_task(self):
        from atguigu.agent.harness.tools.executor import ToolExecutor
        executor = ToolExecutor()
        executor._create_tool_call = AsyncMock()
        executor._save_tool_result = AsyncMock()
        ctx = AgentRuntimeContext('r', 'c', 'u', 's', execution=RunExecution())
        operation = AsyncMock(side_effect=RuntimeError('down'))
        for code in ('order', 'logistics'):
            child = ctx.for_task(code, ('get_order',))
            await executor.execute(child, 'id', 'get_order', {'order_id': 'ORDER_001'}, operation, dict)
        self.assertEqual(operation.await_count, 1)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_domain_results_override_supervisor_hallucination(self):
        from atguigu.agent.harness.run.executor import AgentExecutor
        from atguigu.agent.multi_agent.definition import DomainResult
        agent = AsyncMock()
        async def invoke(*args, **kwargs):
            kwargs['context'].execution.results.extend([
                DomainResult(task_id='a', agent_code='policy', status='SUCCESS', summary='七天内可申请退货。'),
                DomainResult(task_id='b', agent_code='logistics', status='SERVICE_FAILED', summary='物流查询失败。'),
            ])
            return {'structured_response': {'reply_type': 'ANSWER', 'reply_content': '您的订单已经退款。'}}
        agent.ainvoke.side_effect = invoke
        executor = AgentExecutor(agent, AsyncMock())
        executor.context_compiler.compile_messages = lambda request: []
        result = await executor.execute(None, AgentRuntimeContext('r', 'c', 'u', 's'))
        self.assertIn('七天内', result.reply_content)
        self.assertIn('物流查询失败', result.reply_content)
        self.assertNotIn('已经退款', result.reply_content)

    async def test_timeout_preserves_finished_results(self):
        from atguigu.agent.harness.run.executor import AgentExecutor
        from atguigu.agent.multi_agent.budget import RunBudget
        from atguigu.agent.multi_agent.definition import DomainResult
        agent = AsyncMock()
        async def slow(*args, **kwargs):
            kwargs['context'].execution.results.append(DomainResult(
                task_id='a', agent_code='policy', status='SUCCESS', summary='可申请退货。'))
            await asyncio.sleep(10)
        agent.ainvoke.side_effect = slow
        executor = AgentExecutor(agent, AsyncMock())
        executor.context_compiler.compile_messages = lambda request: []
        ctx = AgentRuntimeContext('r', 'c', 'u', 's', execution=RunExecution(budget=RunBudget(timeout_seconds=.02)))
        result = await executor.execute(None, ctx)
        self.assertIn('可申请退货', result.reply_content)
        self.assertIn('未完成', result.reply_content)

    def test_factory_builds_seven_agents_with_fixed_tool_sets(self):
        from atguigu.agent.factory import create_support_agent
        with patch('atguigu.agent.factory.create_agent', return_value=object()) as create, \
                patch('atguigu.agent.factory.ModelAdapter.create_model', return_value=object()):
            create_support_agent()
        self.assertEqual(create.call_count, 7)
        supervisor = create.call_args_list[-1].kwargs
        self.assertEqual({t.name for t in supervisor['tools']}, {
            'consult_product', 'consult_order', 'consult_logistics', 'consult_aftersales', 'consult_policy', 'consult_points'})
        for call in create.call_args_list[:-1]:
            self.assertNotIn('load_skill', [t.name for t in call.kwargs['tools']])


if __name__ == '__main__':
    unittest.main()
