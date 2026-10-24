"""运行真实 Agent 图，替换外部模型及后端，验证积分工具注入和证据回传。"""
import json
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from atguigu.agent.factory import create_support_agent
from atguigu.agent.harness.run.executor import AgentExecutor
from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.harness.tools.executor import ToolExecutor
from atguigu.agent.harness.validator.output import AgentOutputValidator
from atguigu.app.schemas.run import AgentRunRequest
from atguigu.infrastructure.client import EcommerceClient

TIME = '2026-10-24T10:00:00+08:00'
TOOLS = ('get_points_account', 'get_points_transactions', 'check_points_eligibility', 'get_order_points')
RESPONSES = {
    '/points/account': {'account_id': 'ACCOUNT_001', 'available_points': 3000, 'frozen_points': 500,
                        'expiring_points': 100, 'as_of': TIME, 'expiry_included': False,
                        'expiry_batches': None, 'page': 1, 'page_size': 20, 'total': None},
    '/points/transactions': {'account_id': 'ACCOUNT_001', 'page': 1, 'page_size': 20, 'total': 1, 'items': [{
        'transaction_id': 'TXN_001', 'account_id': 'ACCOUNT_001', 'event_type': 'consume',
        'change_points': -500, 'occurred_at': TIME, 'order_id': 'ORDER_001',
    }]},
    '/points/eligibility': {'account_id': 'ACCOUNT_001', 'product_id': 'PRODUCT_001', 'quantity': 1,
                            'eligible': False, 'reason_code': 'LIMIT', 'reason_message': '超过限兑次数',
                            'required_points': 2000, 'available_points': 3000, 'checked_at': TIME},
    '/orders/ORDER_001/points': {'account_id': 'ACCOUNT_001', 'order_id': 'ORDER_001',
        'deducted_points': 500, 'frozen_points': 0, 'as_of': TIME, 'status': 'processing',
        'expected_points': 500, 'returned_points': 0, 'updated_at': TIME},
    '/points/rules': [{'knowledge_id': 'RULE_001', 'title': '积分使用规则', 'content': '积分适用范围以商城项目规则为准。',
                      'source': '当前商城', 'version': 'v1', 'effective_at': TIME}],
    '/products': [{'id': 'PRODUCT_001', 'name': '电饭煲', 'category': '厨电', 'price': '199',
                   'points_price': 2000, 'status': 'on_sale', 'description': '商品', 'specs': {}}],
}


class PointsChatModel(BaseChatModel):
    role: str
    query_tool: str
    combined: bool = False

    @property
    def _llm_type(self):
        return 'points-test'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        replies = [message for message in messages if isinstance(message, ToolMessage)]
        if self.role == 'supervisor':
            steps = ['consult_product', 'consult_points', 'consult_policy'] if self.combined else ['consult_points']
            if len(replies) < len(steps):
                name, args = steps[len(replies)], {'task': '查询 PRODUCT_001 的兑换条件和积分规则'}
            else:
                name, args = 'AgentOutput', {'reply_type': 'ANSWER', 'reply_content': '汇总'}
        elif not replies:
            if self.role == 'product':
                name, args = 'search_products', {'query': '电饭煲', 'max_points': 3000}
            elif self.role == 'policy':
                name, args = 'search_knowledge', {'query': '积分使用规则', 'topic': 'points'}
            else:
                name, args = self.query_tool, {}
                if name == 'check_points_eligibility':
                    args = {'product_id': 'PRODUCT_001', 'quantity': 1}
                elif name == 'get_order_points':
                    args = {'order_id': 'ORDER_001'}
        else:
            envelope = json.loads(replies[-1].content)
            name, args = 'DomainAgentOutput', {'status': 'SERVICE_FAILED'}
            if envelope['success']:
                data = envelope['data']
                if self.role == 'product':
                    resource, text = data[0]['id'], '商品 PRODUCT_001 需要2000积分'
                elif self.role == 'policy':
                    resource, text = None, data[0]['content']
                else:
                    record = data[0] if isinstance(data, list) else data['items'][0] if 'items' in data else data
                    resource = next(record[key] for key in ('transaction_id', 'batch_id', 'product_id', 'order_id', 'account_id')
                                    if key in record)
                    text = '积分模型草稿，最终由服务端依据字段生成'
                args = {'status': 'SUCCESS', 'findings': [{
                    'resource_id': resource, 'text': text, 'evidence_id': envelope['evidence_id'],
                }]}
        message = AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'call-{len(replies)}'}],
                            usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15},
                            response_metadata={'model_name': self.role})
        return ChatResult(generations=[ChatGeneration(message=message)])


class PointsGraphTests(unittest.IsolatedAsyncioTestCase):
    async def run_graph(self, tool_name, combined=False, missing=False):
        roles = ('product', 'order', 'logistics', 'aftersales', 'policy', 'points', 'supervisor')
        models = [PointsChatModel(role=role, query_tool=tool_name, combined=combined) for role in roles]
        with patch('atguigu.agent.factory.ModelAdapter.create_model', side_effect=models):
            agent = create_support_agent()
        reader = AsyncMock()
        reader.read_by_run_id.return_value = ()
        executor = AgentExecutor(agent, AgentOutputValidator(reader))
        request = AgentRunRequest(conversation_id='c', turn_id='t', messages=[{
            'message_id': 'm', 'type': 'text', 'content': {'text': '查询积分'},
        }])

        async def respond(path, query_params=None):
            if missing:
                raise RuntimeError('backend unavailable')
            return {'success': True, 'code': 'OK', 'message': 'OK', 'data': RESPONSES[path]}

        with patch.object(EcommerceClient, 'get_json', side_effect=respond), \
                patch.object(ToolExecutor, '_create_tool_call', new_callable=AsyncMock), \
                patch.object(ToolExecutor, '_save_tool_result', new_callable=AsyncMock):
            return await executor.execute(request, AgentRuntimeContext('run', 'c', 'user', 'token'))

    async def test_four_points_tools_through_real_graph(self):
        expected = ('可用积分3000', '-500', '预校验未通过', '扣减积分500')
        for tool_name, text in zip(TOOLS, expected):
            with self.subTest(tool=tool_name):
                result = await self.run_graph(tool_name)
                self.assertIn(text, result.reply_content)
                self.assertNotIn('模型草稿', result.reply_content)
                self.assertEqual(result.token_usage, {'input_tokens': 40, 'output_tokens': 20})
                self.assertEqual(result.model_metrics['call_count'], 4)
                self.assertTrue(all(call['status'] == 'SUCCESS' for call in result.model_metrics['calls']))
                self.assertEqual([call['agent_code'] for call in result.model_metrics['calls']],
                                 ['supervisor', 'points', 'points', 'supervisor'])

    async def test_product_points_and_policy_cooperate(self):
        result = await self.run_graph('check_points_eligibility', combined=True)
        self.assertIn('2000积分', result.reply_content)
        self.assertIn('预校验未通过', result.reply_content)
        self.assertIn('商城项目规则', result.reply_content)
        self.assertEqual(result.token_usage, {'input_tokens': 100, 'output_tokens': 50})

    async def test_missing_backend_never_returns_balance(self):
        with self.assertLogs('atguigu.agent.harness.tools.executor', level='WARNING'):
            result = await self.run_graph('get_points_account', missing=True)
        self.assertEqual(result.reply_type, 'DECLINE')
        self.assertNotIn('3000', result.reply_content)


if __name__ == '__main__':
    unittest.main()
