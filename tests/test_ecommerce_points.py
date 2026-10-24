"""跨服务契约测试：使用独立数据库，不连接或修改现有业务数据库。"""
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


BACKEND = Path(__file__).resolve().parents[3] / 'backend/backend/ecommerce-service'
if BACKEND.is_dir():
    sys.path.insert(0, str(BACKEND))


@compiles(JSONB, 'sqlite')
def sqlite_jsonb(_type, _compiler, **_kwargs):
    # 仅测试库使用 SQLite JSON；生产模型仍然保持 PostgreSQL JSONB。
    return 'JSON'


@unittest.skipUnless(BACKEND.is_dir(), '同级 Ecommerce 源码不存在')
class EcommercePointsTests(unittest.TestCase):
    def setUp(self):
        with patch.dict('os.environ', ECOM_DATABASE_URL='sqlite://'):
            from ecommerce_service import main
        from ecommerce_service.seed import seed_demo_data
        self.main = main
        self.temp = tempfile.TemporaryDirectory(prefix='points-test-')
        self.engine = create_engine(f'sqlite:///{Path(self.temp.name) / "ecommerce.db"}',
                                    connect_args={'check_same_thread': False})
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        main.Base.metadata.create_all(self.engine)
        with self.sessions() as db:
            seed_demo_data(db)
            from ecommerce_service.points_seed import seed_points_demo
            seed_points_demo(db)

        self.auth_settings = SimpleNamespace(jwt_secret='isolated-points-test-secret-32-bytes',
                                             jwt_algorithm='HS256')
        auth_patch = patch('ecommerce_service.common.auth.get_settings', return_value=self.auth_settings)
        auth_patch.start()
        self.addCleanup(auth_patch.stop)

        def database():
            with self.sessions() as db:
                yield db

        main.app.dependency_overrides[main.get_db] = database
        # 不进入生产 lifespan，以免连接配置中的业务数据库。
        self.client = TestClient(main.app)
        result = self.client.post('/api/v1/auth/demo-token', json={'user_id': 'user_001'})
        self.token = result.json()['access_token']
        self.headers = {'Authorization': f'Bearer {self.token}'}

    def tearDown(self):
        self.client.close()
        self.main.app.dependency_overrides.clear()
        self.engine.dispose()
        self.temp.cleanup()

    def test_points_balance_endpoint_exists(self):
        response = self.client.get('/api/v1/points/balance', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn('success', response.json())

    def get(self, path, **params):
        response = self.client.get('/api/v1' + path, params=params, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_balance_expiry_and_seed_are_consistent(self):
        data = self.get('/points/balance')['data']
        self.assertEqual((data['available_points'], data['frozen_points'], data['expiring_points']),
                         (4000, 200, 3000))
        batches = self.get('/points/expiry')['data']
        self.assertEqual(sum(row['points'] for row in batches), 4000)
        transactions = self.get('/points/transactions')['data']
        self.assertEqual(sum(row['change_points'] for row in transactions['items']), 4000)
        from ecommerce_service.points_seed import seed_points_demo
        with self.sessions() as db:
            seed_points_demo(db)
        self.assertEqual(self.get('/points/transactions')['data']['total'], transactions['total'])
        self.assertEqual(self.get('/points/balance')['data']['available_points'], 4000)

    def test_ownership_and_authentication(self):
        self.assertFalse(self.get('/points/balance', account_id='ACCOUNT_DEMO_002')['success'])
        for suffix in ('points-deduction', 'points-refund'):
            response = self.get('/orders/ORDER_004/' + suffix)
            self.assertFalse(response['success'])
            self.assertIsNone(response['data'])
        self.assertEqual(self.client.get('/api/v1/points/balance').status_code, 401)
        self.assertEqual(self.client.get('/api/v1/points/balance', headers={
            'Authorization': 'Bearer invalid'}).status_code, 401)

    def test_default_account_ambiguity_and_explicit_selection(self):
        from ecommerce_service.points_models import PointsAccount
        with self.sessions() as db:
            account = db.get(PointsAccount, 'ACCOUNT_DEMO_001')
            account.is_default = False
            db.add(PointsAccount(id='ACCOUNT_SECOND', user_id='user_001', project_id='other',
                                 account_type='general', is_default=False, status='active'))
            db.commit()
        self.assertEqual(self.get('/points/balance')['code'], 'POINTS_ACCOUNT_REQUIRED')
        self.assertTrue(self.get('/points/balance', account_id='ACCOUNT_SECOND')['success'])
        self.assertFalse(self.get('/points/rules', query='规则')['success'])

    def test_eligibility_is_read_only_and_checks_stock_budget_and_limits(self):
        before = self.get('/points/balance')['data']
        data = self.get('/points/eligibility', product_id='PRODUCT_005')['data']
        self.assertTrue(data['eligible'])
        for product, quantity, reason in (
            ('PRODUCT_001', 1, 'INSUFFICIENT_POINTS'),
            ('PRODUCT_002', 1, 'OUT_OF_STOCK'),
            ('PRODUCT_005', 3, 'QUANTITY_LIMIT'),
        ):
            result = self.get('/points/eligibility', product_id=product, quantity=quantity)
            self.assertTrue(result['success'])
            self.assertFalse(result['data']['eligible'])
            self.assertEqual(result['data']['reason_code'], reason)
        self.assertEqual(self.get('/points/balance')['data']['available_points'], before['available_points'])
        self.assertEqual(self.get('/points/transactions')['data']['total'], 4)

    def test_invalid_parameters_and_empty_page(self):
        for path, params in (
            ('/points/eligibility', {'product_id': 'PRODUCT_005', 'quantity': 0}),
            ('/points/transactions', {'page_size': 101}),
            ('/points/expiry', {'page': 0}),
            ('/products', {'max_points': -1}),
        ):
            self.assertEqual(self.client.get('/api/v1' + path, params=params,
                                             headers=self.headers).status_code, 422)
        page = self.get('/points/transactions', page=100)['data']
        self.assertEqual(page['items'], [])
        self.assertEqual(page['total'], 4)
        self.assertFalse(self.get('/points/transactions', date_from='2026-10-25',
                                  date_to='2026-10-24')['success'])

    def test_shanghai_date_filter_and_expired_batches(self):
        from ecommerce_service.points_models import PointsBatch, PointsTransaction
        with self.sessions() as db:
            db.add(PointsTransaction(id='TXN_BOUNDARY', account_id='ACCOUNT_DEMO_001',
                                    event_type='grant', change_points=1,
                                    occurred_at=datetime(2026, 10, 23, 16, 0, tzinfo=UTC)))
            db.add(PointsBatch(id='BATCH_EXPIRED', account_id='ACCOUNT_DEMO_001',
                               available_points=9999, frozen_points=0,
                               expires_at=datetime.now(UTC) - timedelta(days=1)))
            db.commit()
        result = self.get('/points/transactions', date_from='2026-10-24', date_to='2026-10-24',
                          event_type='grant')['data']['items']
        self.assertIn('TXN_BOUNDARY', [row['transaction_id'] for row in result])
        earlier = self.get('/points/transactions', date_to='2026-10-23')['data']['items']
        self.assertNotIn('TXN_BOUNDARY', [row['transaction_id'] for row in earlier])
        self.assertEqual(self.get('/points/balance')['data']['available_points'], 4000)
        self.assertNotIn('BATCH_EXPIRED', [row['batch_id'] for row in self.get('/points/expiry')['data']])

    def test_order_deduction_and_cancel_does_not_claim_refund_completed(self):
        order_id = 'ORDER_POINTS_DEMO_001'
        self.assertEqual(self.get(f'/orders/{order_id}/points-deduction')['data']['deducted_points'], 2000)
        self.assertFalse(self.get('/orders/ORDER_001/points-deduction')['success'])
        response = self.client.post(f'/api/v1/orders/{order_id}/cancel', headers=self.headers)
        self.assertTrue(response.json()['success'])
        data = self.get(f'/orders/{order_id}/points-refund')['data']
        self.assertEqual(data['status'], 'pending')
        self.assertEqual((data['expected_points'], data['returned_points']), (2000, 0))
        self.assertEqual(self.get('/points/balance')['data']['available_points'], 4000)

    def test_project_rules_and_prices_are_isolated(self):
        from ecommerce_service.points_models import PointsRule, ProductPoints
        with self.sessions() as db:
            db.add(PointsRule(id='RULE_OTHER', project_id='other', title='转赠秘密规则',
                              content='不应返回的其他商城规则', source='other', version='v1',
                              effective_at=datetime.now(UTC) - timedelta(days=1)))
            db.add(ProductPoints(project_id='other', product_id='PRODUCT_005', points_price=1,
                                 allowed_account_types=['general'], max_per_order=99))
            db.commit()
        rules = self.get('/points/rules', query='积分能转赠吗')['data']
        self.assertTrue(rules)
        self.assertNotIn('RULE_OTHER', [row['knowledge_id'] for row in rules])
        products = self.get('/products', max_points=2500)['data']
        self.assertTrue(products)
        self.assertTrue(all(row['points_price'] <= 2500 for row in products))
        product = self.get('/products/PRODUCT_005')['data']
        self.assertEqual(product['points_price'], 2000)
        self.assertNotEqual(product['points_price'], product['price'])

    def test_selected_nondefault_account_controls_rules_and_product_prices(self):
        from ecommerce_service.points_models import PointsAccount, PointsRule, ProductPoints
        with self.sessions() as db:
            db.add(PointsAccount(id='ACCOUNT_SECOND', user_id='user_001', project_id='other',
                                 account_type='general', is_default=False, status='active'))
            db.add(ProductPoints(project_id='other', product_id='PRODUCT_005', points_price=10,
                                 allowed_account_types=['general'], max_per_order=99))
            db.add(PointsRule(id='RULE_SECOND', project_id='other', title='第二商城规则',
                              content='第二商城专属兑换规则', source='other', version='v1',
                              effective_at=datetime.now(UTC) - timedelta(days=1)))
            db.commit()
        params = {'account_id': 'ACCOUNT_SECOND'}
        self.assertEqual(self.get('/products/PRODUCT_005', **params)['data']['points_price'], 10)
        self.assertEqual(self.get('/products', max_points=10, **params)['data'][0]['points_price'], 10)
        self.assertEqual(self.get('/points/rules', **params)['data'][0]['knowledge_id'], 'RULE_SECOND')
        self.assertFalse(self.get('/products/PRODUCT_005', account_id='ACCOUNT_DEMO_002')['success'])


if __name__ == '__main__':
    unittest.main()
