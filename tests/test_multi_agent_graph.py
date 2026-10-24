"""运行真实 LangChain 图；仅替换模型、数据库写入与远程业务接口。"""
import asyncio
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
from atguigu.agent.harness.run.output import AgentRunOutPutMapper
from atguigu.app.schemas.run import AgentRunRequest


class ScriptedChatModel(BaseChatModel):
    role: str
    scenario: str = 'multi'

    @property
    def _llm_type(self):
        return 'scripted-test'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        if self.role == 'supervisor' and self.scenario == 'parallel' and not tool_messages:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[
                {'name': 'consult_logistics', 'args': {'task': '查询 ORDER_001 的物流'}, 'id': 'logistics'},
                {'name': 'consult_policy', 'args': {'task': '查询退货政策'}, 'id': 'policy'},
            ], usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15},
                response_metadata={'model_name': self.role}))])
        if self.role == 'supervisor':
            if self.scenario == 'greeting':
                name, args = 'AgentOutput', {'reply_type': 'ANSWER', 'reply_content': '您好！'}
            elif self.scenario == 'page':
                name, args = (('consult_order', {'task': '前往修改 ORDER_001 的地址'}) if not tool_messages
                              else ('AgentOutput', {'reply_type': 'ANSWER', 'reply_content': '请前往页面'}))
            elif not tool_messages:
                name, args = 'consult_logistics', {'task': '查询 ORDER_001 的物流'}
            elif len(tool_messages) == 1:
                name, args = 'consult_policy', {'task': '查询七天无理由退货规则'}
            else:
                name, args = 'AgentOutput', {'reply_type': 'ANSWER', 'reply_content': '主控自由汇总内容'}
                if self.scenario == 'handoff':
                    args = {'reply_type': 'REQUEST_HANDOFF', 'reply_content': '请人工处理', 'handoff_request': {
                        'reason_code': 'USER_REQUESTED', 'summary': '用户查询后要求人工处理',
                    }}
        elif not tool_messages:
            if self.role == 'order':
                name, args = 'get_order', {'order_id': 'ORDER_001'}
            elif self.role == 'logistics':
                name, args = 'get_logistics', {'order_id': 'ORDER_001'}
            else:
                name, args = 'search_knowledge', {'query': '七天无理由退货'}
        else:
            result = json.loads(tool_messages[-1].content)
            if self.role == 'order':
                text, resource = '订单 ORDER_001 已支付', 'ORDER_001'
            elif self.role == 'logistics':
                text, resource = '订单 ORDER_001 运输中', 'ORDER_001'
            else:
                text, resource = result['data'][0]['content'], None
            name, args = 'DomainAgentOutput', {'status': 'SUCCESS', 'findings': [{
                'text': text, 'resource_id': resource, 'evidence_id': result['evidence_id'],
            }]}
            if self.scenario == 'page':
                args['page_action_request'] = {'action_code': 'EDIT_ADDRESS', 'resource_id': 'ORDER_001'}
        msg = AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': 'model-call'}],
                        usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15},
                        response_metadata={'model_name': self.role})
        return ChatResult(generations=[ChatGeneration(message=msg)])


class RealGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_round_delegations_run_in_parallel_tool_node(self):
        from atguigu.agent.multi_agent.delegation import DelegationService
        entered, release = asyncio.Queue(), asyncio.Event()
        original = DelegationService._delegate
        async def blocked(service, code, task, context, config):
            entered.put_nowait(code)
            await release.wait()
            return await original(service, code, task, context, config)
        with patch.object(DelegationService, '_delegate', new=blocked):
            task = asyncio.create_task(self.run_graph('parallel'))
            try:
                codes = {await asyncio.wait_for(entered.get(), 2),
                         await asyncio.wait_for(entered.get(), 2)}
                self.assertEqual(codes, {'logistics', 'policy'})
                release.set()
                result = await task
                self.assertIn('运输中', result.reply_content)
                self.assertIn('七天', result.reply_content)
                self.assertEqual(result.token_usage, {'input_tokens': 60, 'output_tokens': 30})
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def asyncSetUp(self):
        self.request = AgentRunRequest(conversation_id='c', turn_id='t', messages=[{
            'message_id': 'm', 'type': 'text', 'content': {'text': '查物流和退货政策'},
        }])

    async def run_graph(self, scenario='multi'):
        models = [ScriptedChatModel(role=r, scenario=scenario) for r in
                  ('product', 'order', 'logistics', 'aftersales', 'policy', 'points', 'supervisor')]
        with patch('atguigu.agent.factory.ModelAdapter.create_model', side_effect=models):
            agent = create_support_agent()
        reader = AsyncMock()
        reader.read_by_run_id.return_value = ()
        executor = AgentExecutor(agent, AgentOutputValidator(reader))
        response = {'success': True, 'code': 'OK', 'message': 'OK', 'data': {
            'order_id': 'ORDER_001', 'company': '快递', 'tracking_number': 'T001',
            'status': 'in_transit', 'latest_event': '运输中', 'events': [],
        }}
        if scenario == 'page':
            response['data'] = {'id': 'ORDER_001', 'status': 'paid', 'total_amount': '99',
                                'created_at': '2026-10-24T00:00:00Z', 'items': []}
        with patch.object(ToolExecutor, '_create_tool_call', new_callable=AsyncMock), \
                patch.object(ToolExecutor, '_save_tool_result', new_callable=AsyncMock), \
                patch('atguigu.agent.harness.tools.read.EcommerceClient.get_json',
                      new_callable=AsyncMock, return_value=response):
            return await executor.execute(self.request, AgentRuntimeContext('r', 'c', 'u', 'token'))

    async def test_two_domains_real_graph_and_token_accounting(self):
        result = await self.run_graph()
        self.assertIn('运输中', result.reply_content)
        self.assertIn('七天', result.reply_content)
        self.assertNotIn('自由汇总', result.reply_content)
        # 主控 3 次，物流 2 次，政策 2 次；每次输入10输出5，嵌套回调不重复累计。
        self.assertEqual(result.token_usage, {'input_tokens': 70, 'output_tokens': 35})
        calls = result.model_metrics['calls']
        self.assertTrue(all(call['token_usage'] is not None for call in calls))
        self.assertEqual(sum(call['token_usage']['input_tokens'] for call in calls), 70)
        self.assertEqual(sum(call['token_usage']['output_tokens'] for call in calls), 35)

    async def test_greeting_does_not_delegate(self):
        result = await self.run_graph('greeting')
        self.assertEqual(result.reply_content, '您好！')
        self.assertEqual(result.token_usage, {'input_tokens': 10, 'output_tokens': 5})

    async def test_page_action_uses_verified_order_and_existing_response(self):
        result = await self.run_graph('page')
        self.assertEqual(result.page_action.href, '/me/orders/ORDER_001/address')
        state, response = AgentRunOutPutMapper.map(result)
        self.assertEqual(state, 'DECISION_PREPARED')
        self.assertEqual(response['content']['action']['label'], '前往修改地址')

    async def test_handoff_after_two_domains_preserves_ticket(self):
        result = await self.run_graph('handoff')
        state, response = AgentRunOutPutMapper.map(result)
        self.assertEqual(state, 'HANDED_OFF')
        self.assertEqual(response['reason_code'], 'USER_REQUESTED')
        self.assertIn('运输中', response['message'])


if __name__ == '__main__':
    unittest.main()
