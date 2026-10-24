"""验证模型耗时回调，不发起网络请求。"""
import unittest
from unittest.mock import patch
from uuid import uuid4


class ModelMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_tokens_per_call_when_parallel_calls_finish_out_of_order(self):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, LLMResult
        from atguigu.agent.harness.run.metrics import ModelTimingCallback
        callback = ModelTimingCallback()
        first, second = uuid4(), uuid4()
        await callback.on_chat_model_start({}, [], run_id=first, metadata={'agent_code': 'supervisor'})
        await callback.on_chat_model_start({}, [], run_id=second, metadata={'agent_code': 'points'})
        def response(input_tokens, output_tokens):
            return LLMResult(generations=[[ChatGeneration(message=AIMessage(content='', usage_metadata={
                'input_tokens': input_tokens, 'output_tokens': output_tokens,
                'total_tokens': input_tokens + output_tokens,
                'input_token_details': {'cache_read': 3}, 'output_token_details': {'reasoning': 2},
            }))]])
        await callback.on_llm_end(response(20, 5), run_id=second)
        await callback.on_llm_end(response(10, 4), run_id=first)
        calls = callback.summary()['calls']
        self.assertEqual([call['token_usage']['input_tokens'] for call in calls], [10, 20])
        self.assertEqual([call['token_usage']['output_tokens'] for call in calls], [4, 5])
        self.assertEqual([call['token_usage']['total_tokens'] for call in calls], [14, 25])
        self.assertEqual(calls[1]['token_usage']['output_token_details']['reasoning'], 2)
        self.assertEqual(calls[0]['token_usage']['input_token_details']['cache_read'], 3)

    async def test_missing_usage_and_failed_calls_are_unknown_instead_of_zero(self):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, LLMResult
        from atguigu.agent.harness.run.metrics import ModelTimingCallback
        callback = ModelTimingCallback()
        for status in ('missing', 'failed', 'incomplete'):
            identifier = uuid4()
            await callback.on_chat_model_start({}, [], run_id=identifier)
            if status == 'missing':
                await callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content=''))]]),
                                          run_id=identifier)
            elif status == 'failed':
                await callback.on_llm_error(RuntimeError('hidden-key'), run_id=identifier)
        for call in callback.summary()['calls']:
            self.assertIsNone(call['token_usage'])

    async def test_reports_overlapping_model_intervals(self):
        from atguigu.agent.harness.run.metrics import ModelTimingCallback
        callback = ModelTimingCallback()
        first, second = uuid4(), uuid4()
        with patch('atguigu.agent.harness.run.metrics.perf_counter', side_effect=[10, 11, 12, 14]):
            await callback.on_chat_model_start({}, [], run_id=first)
            await callback.on_chat_model_start({}, [], run_id=second)
            await callback.on_llm_end(None, run_id=first)
            await callback.on_llm_end(None, run_id=second)
        report = callback.summary()
        self.assertEqual(report['max_parallel_calls'], 2)
        self.assertEqual(report['total_latency_ms'], 5000)
        self.assertEqual([(call['started_after_ms'], call['finished_after_ms']) for call in report['calls']],
                         [(0, 2000), (1000, 4000)])

    async def test_records_supervisor_and_specialist_identity_from_metadata(self):
        from atguigu.agent.harness.run.metrics import ModelTimingCallback
        callback = ModelTimingCallback()
        for code, role, name in (('supervisor', 'supervisor', '主控智能体'),
                                  ('points', 'specialist', '积分子智能体')):
            identifier = uuid4()
            await callback.on_chat_model_start({}, [], run_id=identifier, metadata={
                'agent_code': code, 'agent_role': role, 'agent_name': name, 'task_id': 'task'})
            await callback.on_llm_end(None, run_id=identifier)
        calls = callback.summary()['calls']
        self.assertEqual([call['agent_code'] for call in calls], ['supervisor', 'points'])
        self.assertEqual([call['agent_name'] for call in calls], ['主控智能体', '积分子智能体'])

    async def test_records_each_model_call_duration_and_error(self):
        from atguigu.agent.harness.run.metrics import ModelTimingCallback
        callback = ModelTimingCallback()
        first, second = uuid4(), uuid4()
        with patch('atguigu.agent.harness.run.metrics.perf_counter', side_effect=[10, 10.25, 20, 20.5]):
            await callback.on_chat_model_start({}, [], run_id=first, metadata={'ls_model_name': 'test-model'})
            await callback.on_llm_end(None, run_id=first)
            await callback.on_chat_model_start({}, [], run_id=second)
            await callback.on_llm_error(RuntimeError('hidden-key'), run_id=second)
        report = callback.summary()
        self.assertEqual(report['call_count'], 2)
        self.assertEqual(report['total_latency_ms'], 750)
        self.assertEqual([item['status'] for item in report['calls']], ['SUCCESS', 'FAILED'])
        self.assertNotIn('hidden-key', str(report))

    async def test_cancelled_call_still_has_measured_duration(self):
        from atguigu.agent.harness.run.metrics import ModelTimingCallback
        callback = ModelTimingCallback()
        with patch('atguigu.agent.harness.run.metrics.perf_counter', side_effect=[10, 11]):
            await callback.on_chat_model_start({}, [], run_id=uuid4())
            report = callback.summary()
        self.assertEqual(report['call_count'], 1)
        self.assertEqual(report['calls'][0]['status'], 'INCOMPLETE')
        self.assertEqual(report['calls'][0]['latency_ms'], 1000)
