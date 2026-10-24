"""覆盖委派、证据和协议兼容的回归场景。"""
import asyncio
import json
import unittest
from contextlib import nullcontext
from unittest.mock import AsyncMock

from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.harness.tools.snapshot import ToolCallSnapshot
from atguigu.agent.multi_agent.definition import DomainAgentOutput, DomainResult
from atguigu.agent.multi_agent.state import RunExecution
from atguigu.agent.multi_agent.validation import compose_results, validate_domain_output


class EvidenceTests(unittest.TestCase):
    def test_aftersale_records_cannot_borrow_status(self):
        call = ToolCallSnapshot('list_after_sales', {}, {'data': [
            {'id': 'AS_001', 'order_id': 'ORDER_001', 'status': 'pending'},
            {'id': 'AS_002', 'order_id': 'ORDER_001', 'status': 'approved'},
        ]}, True, tool_call_id='call')
        output = DomainAgentOutput(status='SUCCESS', findings=[{
            'text': 'AS_001 已通过', 'resource_id': 'ORDER_001', 'evidence_id': 'call',
        }])
        with self.assertRaises(ValueError):
            validate_domain_output(output, (call,), 'aftersales', 'task')

    def test_clarification_cannot_mix_facts_from_two_orders(self):
        call = ToolCallSnapshot('list_orders', {}, {'data': [
            {'id': 'ORDER_001', 'status': 'paid'},
            {'id': 'ORDER_002', 'status': 'delivered'},
        ]}, True, tool_call_id='call')
        output = DomainAgentOutput(status='NEEDS_CLARIFICATION',
            clarification='ORDER_001 已签收，请确认订单。')
        with self.assertRaises(ValueError):
            validate_domain_output(output, (call,), 'order', 'task')

    def test_empty_clarification_is_allowed_without_query(self):
        output = DomainAgentOutput(status='NEEDS_CLARIFICATION', clarification='请提供订单号。')
        result = validate_domain_output(output, (), 'logistics', 'task')
        self.assertEqual(result.status, 'NEEDS_CLARIFICATION')

    def test_twelve_long_results_still_fit_response(self):
        results = [DomainResult(task_id=str(i), agent_code='product', status='SUCCESS', summary='内容' * 1000)
                   for i in range(12)]
        output = compose_results(results, '未完成部分请分批查询。')
        self.assertLessEqual(len(output.reply_content), 4000)
        self.assertIn('未完成', output.reply_content)

    def test_shortened_summary_keeps_partial_failure_marker(self):
        results = [DomainResult(task_id='mixed', agent_code='logistics', status='SUCCESS',
                               summary='已查到订单。' * 300 + '物流查询失败。', failed_tools=['get_logistics'])]
        results.extend(DomainResult(task_id=str(i), agent_code='product', status='SUCCESS', summary='商品' * 1000)
                       for i in range(11))
        text = compose_results(results).reply_content
        self.assertIn('get_logistics', text)
        self.assertIn('未完成', text)
        self.assertLessEqual(len(text), 4000)

    def test_page_action_requires_matching_response_identity(self):
        from atguigu.agent.harness.validator.action import PageActionValidator
        from atguigu.agent.llm.output import PageActionRequest
        call = ToolCallSnapshot('get_order', {'order_id': 'ORDER_001'},
                                {'data': {'id': 'ORDER_002'}}, True)
        with self.assertRaises(ValueError):
            PageActionValidator().validate(PageActionRequest(
                action_code='EDIT_ADDRESS', resource_id='ORDER_001'), (call,))


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_service_failure_keeps_failure_type(self):
        from atguigu.agent.harness.tools.executor import ToolExecutor
        executor = ToolExecutor()
        executor._create_tool_call = AsyncMock()
        executor._save_tool_result = AsyncMock()
        context = AgentRuntimeContext('r', 'c', 'u', 's', execution=RunExecution())
        operation = AsyncMock(side_effect=RuntimeError('down'))
        for code in ('order', 'logistics'):
            child = context.for_task(code, ('get_order',))
            with self.assertLogs('atguigu.agent.harness.tools.executor', level='WARNING') if code == 'order' else nullcontext():
                result = json.loads(await executor.execute(child, 'id', 'get_order', {}, operation, dict))
            self.assertEqual(result['failure_type'], 'SERVICE_CALL')
        self.assertEqual(operation.await_count, 1)

    async def test_handoff_survives_domain_query(self):
        from atguigu.agent.harness.run.executor import AgentExecutor
        agent = AsyncMock()
        async def invoke(*args, **kwargs):
            kwargs['context'].execution.results.append(DomainResult(
                task_id='t', agent_code='policy', status='SUCCESS', summary='已查询到平台规则。'))
            return {'structured_response': {'reply_type': 'REQUEST_HANDOFF', 'reply_content': '请人工处理',
                'handoff_request': {'reason_code': 'USER_REQUESTED', 'summary': '用户查询后要求人工处理'}}}
        agent.ainvoke.side_effect = invoke
        executor = AgentExecutor(agent, AsyncMock())
        executor.context_compiler.compile_messages = lambda _: []
        result = await executor.execute(None, AgentRuntimeContext('r', 'c', 'u', 's'))
        self.assertEqual(result.reply_type, 'REQUEST_HANDOFF')
        self.assertEqual(result.handoff_request.reason_code, 'USER_REQUESTED')
        self.assertIn('平台规则', result.reply_content)

    async def test_parallel_delegations_overlap(self):
        from atguigu.agent.multi_agent.delegation import DelegationService
        active, peak = 0, 0
        async def invoke(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(.01)
            active -= 1
            return {'structured_response': {'status': 'NEEDS_CLARIFICATION', 'clarification': '请提供订单号。'}}
        expert = AsyncMock()
        expert.ainvoke.side_effect = invoke
        context = AgentRuntimeContext('r', 'c', 'u', 's', execution=RunExecution())
        service = DelegationService({'order': expert, 'logistics': expert})
        await asyncio.gather(service.delegate('order', '查订单', context, {}),
                             service.delegate('logistics', '查物流', context, {}))
        self.assertEqual(peak, 2)

    async def test_shared_expert_does_not_share_user_state(self):
        from atguigu.agent.multi_agent.delegation import DelegationService
        seen = []
        async def invoke(*args, **kwargs):
            context = kwargs['context']
            seen.append((context.user_id, context.access_token))
            await asyncio.sleep(0)
            return {'structured_response': {'status': 'NEEDS_CLARIFICATION', 'clarification': '请提供订单号。'}}
        expert = AsyncMock()
        expert.ainvoke.side_effect = invoke
        service = DelegationService({'order': expert})
        contexts = [AgentRuntimeContext(f'r{i}', 'c', f'u{i}', f'token{i}', execution=RunExecution())
                    for i in range(2)]
        results = await asyncio.gather(*(service.delegate('order', '查订单', ctx, {}) for ctx in contexts))
        self.assertEqual(set(seen), {('u0', 'token0'), ('u1', 'token1')})
        self.assertNotEqual(results[0].task_id, results[1].task_id)
        for ctx in contexts:
            self.assertEqual(len(ctx.execution.results), 1)
            self.assertEqual(len(ctx.execution.evidence), 1)

    async def test_cancelled_expert_is_not_converted_to_service_failure(self):
        from atguigu.agent.multi_agent.delegation import DelegationService
        expert = AsyncMock()
        expert.ainvoke.side_effect = asyncio.CancelledError
        context = AgentRuntimeContext('r', 'c', 'u', 's', execution=RunExecution())
        with self.assertRaises(asyncio.CancelledError):
            await DelegationService({'order': expert}).delegate('order', '查订单', context, {})
        self.assertEqual(context.execution.results, [])


if __name__ == '__main__':
    unittest.main()
