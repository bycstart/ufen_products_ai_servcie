"""积分功能测试：模拟外部响应，不向业务系统写入积分。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError


class PointsModelTests(unittest.TestCase):
    def test_points_order_keeps_real_points_without_fake_cash(self):
        from atguigu.agent.harness.tools.output import OrderData
        data = dict(id='ORDER_001', status='paid', payment_type='points', total_points=5000,
                    total_amount=None, created_at='2026-10-24T10:00:00+08:00', items=[{
                        'product_id': 'PRODUCT_001', 'product_name': '商品', 'quantity': 1,
                        'unit_price': None, 'unit_points': 5000}])
        order = OrderData(**data)
        self.assertEqual(order.total_points, 5000)
        self.assertIsNone(order.total_amount)
        self.assertIsNone(order.items[0].unit_price)
        for invalid in (-1, True, 1.5, '5000'):
            with self.subTest(value=invalid), self.assertRaises(ValidationError):
                OrderData(**{**data, 'total_points': invalid})

    def test_balance_requires_real_nonnegative_integer_points(self):
        from atguigu.agent.harness.tools.points_output import PointsBalanceData
        base = dict(account_id='ACCOUNT_001', available_points=3000, frozen_points=500,
                    expiring_points=100, as_of='2026-10-24T10:00:00+08:00')
        self.assertEqual(PointsBalanceData(**base).available_points, 3000)
        for value in (-1, 1.5, True, '3000'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PointsBalanceData(**{**base, 'available_points': value})

    def test_product_keeps_points_price_separate_from_cash(self):
        from atguigu.agent.harness.tools.output import ProductData
        product = ProductData(id='PRODUCT_001', name='电饭煲', category='厨电', price='199',
                              status='on_sale', description='商品', specs={}, points_price=3000)
        self.assertEqual(product.points_price, 3000)
        self.assertEqual(str(product.price), '199')
        self.assertIsNone(ProductData(**{k: v for k, v in product.model_dump().items() if k != 'points_price'}).points_price)

    def test_points_skill_has_four_read_tools(self):
        from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
        tools = DOMAIN_SPECS['points'].tools
        self.assertEqual(set(tools), {
            'get_points_account', 'get_points_transactions',
            'check_points_eligibility', 'get_order_points',
        })

    def test_points_only_product_does_not_need_fake_cash_price(self):
        from atguigu.agent.harness.tools.output import ProductData
        product = ProductData(id='PRODUCT_001', name='电饭煲', category='厨电', points_price=3000,
                              status='on_sale', description='商品', specs={})
        self.assertIsNone(product.price)


class PointsToolTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self):
        from atguigu.agent.harness.run.runtime import AgentRuntimeContext
        from atguigu.agent.multi_agent.state import RunExecution
        from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
        context = AgentRuntimeContext('run', 'conv', 'user', 'user-token', execution=RunExecution())
        context = context.for_task('points', DOMAIN_SPECS['points'].tools)
        return SimpleNamespace(context=context, tool_call_id='call')

    async def test_balance_uses_user_client_and_evidence_pipeline(self):
        from atguigu.agent.harness.tools import points
        response = {'success': True, 'code': 'OK', 'message': 'OK', 'data': {
            'account_id': 'ACCOUNT_001', 'available_points': 3000, 'frozen_points': 500,
            'expiring_points': 100, 'as_of': '2026-10-24T10:00:00+08:00',
            'expiry_included': False, 'expiry_batches': None, 'total': None, 'page': 1, 'page_size': 20,
        }}
        with patch.object(points.EcommerceClient, 'get_json', new_callable=AsyncMock, return_value=response) as get, \
                patch.object(points.tool_executor, '_create_tool_call', new_callable=AsyncMock), \
                patch.object(points.tool_executor, '_save_tool_result', new_callable=AsyncMock):
            runtime = self.runtime()
            result = json.loads(await points.get_points_account.coroutine(runtime=runtime))
        get.assert_awaited_once_with('/points/account', query_params={
            'include_expiry': False, 'page': 1, 'page_size': 20})
        self.assertTrue(result['success'])
        self.assertIn('evidence_id', result)
        self.assertEqual(runtime.context.access_token, 'user-token')

    async def test_no_fake_balance_when_backend_missing(self):
        from atguigu.agent.harness.tools import points
        with patch.object(points.EcommerceClient, 'get_json', new_callable=AsyncMock, side_effect=RuntimeError('404')), \
                patch.object(points.tool_executor, '_create_tool_call', new_callable=AsyncMock), \
                patch.object(points.tool_executor, '_save_tool_result', new_callable=AsyncMock), \
                self.assertLogs('atguigu.agent.harness.tools.executor', level='WARNING'):
            result = json.loads(await points.get_points_account.coroutine(runtime=self.runtime()))
        self.assertFalse(result['success'])
        self.assertIsNone(result['data'])

    async def test_all_four_tools_and_expiry_mode_use_read_only_paths(self):
        from atguigu.agent.harness.tools import points
        cases = [
            ('get_points_account', {}, '/points/account'),
            ('get_points_transactions', {'page': 2}, '/points/transactions'),
            ('get_points_account', {'include_expiry': True}, '/points/account'),
            ('check_points_eligibility', {'product_id': 'PRODUCT_001', 'quantity': 2}, '/points/eligibility'),
            ('get_order_points', {'order_id': 'ORDER_001'}, '/orders/ORDER_001/points'),
        ]
        for name, kwargs, path in cases:
            with self.subTest(tool=name), \
                    patch.object(points.tool_executor, 'execute', new_callable=AsyncMock) as execute, \
                    patch.object(points.EcommerceClient, 'get_json', new_callable=AsyncMock) as get:
                await getattr(points, name).coroutine(runtime=self.runtime(), **kwargs)
                await execute.call_args.kwargs['operation']()
                self.assertEqual(get.call_args.args[0], path)
                self.assertEqual(execute.call_args.kwargs['tool_name'], name)

    def test_model_cannot_supply_identity_or_invalid_quantity(self):
        from atguigu.agent.harness.tools.points import check_points_eligibility, get_points_transactions
        schema = check_points_eligibility.tool_call_schema.model_json_schema()
        self.assertNotIn('user_id', schema['properties'])
        self.assertNotIn('access_token', schema['properties'])
        with self.assertRaises(ValidationError):
            check_points_eligibility.tool_call_schema.model_validate({'product_id': 'PRODUCT_001', 'quantity': 0})
        with self.assertRaises(ValidationError):
            get_points_transactions.tool_call_schema.model_validate({'page_size': 1000})

    async def test_points_rules_use_authenticated_backend(self):
        from atguigu.agent.harness.tools import read
        with patch.object(read.tool_executor, 'execute', new_callable=AsyncMock) as execute, \
                patch.object(read.EcommerceClient, 'get_json', new_callable=AsyncMock) as get:
            await read.search_knowledge.coroutine('积分退款后是否延期', self.runtime(), topic='points')
            await execute.call_args.kwargs['operation']()
        get.assert_awaited_once_with('/points/rules', query_params={'query': '积分退款后是否延期'})

    async def test_budget_search_does_not_convert_cash_price(self):
        from atguigu.agent.harness.tools import read
        response = {'success': True, 'code': 'OK', 'message': 'OK', 'data': [{
            'id': 'PRODUCT_001', 'name': '商品', 'category': '厨电', 'price': '199',
            'status': 'on_sale', 'description': '商品', 'specs': {},
        }]}
        with patch.object(read.tool_executor, 'execute', new_callable=AsyncMock) as execute, \
                patch.object(read.EcommerceClient, 'get_json', new_callable=AsyncMock, return_value=response):
            await read.search_products.coroutine('电饭煲', self.runtime(), max_points=3000)
            result = await execute.call_args.kwargs['operation']()
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'POINTS_PRICE_UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
