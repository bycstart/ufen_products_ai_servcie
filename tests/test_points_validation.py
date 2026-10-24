"""积分按字段、账户与业务对象校验，避免把冻结积分回答成可用积分。"""
import unittest

from atguigu.agent.harness.tools.snapshot import ToolCallSnapshot
from atguigu.agent.multi_agent.definition import DomainAgentOutput
from atguigu.agent.multi_agent.validation import validate_domain_output

TIME = '2026-10-24T10:00:00+08:00'


def points_output(resource_id, text='模型的自由表述'):
    return DomainAgentOutput(status='SUCCESS', findings=[{
        'text': text, 'resource_id': resource_id, 'evidence_id': 'call',
    }])


class PointsValidationTests(unittest.TestCase):
    def test_finding_requires_explicit_resource_field_and_policy_can_use_null(self):
        with self.assertRaises(ValueError):
            DomainAgentOutput(status='SUCCESS', findings=[{'text': '业务结论', 'evidence_id': 'call'}])
        output = DomainAgentOutput(status='SUCCESS', findings=[{
            'text': '政策原文', 'evidence_id': 'call', 'resource_id': None}])
        self.assertIsNone(output.findings[0].resource_id)

    def test_merged_account_rejects_unloaded_foreign_and_unrequested_batches(self):
        from atguigu.agent.harness.tools.points_output import PointsAccountData
        base = dict(account_id='ACCOUNT_001', available_points=3000, frozen_points=0,
                    expiring_points=100, as_of=TIME, expiry_included=True,
                    page=1, page_size=20, total=1,
                    expiry_batches=[dict(batch_id='BATCH_001', account_id='ACCOUNT_002', points=100, expires_at=TIME)])
        with self.assertRaises(ValueError):
            PointsAccountData(**base)
        base['expiry_batches'][0]['account_id'] = 'ACCOUNT_001'
        with self.assertRaises(ValueError):
            self.validate('get_points_account', base, 'BATCH_001', {'include_expiry': False})
        with self.assertRaises(ValueError):
            self.validate('get_points_account', base, 'BATCH_OTHER', {'include_expiry': True})
        with self.assertRaises(ValueError):
            PointsAccountData(**{**base, 'expiry_included': False})

    def test_merged_order_rejects_excess_or_unfinished_refund(self):
        from atguigu.agent.harness.tools.points_output import OrderPointsData
        base = dict(order_id='ORDER_001', account_id='ACCOUNT_001', deducted_points=500,
                    frozen_points=0, expected_points=500, returned_points=0,
                    status='completed', as_of=TIME, updated_at=TIME)
        with self.assertRaises(ValueError):
            OrderPointsData(**base)
        with self.assertRaises(ValueError):
            OrderPointsData(**{**base, 'status': 'processing', 'returned_points': 501})

    def validate(self, name, data, resource, arguments=None):
        call = ToolCallSnapshot(name, arguments or {}, {'data': data}, True, tool_call_id='call')
        return validate_domain_output(points_output(resource), (call,), 'points', 'task')

    def test_balance_text_comes_from_named_fields(self):
        result = self.validate('get_points_account', {
            'account_id': 'ACCOUNT_001', 'available_points': 3000, 'frozen_points': 500,
            'expiring_points': 100, 'as_of': TIME,
            'expiry_included': False, 'expiry_batches': None, 'page': 1, 'page_size': 20, 'total': None,
        }, 'ACCOUNT_001')
        self.assertIn('可用积分3000', result.summary)
        self.assertIn('冻结积分500', result.summary)
        self.assertNotIn('模型的自由表述', result.summary)

    def test_transaction_uses_one_row_and_preserves_pagination(self):
        data = {'account_id': 'ACCOUNT_001', 'page': 2, 'page_size': 20, 'total': 30, 'items': [{
            'transaction_id': 'TXN_001', 'account_id': 'ACCOUNT_001', 'event_type': 'consume',
            'change_points': -500, 'occurred_at': TIME, 'order_id': 'ORDER_001',
        }]}
        result = self.validate('get_points_transactions', data, 'TXN_001')
        self.assertIn('-500', result.summary)
        self.assertIn('第2页', result.summary)
        with self.assertRaises(ValueError):
            self.validate('get_points_transactions', data, 'TXN_OTHER')

    def test_transaction_must_match_event_and_business_date_filters(self):
        data = {'account_id': 'ACCOUNT_001', 'page': 1, 'page_size': 20, 'total': 1, 'items': [{
            'transaction_id': 'TXN_001', 'account_id': 'ACCOUNT_001', 'event_type': 'consume',
            'change_points': -500, 'occurred_at': '2026-10-23T23:30:00Z',
        }]}
        # UTC 23:30 对应上海次日，按商城约定的上海业务日期检查。
        self.validate('get_points_transactions', data, 'TXN_001', {'date_from': '2026-10-24'})
        for arguments in ({'event_type': 'refund'}, {'date_to': '2026-10-23'}, {'date_from': '2026-10-25'}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.validate('get_points_transactions', data, 'TXN_001', arguments)

    def test_expiry_uses_exact_batch_date(self):
        result = self.validate('get_points_account', {
            'account_id': 'ACCOUNT_001', 'available_points': 3000, 'frozen_points': 500,
            'expiring_points': 100, 'as_of': TIME, 'expiry_included': True,
            'page': 1, 'page_size': 20, 'total': 1, 'expiry_batches': [{
            'batch_id': 'BATCH_001', 'account_id': 'ACCOUNT_001', 'points': 100, 'expires_at': TIME,
        }]}, 'BATCH_001', {'include_expiry': True})
        self.assertIn('2026-10-24', result.summary)
        self.assertIn('100积分', result.summary)

    def test_negative_eligibility_does_not_mean_exchange_succeeded(self):
        data = {'account_id': 'ACCOUNT_001', 'product_id': 'PRODUCT_001', 'quantity': 2,
                'eligible': False, 'reason_code': 'LIMIT_EXCEEDED', 'reason_message': '超过商品限兑数量',
                'required_points': 2000, 'available_points': 3000, 'checked_at': TIME}
        result = self.validate('check_points_eligibility', data, 'PRODUCT_001',
                               {'product_id': 'PRODUCT_001', 'quantity': 2})
        self.assertIn('预校验未通过', result.summary)
        self.assertIn('超过商品限兑数量', result.summary)
        with self.assertRaises(ValueError):
            self.validate('check_points_eligibility', data, 'PRODUCT_001',
                          {'product_id': 'PRODUCT_001', 'quantity': 1})

    def test_one_order_query_preserves_deduction_and_refund_fields(self):
        result = self.validate('get_order_points', {
            'order_id': 'ORDER_001', 'account_id': 'ACCOUNT_001', 'deducted_points': 500,
            'frozen_points': 0, 'returned_points': 0, 'as_of': TIME,
            'status': 'processing', 'expected_points': 500, 'updated_at': TIME,
        }, 'ORDER_001', {'order_id': 'ORDER_001'})
        self.assertIn('扣减积分500', result.summary)
        self.assertIn('返还处理中', result.summary)
        self.assertIn('已返还积分0', result.summary)

    def test_requested_account_must_match_response(self):
        with self.assertRaises(ValueError):
            self.validate('get_points_account', {
                'account_id': 'ACCOUNT_002', 'available_points': 3000, 'frozen_points': 0,
                'expiring_points': 0, 'as_of': TIME,
                'expiry_included': False, 'expiry_batches': None, 'page': 1, 'page_size': 20, 'total': None,
            }, 'ACCOUNT_002', {'account_id': 'ACCOUNT_001'})

    def test_empty_transaction_page_is_not_a_service_error(self):
        call = ToolCallSnapshot('get_points_transactions', {'page': 3}, {'data': {
            'account_id': 'ACCOUNT_001', 'page': 3, 'page_size': 20, 'total': 5, 'items': [],
        }}, True, tool_call_id='call')
        result = validate_domain_output(DomainAgentOutput(status='SUCCESS'), (call,), 'points', 'task')
        self.assertIn('第3页', result.summary)
        self.assertEqual(result.status, 'SUCCESS')

    def test_cash_price_cannot_support_points_claim(self):
        from atguigu.agent.harness.rules.fact import FactChecker
        call = ToolCallSnapshot('get_product', {}, {'data': {'id': 'PRODUCT_001', 'price': 3000,
                                                            'points_price': 500}}, True)
        self.assertTrue(FactChecker().get_unsupported_facts('商品需要3000积分', (call,)))
        self.assertFalse(FactChecker().get_unsupported_facts('商品需要500积分', (call,)))

    def test_product_bonus_points_cannot_support_redemption_price(self):
        from atguigu.agent.harness.rules.fact import FactChecker
        call = ToolCallSnapshot('get_product', {}, {'data': {
            'id': 'PRODUCT_001', 'points_price': 3000, 'specs': {'reward_points': 100},
        }}, True)
        self.assertTrue(FactChecker().get_unsupported_facts('商品积分价格100', (call,)))

    def test_points_only_product_cannot_support_cash_quote(self):
        from atguigu.agent.harness.rules.fact import FactChecker
        call = ToolCallSnapshot('get_product', {}, {'data': {
            'id': 'PRODUCT_001', 'price': None, 'points_price': 500,
        }}, True)
        for text in ('商品价格500元', '商品价格500', '商品售价￥500'):
            with self.subTest(text=text):
                self.assertTrue(FactChecker().get_unsupported_facts(text, (call,)))
        self.assertFalse(FactChecker().get_unsupported_facts('商品积分价格500', (call,)))

    def test_points_claim_without_query_is_rejected(self):
        from atguigu.agent.harness.rules.fact import FactChecker
        self.assertTrue(FactChecker().get_unsupported_facts('您的可用积分为3000', ()))


if __name__ == '__main__':
    unittest.main()
