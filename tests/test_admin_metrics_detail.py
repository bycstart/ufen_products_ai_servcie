"""管理员观测数据，不连接数据库或调用模型。"""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
from atguigu.app.schemas.run import CurrentMessage
from atguigu.app.services.admin import AdminService


class AdminMetricsDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_message_without_role_is_a_user_question(self):
        message = CurrentMessage(message_id='message-test', type='text', content={'text': '当前问题'})
        context = {'messages': [message.model_dump(), {'role': 'assistant', 'content': '不能取助手回答'}]}
        session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [
            self.run_record(input_context=context)])))
        row = (await AdminService(session).list_runs(10))[0]
        self.assertEqual(row['user_question'], '当前问题')

    async def test_missing_run_returns_404_before_querying_tools(self):
        session = SimpleNamespace(get=AsyncMock(return_value=None), scalars=AsyncMock())
        with self.assertRaises(HTTPException) as raised:
            await AdminService(session).get_run('missing')
        self.assertEqual(raised.exception.status_code, 404)
        session.scalars.assert_not_awaited()

    def run_record(self, **changes):
        fields = dict(id='run-test', conversation_id='conv-test', state='COMPLETED',
                      model_name='test-model', latency_ms=1200, input_tokens=12,
                      output_tokens=4, prompt_version='v1', input_context={'messages': [
                          {'role': 'user', 'content': {'text': '查询积分'}}]},
                      result=None, started_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
                      finished_at=None)
        fields.update(changes)
        return SimpleNamespace(**fields)

    async def test_list_exposes_question_time_and_conversation_for_matching_chat(self):
        session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [self.run_record()])))
        row = (await AdminService(session).list_runs(10))[0]
        self.assertEqual(row['user_question'], '查询积分')
        self.assertEqual(row['conversation_id'], 'conv-test')
        self.assertEqual(row['started_at'], '2026-10-24T00:00:00+00:00')
        self.assertEqual(row['total_tokens'], 16)

    async def test_detail_preserves_missing_measurements_and_tool_status(self):
        call = SimpleNamespace(tool_call_id='tool-1', tool_name='points',
                               arguments={'authorization': 'secret', 'query': '积分'},
                               latency_ms=None, success=False, result={'code': 'TIMEOUT'})
        session = SimpleNamespace(get=AsyncMock(return_value=self.run_record(input_tokens=None, output_tokens=None)),
                                  scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [call])))
        result = await AdminService(session).get_run('run-test')
        self.assertIsNone(result['run']['total_tokens'])
        self.assertEqual(result['run']['latency_ms'], 1200)
        self.assertEqual(result['tool_calls'][0]['code'], 'TIMEOUT')
        self.assertFalse(result['tool_calls'][0]['success'])
        self.assertNotIn('secret', str(result))

    async def test_model_metrics_and_plain_string_user_content_are_preserved(self):
        metrics = {'calls': [{'agent_code': 'supervisor', 'latency_ms': 42, 'token_usage': None}],
                   'max_parallel_calls': 1}
        session = SimpleNamespace(get=AsyncMock(return_value=self.run_record(
            result={'model_metrics': metrics}, input_context={'messages': [
                {'role': 'user', 'content': '旧问题'}, {'role': 'assistant', 'content': '回答'},
                {'role': 'user', 'content': '  最新问题\n详情  '}]})),
            scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
        detail = await AdminService(session).get_run('run-test')
        self.assertEqual(detail['run']['user_question'], '最新问题 详情')
        self.assertEqual(detail['run']['outcome']['model_metrics'], metrics)

    async def test_actual_zero_is_distinct_from_missing_data(self):
        session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [
            self.run_record(input_tokens=0, output_tokens=0, latency_ms=0,
                            started_at=None, input_context={})])))
        row = (await AdminService(session).list_runs(10))[0]
        self.assertEqual(row['total_tokens'], 0)
        self.assertEqual(row['latency_ms'], 0)
        self.assertIsNone(row['started_at'])
        self.assertIsNone(row['user_question'])

    async def test_nested_authentication_fields_are_removed_without_hiding_token_metrics(self):
        session = SimpleNamespace(get=AsyncMock(return_value=self.run_record(result={
            'nested': [{'api_key': 'hidden-api-key', 'access_token': 'hidden-jwt'}],
            'model_metrics': {'calls': [{'token_usage': {'total_tokens': 5}}]}})),
            scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
        detail = await AdminService(session).get_run('run-test')
        self.assertNotIn('hidden-api-key', str(detail))
        self.assertNotIn('hidden-jwt', str(detail))
        self.assertEqual(detail['run']['outcome']['model_metrics']['calls'][0]['token_usage']['total_tokens'], 5)
