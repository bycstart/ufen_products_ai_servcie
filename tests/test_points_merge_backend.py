"""合并接口的权限、按需分页和退款事实验证，使用隔离数据库。"""
import unittest

import test_ecommerce_points as backend_tests


@unittest.skipUnless(backend_tests.BACKEND.is_dir(), '同级 Ecommerce 源码不存在')
class PointsMergeBackendTests(unittest.TestCase):
    setUp = backend_tests.EcommercePointsTests.setUp
    tearDown = backend_tests.EcommercePointsTests.tearDown
    get = backend_tests.EcommercePointsTests.get

    def test_account_summary_does_not_load_expiry_until_requested(self):
        data = self.get('/points/account')['data']
        self.assertEqual((data['available_points'], data['frozen_points'], data['expiring_points']),
                         (4000, 200, 3000))
        self.assertFalse(data['expiry_included'])
        self.assertIsNone(data['expiry_batches'])
        self.assertIsNone(data['total'])
        page = self.get('/points/account', include_expiry=True, page_size=1)['data']
        self.assertTrue(page['expiry_included'])
        self.assertEqual(page['total'], 2)
        self.assertEqual(len(page['expiry_batches']), 1)
        self.assertEqual(page['expiry_batches'][0]['account_id'], page['account_id'])
        empty = self.get('/points/account', include_expiry=True, page=3, page_size=1)['data']
        self.assertEqual(empty['expiry_batches'], [])
        self.assertEqual(empty['total'], 2)

    def test_merged_endpoints_enforce_identity_and_pagination(self):
        self.assertFalse(self.get('/points/account', account_id='ACCOUNT_DEMO_002')['success'])
        self.assertFalse(self.get('/orders/ORDER_004/points')['success'])
        self.assertFalse(self.get('/orders/ORDER_001/points')['success'])
        for path in ('/points/account', '/orders/ORDER_POINTS_DEMO_001/points'):
            self.assertEqual(self.client.get('/api/v1'+path).status_code, 401)
        self.assertEqual(self.client.get('/api/v1/points/account', headers=self.headers,
                                        params={'page_size': 101}).status_code, 422)

    def test_order_query_combines_deduction_and_pending_refund_without_crediting(self):
        path = '/orders/ORDER_POINTS_DEMO_001/points'
        before = self.get('/points/balance')['data']
        initial = self.get(path)['data']
        self.assertEqual(initial['deducted_points'], 2000)
        self.assertEqual(initial['status'], 'not_required')
        self.client.post('/api/v1/orders/ORDER_POINTS_DEMO_001/cancel', headers=self.headers)
        refunded = self.get(path)['data']
        self.assertEqual((refunded['deducted_points'], refunded['expected_points'], refunded['returned_points']),
                         (2000, 2000, 0))
        self.assertEqual(refunded['status'], 'pending')
        self.assertEqual(self.get('/points/balance')['data']['available_points'], before['available_points'])
