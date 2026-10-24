"""临期退款演示数据：隔离数据库验证，不修改真实用户历史。"""
import importlib.util
import unittest
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tests import test_ecommerce_points as fixture


@unittest.skipUnless(fixture.BACKEND.is_dir(), '同级 Ecommerce 源码不存在')
class RefundExpiryDemoTests(unittest.TestCase):
    setUp = fixture.EcommercePointsTests.setUp
    tearDown = fixture.EcommercePointsTests.tearDown
    get = fixture.EcommercePointsTests.get

    def seed(self, now=None):
        name = 'ecommerce_service.refund_expiry_seed'
        self.assertIsNotNone(importlib.util.find_spec(name), '缺少独立临期退款演示初始化模块')
        from ecommerce_service import refund_expiry_seed
        with self.sessions.begin() as db:
            created = refund_expiry_seed.seed_refund_expiry_demo(db, now=now)
        return refund_expiry_seed, created

    def login_demo(self, user_id):
        response = self.client.post('/api/v1/auth/demo-token', json={'user_id': user_id})
        self.assertEqual(response.status_code, 200)
        self.headers = {'Authorization': 'Bearer ' + response.json()['access_token']}

    def test_frontend_login_and_points_queries(self):
        old = self.get('/points/account')['data']['available_points']
        seed, created = self.seed()
        self.assertTrue(created)
        self.assertEqual(self.get('/points/account')['data']['available_points'], old)
        # 原用户不能访问新建用户的订单和积分账户。
        self.assertFalse(self.get('/orders/' + seed.ORDER_ID + '/points')['success'])
        self.assertFalse(self.get('/points/account', account_id=seed.ACCOUNT_ID)['success'])
        users = self.client.get('/api/v1/demo/users').json()
        self.assertTrue(any(row['id'] == seed.USER_ID for row in users))
        self.login_demo(seed.USER_ID)
        account = self.get('/points/account', include_expiry=True)['data']
        self.assertEqual(account['available_points'], 1000)
        self.assertEqual(account['frozen_points'], 0)
        batch = account['expiry_batches'][0]
        self.assertEqual(batch['batch_id'], seed.BATCH_ID)
        expiry = datetime.fromisoformat(batch['expires_at']).astimezone(ZoneInfo('Asia/Shanghai'))
        self.assertEqual(expiry.date(), datetime.now(ZoneInfo('Asia/Shanghai')).date() + timedelta(days=1))
        self.assertEqual((expiry.hour, expiry.minute, expiry.second), (23, 59, 59))
        detail = self.get('/orders/' + seed.ORDER_ID)['data']
        self.assertEqual(detail['status'], 'cancelled')
        self.assertEqual(detail['total_points'], 2000)
        self.assertEqual(detail['items'][0]['unit_points'], 2000)
        points = self.get('/orders/' + seed.ORDER_ID + '/points')['data']
        self.assertEqual(points['deducted_points'], 2000)
        self.assertEqual((points['status'], points['expected_points'], points['returned_points']),
                         ('pending', 2000, 0))
        transactions = self.get('/points/transactions')['data']
        self.assertEqual(sum(row['change_points'] for row in transactions['items']), 1000)
        self.assertEqual(self.get('/points/transactions', event_type='refund')['data']['items'], [])
        rules = self.get('/points/rules', query='返还积分过期')['data']
        self.assertTrue(rules)
        self.assertTrue(all('演示' in row['source'] for row in rules))
        self.assertIn('不承诺自动延期', ''.join(row['content'] for row in rules))

    def test_repeat_does_not_reset_progress_or_extend_expiry(self):
        seed, _ = self.seed(now=datetime(2026, 10, 24, 16, 10, tzinfo=UTC))
        from ecommerce_service.points_models import OrderPoints, PointsBatch
        with self.sessions.begin() as db:
            batch = db.get(PointsBatch, seed.BATCH_ID)
            original_expiry = batch.expires_at
            # 模拟后续业务修改；重复初始化不能覆盖业务进度。
            batch.available_points = 500
            db.get(OrderPoints, seed.ORDER_ID).refund_status = 'processing'
        _, created = self.seed(now=datetime(2026, 11, 1, tzinfo=UTC))
        self.assertFalse(created)
        with self.sessions() as db:
            self.assertEqual(db.get(PointsBatch, seed.BATCH_ID).expires_at, original_expiry)
            self.assertEqual(db.get(PointsBatch, seed.BATCH_ID).available_points, 500)
            self.assertEqual(db.get(OrderPoints, seed.ORDER_ID).refund_status, 'processing')
        # UTC 10 月 24 日 16:10 已是上海 25 日，明天应为 26 日。
        self.assertEqual(original_expiry.replace(tzinfo=UTC).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),
                         '2026-10-26')

    def test_identifier_conflict_leaves_existing_data_unchanged(self):
        import importlib
        from ecommerce_service.models import User
        self.assertIsNotNone(importlib.util.find_spec('ecommerce_service.refund_expiry_seed'),
                             '缺少独立临期退款演示初始化模块')
        seed = importlib.import_module('ecommerce_service.refund_expiry_seed')
        with self.sessions.begin() as db:
            db.add(User(id=seed.USER_ID, name='已有用户', membership='普通会员'))
        with self.assertRaises(ValueError):
            self.seed()
        from ecommerce_service.points_models import PointsAccount
        with self.sessions() as db:
            self.assertEqual(db.get(User, seed.USER_ID).name, '已有用户')
            self.assertIsNone(db.get(PointsAccount, seed.ACCOUNT_ID))

    def test_existing_project_is_not_reused_for_another_customer(self):
        from ecommerce_service import refund_expiry_seed as seed
        from ecommerce_service.models import User
        from ecommerce_service.points_models import PointsAccount
        with self.sessions.begin() as db:
            db.add(PointsAccount(id='OTHER_PROJECT_OWNER', user_id='user_001',
                                 project_id=seed.PROJECT, account_type='general', status='active'))
        with self.assertRaisesRegex(ValueError, '项目'):
            self.seed()
        with self.sessions() as db:
            self.assertIsNone(db.get(User, seed.USER_ID))
