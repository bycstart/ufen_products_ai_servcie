import asyncio
import unittest
from unittest.mock import AsyncMock, patch


class MultiAgentCoreTests(unittest.IsolatedAsyncioTestCase):
    def test_context_children_share_budget_but_not_evidence(self):
        from atguigu.agent.harness.run.runtime import AgentRuntimeContext
        from atguigu.agent.multi_agent.state import RunExecution
        state = RunExecution()
        parent = AgentRuntimeContext('run', 'conversation', 'user', 'secret', execution=state)
        a, b = parent.for_task('order'), parent.for_task('logistics')
        self.assertNotEqual(a.task_id, b.task_id)
        self.assertIs(a.execution, b.execution)
        self.assertEqual(a.access_token, 'secret')
        self.assertNotIn('secret', repr(a))

    def test_experts_have_only_their_domain_tools(self):
        from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
        self.assertEqual(len(DOMAIN_SPECS), 6)
        logistics = DOMAIN_SPECS['logistics']
        self.assertEqual(set(logistics.tools), {'list_orders', 'get_order', 'get_logistics'})
        self.assertTrue(all('load_skill' not in s.tools for s in DOMAIN_SPECS.values()))

    def test_tool_catalog_contains_only_current_domain_tools(self):
        from atguigu.agent.harness.tools.catalog import TOOL_CATALOG
        from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
        self.assertEqual({tool.name for tool in TOOL_CATALOG.get_agent_tools()},
                         {name for spec in DOMAIN_SPECS.values() for name in spec.tools})

    def test_fabricated_evidence_is_rejected(self):
        from atguigu.agent.multi_agent.definition import DomainAgentOutput
        from atguigu.agent.multi_agent.validation import validate_domain_output
        output = DomainAgentOutput(status='SUCCESS', findings=[{
            'text': '订单 ORDER_001 已签收', 'evidence_id': 'foreign',
            'resource_id': 'ORDER_001',
        }])
        with self.assertRaises(ValueError):
            validate_domain_output(output, (), 'logistics', 'task')

    def test_order_a_cannot_borrow_order_b_status(self):
        from atguigu.agent.harness.tools.snapshot import ToolCallSnapshot
        from atguigu.agent.multi_agent.definition import DomainAgentOutput
        from atguigu.agent.multi_agent.validation import validate_domain_output
        call = ToolCallSnapshot('list_orders', {}, {'data': [
            {'id': 'ORDER_001', 'status': 'paid'},
            {'id': 'ORDER_002', 'status': 'delivered'},
        ]}, True, tool_call_id='call')
        output = DomainAgentOutput(status='SUCCESS', findings=[{
            'text': '订单 ORDER_001 已签收', 'resource_id': 'ORDER_001', 'evidence_id': 'call',
        }])
        with self.assertRaises(ValueError):
            validate_domain_output(output, (call,), 'order', 'task')

    def test_policy_must_quote_returned_knowledge(self):
        from atguigu.agent.harness.tools.snapshot import ToolCallSnapshot
        from atguigu.agent.multi_agent.definition import DomainAgentOutput
        from atguigu.agent.multi_agent.validation import validate_domain_output
        call = ToolCallSnapshot('search_knowledge', {}, {'data': [{'content': '七天内可申请退货。'}]},
                                True, tool_call_id='call')
        output = DomainAgentOutput(status='SUCCESS', findings=[{
            'text': '任何商品都能无条件退款。', 'evidence_id': 'call', 'resource_id': None,
        }])
        with self.assertRaises(ValueError):
            validate_domain_output(output, (call,), 'policy', 'task')

    def test_partial_failure_preserves_success(self):
        from atguigu.agent.multi_agent.definition import DomainResult
        from atguigu.agent.multi_agent.validation import compose_results
        results = [
            DomainResult(task_id='one', agent_code='policy', status='SUCCESS', summary='七天内可申请退货。'),
            DomainResult(task_id='two', agent_code='logistics', status='SERVICE_FAILED', summary='物流暂时无法查询。'),
        ]
        output = compose_results(results)
        self.assertIn('七天内', output.reply_content)
        self.assertIn('暂时无法', output.reply_content)
        self.assertEqual(output.reply_type, 'ANSWER')

    def test_multiple_actions_request_choice(self):
        from atguigu.agent.multi_agent.definition import DomainResult
        from atguigu.agent.multi_agent.validation import compose_results
        results = [DomainResult(task_id=str(i), agent_code='order', status='SUCCESS', summary='请到页面操作',
            page_action_request={'action_code': 'EDIT_ADDRESS', 'resource_id': f'ORDER_00{i}'}) for i in (1, 2)]
        output = compose_results(results)
        self.assertEqual(output.reply_type, 'CLARIFY')
        self.assertIsNone(output.page_action_request)

    async def test_delegation_forwards_identity_and_only_callbacks(self):
        from atguigu.agent.harness.run.runtime import AgentRuntimeContext
        from atguigu.agent.multi_agent.state import RunExecution
        from atguigu.agent.multi_agent.delegation import DelegationService
        expert = AsyncMock()
        expert.ainvoke.return_value = {'structured_response': {
            'status': 'NEEDS_CLARIFICATION', 'clarification': '请提供订单号。',
        }}
        context = AgentRuntimeContext('run', 'conv', 'u1', 'secret', execution=RunExecution())
        service = DelegationService({'logistics': expert})
        result = await service.delegate('logistics', '查询物流', context, {
            'callbacks': [], 'configurable': {'checkpoint_id': 'parent-only'},
        })
        kwargs = expert.ainvoke.call_args.kwargs
        self.assertEqual(kwargs['context'].user_id, 'u1')
        self.assertEqual(kwargs['context'].access_token, 'secret')
        self.assertNotIn('configurable', kwargs['config'])
        self.assertEqual(result.status, 'NEEDS_CLARIFICATION')
        self.assertEqual(len(context.execution.results), 1)

    async def test_identical_delegation_reuses_validated_result(self):
        from atguigu.agent.harness.run.runtime import AgentRuntimeContext
        from atguigu.agent.multi_agent.state import RunExecution
        from atguigu.agent.multi_agent.delegation import DelegationService
        expert = AsyncMock()
        expert.ainvoke.return_value = {'structured_response': {
            'status': 'NEEDS_CLARIFICATION', 'clarification': '请提供订单号。',
        }}
        ctx = AgentRuntimeContext('run', 'conv', 'user', 'secret', execution=RunExecution())
        service = DelegationService({'logistics': expert})
        await service.delegate('logistics', '查询物流', ctx, {})
        await service.delegate('logistics', '查询物流', ctx, {})
        self.assertEqual(expert.ainvoke.await_count, 1)


if __name__ == '__main__':
    unittest.main()
