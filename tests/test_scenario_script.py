"""验证客户端超时日志，不将未知的服务端调用次数打印成零。"""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx


spec = importlib.util.spec_from_file_location('scenario_cli',
    Path(__file__).resolve().parents[1] / 'scripts/test_customer_scenarios.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class ScenarioScriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_prints_and_report_preserves_per_call_tokens(self):
        for usage, expected in (({'input_tokens': 12, 'output_tokens': 3, 'total_tokens': 15},
                                  'Token 输入 12 / 输出 3 / 合计 15'),
                                 ({'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0},
                                  'Token 输入 0 / 输出 0 / 合计 0'), (None, 'Token 未知')):
            with self.subTest(usage=usage), tempfile.TemporaryDirectory() as temporary:
                call = {'call_index': 1, 'model': 'test-model', 'agent_code': 'points',
                        'agent_name': '积分子智能体', 'status': 'SUCCESS', 'latency_ms': 50, 'token_usage': usage}
                def respond(request):
                    if request.url.path.endswith('/cases'):
                        return httpx.Response(200, json=[{'case_id': '01', 'question': '查询积分'}])
                    if request.url.path.endswith('/demo-token'):
                        return httpx.Response(200, json={'access_token': 'test-only'})
                    return httpx.Response(200, json={'passed': True, 'execution_succeeded': True,
                        'model_metrics': {'call_count': 1, 'total_latency_ms': 50, 'calls': [call]}})
                output = Path(temporary) / 'result.json'
                args = SimpleNamespace(ai_url='http://test', cases=None, list=False, user_id='user_001', output=output)
                settings = SimpleNamespace(internal_service_token='test-only', multi_agent_timeout_seconds=1,
                                           ecommerce_base_url='http://test/api/v1')
                real_client, terminal = httpx.AsyncClient, io.StringIO()
                with patch.object(cli, 'get_settings', return_value=settings), \
                        patch.object(cli.httpx, 'AsyncClient', side_effect=lambda **kwargs: real_client(
                            transport=httpx.MockTransport(respond))), redirect_stdout(terminal):
                    self.assertEqual(await cli.main(args), 0)
                self.assertIn(expected, terminal.getvalue())
                saved = json.loads(output.read_text(encoding='utf-8'))['results'][0]
                self.assertEqual(saved['model_metrics']['calls'][0]['token_usage'], usage)

    async def test_timeout_prints_unknown_metrics_and_saves_client_elapsed_time(self):
        def respond(request):
            if request.url.path.endswith('/cases'):
                return httpx.Response(200, json=[{'case_id': '01', 'category': '积分', 'question': '查询积分'}])
            if request.url.path.endswith('/demo-token'):
                return httpx.Response(200, json={'access_token': 'test-only-token'})
            raise httpx.ReadTimeout('do-not-log-sensitive-details')

        real_client = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'result.json'
            args = SimpleNamespace(ai_url='http://test', cases=None, list=False, user_id='user_001', output=output)
            settings = SimpleNamespace(internal_service_token='internal-only', multi_agent_timeout_seconds=1,
                                       ecommerce_base_url='http://test/api/v1')
            terminal = io.StringIO()
            with patch.object(cli, 'get_settings', return_value=settings), \
                    patch.object(cli.httpx, 'AsyncClient', side_effect=lambda **kwargs: real_client(
                        transport=httpx.MockTransport(respond))), redirect_stdout(terminal):
                code = await cli.main(args)
            self.assertEqual(code, 1)
            self.assertIn('未知', terminal.getvalue())
            self.assertNotIn('模型 0次', terminal.getvalue())
            self.assertNotIn('do-not-log-sensitive-details', terminal.getvalue())
            result = json.loads(output.read_text(encoding='utf-8'))['results'][0]
            self.assertGreaterEqual(result['client_latency_ms'], 0)
