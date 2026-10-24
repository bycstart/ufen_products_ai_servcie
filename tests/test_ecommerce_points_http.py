"""真实 HTTP 联调：临时端口 + 隔离账本，保留 AI 工具与响应校验链路。"""
import json
from datetime import UTC, datetime, timedelta
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import uvicorn

import test_ecommerce_points as backend_tests


@unittest.skipUnless(backend_tests.BACKEND.is_dir(), '同级 Ecommerce 源码不存在')
class EcommercePointsHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        backend_tests.EcommercePointsTests.setUp(self)
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.base_url = f'http://127.0.0.1:{self.socket.getsockname()[1]}/api/v1'
        self.server = uvicorn.Server(uvicorn.Config(
            self.main.app, lifespan='off', log_level='error', loop='asyncio', ws='none'))
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertTrue(self.server.started, '隔离 HTTP 服务未启动')

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.socket.close()
        backend_tests.EcommercePointsTests.tearDown(self)

    def runtime(self, domain):
        from atguigu.agent.harness.run.runtime import AgentRuntimeContext
        from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
        from atguigu.agent.multi_agent.state import RunExecution
        context = AgentRuntimeContext('run-http', 'conv-http', 'user_001', self.token,
                                       execution=RunExecution())
        return SimpleNamespace(context=context.for_task(domain, DOMAIN_SPECS[domain].tools),
                                tool_call_id=f'http-{domain}')

    async def test_ai_tools_use_real_http_and_accept_backend_contract(self):
        from atguigu.agent.harness.tools import points, read
        from atguigu.agent.harness.tools.executor import ToolExecutor
        from atguigu.app.services.auth import AuthService

        auth = AuthService()
        auth.settings = self.auth_settings
        self.assertEqual(auth.get_authorized_user(f'Bearer {self.token}', 'customer').user_id, 'user_001')
        from ecommerce_service.points_models import PointsAccount, PointsRule, ProductPoints
        with self.sessions() as db:
            db.add(PointsAccount(id='ACCOUNT_SECOND', user_id='user_001', project_id='other',
                                 account_type='general', is_default=False, status='active'))
            db.add(ProductPoints(project_id='other', product_id='PRODUCT_005', points_price=10,
                                 allowed_account_types=['general'], max_per_order=99))
            db.add(PointsRule(id='RULE_SECOND', project_id='other', title='第二商城积分规则',
                              content='第二商城积分转赠规则', source='other', version='other-v1',
                              effective_at=datetime.now(UTC) - timedelta(days=1)))
            db.commit()
        settings = SimpleNamespace(ecommerce_base_url=self.base_url, ecommerce_timeout_seconds=3)
        cases = (
            (points.get_points_account, {}, 'points', 'available_points'),
            (points.get_points_transactions, {}, 'points', 'items'),
            (points.get_points_account, {'include_expiry': True}, 'points', 'expiry_batches'),
            (points.check_points_eligibility, {'product_id': 'PRODUCT_005'}, 'points', 'eligible'),
            (points.get_order_points, {'order_id': 'ORDER_POINTS_DEMO_001'}, 'points', 'deducted_points'),
            (read.get_order, {'order_id': 'ORDER_POINTS_DEMO_001'}, 'order', 'total_points'),
            (read.search_products, {'query': '', 'max_points': 2500,
                                    'account_id': 'ACCOUNT_SECOND'}, 'product', 'points_price'),
            (read.get_product, {'product_id': 'PRODUCT_005', 'account_id': 'ACCOUNT_SECOND'}, 'product', 'points_price'),
            (read.search_knowledge, {'query': '积分能转赠吗', 'topic': 'points',
                                     'account_id': 'ACCOUNT_SECOND'}, 'policy', 'version'),
        )
        # 仅替换 AI 审计记录写入；HTTP、JWT、后端账本查询、工具模型校验均真实执行。
        with patch('atguigu.infrastructure.client.get_settings', return_value=settings), \
                patch.object(ToolExecutor, '_create_tool_call', new_callable=AsyncMock), \
                patch.object(ToolExecutor, '_save_tool_result', new_callable=AsyncMock):
            for tool, arguments, domain, key in cases:
                with self.subTest(tool=tool.name):
                    response = json.loads(await tool.coroutine(runtime=self.runtime(domain), **arguments))
                    self.assertTrue(response['success'], response)
                    self.assertIn('evidence_id', response)
                    data = response['data']
                    self.assertIn(key, data[0] if isinstance(data, list) else data)
                    if arguments.get('account_id') == 'ACCOUNT_SECOND':
                        row = data[0] if isinstance(data, list) else data
                        if domain == 'product':
                            self.assertEqual(row['points_price'], 10)
                        else:
                            self.assertEqual(row['knowledge_id'], 'RULE_SECOND')
            denied = json.loads(await points.get_points_account.coroutine(
                runtime=self.runtime('points'), account_id='ACCOUNT_DEMO_002'))
            self.assertFalse(denied['success'])
            self.assertIsNone(denied['data'])


if __name__ == '__main__':
    unittest.main()
