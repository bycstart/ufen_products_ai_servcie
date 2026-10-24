"""迁移仅在现有测试 fixture 创建的独立 SQLite 数据库执行。"""
import unittest
from decimal import Decimal

from sqlalchemy import select

from tests import test_ecommerce_points as fixture


@unittest.skipUnless(fixture.BACKEND.is_dir(), '同级 Ecommerce 源码不存在')
class PointsMigrationTests(unittest.TestCase):
    setUp = fixture.EcommercePointsTests.setUp
    tearDown = fixture.EcommercePointsTests.tearDown
    get = fixture.EcommercePointsTests.get

    def migrate(self):
        from ecommerce_service.points_migration import migrate_demo_orders
        with self.sessions() as db:
            return migrate_demo_orders(db)

    def balances(self):
        from ecommerce_service.points_models import PointsBatch, PointsTransaction
        with self.sessions() as db:
            batches = [(row.id, row.available_points, row.frozen_points)
                       for row in db.scalars(select(PointsBatch).order_by(PointsBatch.id))]
            sums = {}
            for row in db.scalars(select(PointsTransaction)):
                sums[row.account_id] = sums.get(row.account_id, 0) + row.change_points
            return batches, sums

    def test_migration_is_idempotent_preserves_balance_and_uses_points_prices(self):
        before = self.balances()
        self.assertEqual(self.migrate(), 4)
        self.assertEqual(self.migrate(), 0)
        self.assertEqual(self.balances(), before)
        for order_id, expected in (('ORDER_001', 5000), ('ORDER_002', 2500),
                                   ('ORDER_003', 3000), ('ORDER_POINTS_DEMO_001', 2000)):
            data = self.get('/orders/' + order_id)['data']
            self.assertEqual((data['payment_type'], data['total_points']), ('points', expected))
            self.assertIsNone(data['total_amount'])
            self.assertIsNone(data['items'][0]['unit_price'])
            self.assertEqual(data['items'][0]['unit_points'], expected)
        self.assertFalse(self.get('/orders/ORDER_004')['success'])
        from ecommerce_service.models import Order
        with self.sessions() as db:
            self.assertEqual(db.get(Order, 'ORDER_001').total_amount, Decimal('899.00'))

    def test_unmigrated_cash_orders_remain_explicit(self):
        data = self.get('/orders/ORDER_001')['data']
        self.assertEqual(data['payment_type'], 'cash')
        self.assertEqual(data['total_amount'], 899)
        self.assertIsNone(data['total_points'])
        self.assertIsNone(data['items'][0]['unit_points'])
        product = self.get('/products/PRODUCT_001')['data']
        self.assertIsNone(product['price'])
        self.assertEqual(product['points_price'], 5000)

    def test_history_is_preserved_when_current_price_changes(self):
        from ecommerce_service.points_models import ProductPoints
        with self.sessions() as db:
            db.get(ProductPoints, ('DEMO_MALL', 'PRODUCT_005')).points_price = 9999
            db.commit()
        self.migrate()
        self.assertEqual(self.get('/orders/ORDER_POINTS_DEMO_001')['data']['items'][0]['unit_points'], 2000)
        with self.sessions() as db:
            db.get(ProductPoints, ('DEMO_MALL', 'PRODUCT_001')).points_price = 7777
            db.commit()
        self.migrate()
        self.assertEqual(self.get('/orders/ORDER_001')['data']['total_points'], 5000)

    def test_conflict_rolls_back_all_orders(self):
        from ecommerce_service.models import Order
        from ecommerce_service.points_models import OrderPoints, OrderItemPoints, PointsTransaction
        cached = self.client.patch('/api/v1/orders/ORDER_001/address',
                                   headers={**self.headers, 'Idempotency-Key': 'rollback-cache'},
                                   json={'address': '上海市迁移前缓存地址'}).json()
        with self.sessions() as db:
            db.get(Order, 'ORDER_004').user_id = 'user_001'
            db.commit()
        before = self.balances()
        with self.assertRaises(ValueError):
            self.migrate()
        self.assertEqual(self.balances(), before)
        with self.sessions() as db:
            from ecommerce_service.models import IdempotencyRecord
            self.assertEqual(db.get(IdempotencyRecord, 'rollback-cache').response, cached)
            self.assertIsNone(db.get(OrderPoints, 'ORDER_001'))
            self.assertEqual(list(db.scalars(select(OrderItemPoints))), [])
            self.assertIsNone(db.get(PointsTransaction, 'TXN_MIGRATION_GRANT_ORDER_001'))

    def test_snapshot_conflict_and_account_ownership_are_rejected(self):
        from ecommerce_service.models import Order
        from ecommerce_service.points_models import OrderItemPoints, PointsAccount
        with self.sessions() as db:
            item = db.get(Order, 'ORDER_POINTS_DEMO_001').items[0]
            db.add(OrderItemPoints(order_item_id=item.id, unit_points=1))
            db.commit()
        with self.assertRaises(ValueError):
            self.migrate()
        with self.sessions() as db:
            db.delete(db.get(OrderItemPoints, item.id))
            db.get(PointsAccount, 'ACCOUNT_DEMO_002').user_id = 'user_001'
            db.commit()
        with self.assertRaises(ValueError):
            self.migrate()

    def test_cancelled_history_and_future_cancellation_do_not_fake_refunds(self):
        from ecommerce_service.models import Order
        with self.sessions() as db:
            db.get(Order, 'ORDER_002').status = 'refunded'
            db.commit()
        before = self.balances()
        self.migrate()
        refund = self.get('/orders/ORDER_002/points-refund')['data']
        self.assertEqual((refund['status'], refund['expected_points'], refund['returned_points']),
                         ('pending', 2500, 0))
        result = self.client.post('/api/v1/orders/ORDER_001/cancel', headers=self.headers)
        self.assertTrue(result.json()['success'])
        self.assertEqual(result.json()['data']['total_points'], 5000)
        refund = self.get('/orders/ORDER_001/points-refund')['data']
        self.assertEqual((refund['status'], refund['returned_points']), ('pending', 0))
        self.assertEqual(self.balances(), before)

    def test_missing_points_price_rolls_back_and_other_cash_orders_are_untouched(self):
        from ecommerce_service.models import Order, OrderItem
        from ecommerce_service.points_models import ProductPoints, OrderPoints
        with self.sessions() as db:
            db.add(Order(id='ORDER_CUSTOM', user_id='user_001', status='paid',
                         total_amount=Decimal('899.00'), address='测试地址',
                         items=[OrderItem(product_id='PRODUCT_001', product_name='测试商品',
                                          quantity=1, unit_price=Decimal('899.00'))]))
            db.commit()
            db.delete(db.get(ProductPoints, ('DEMO_MALL', 'PRODUCT_003')))
            db.commit()
        with self.assertRaises(ValueError):
            self.migrate()
        with self.sessions() as db:
            self.assertIsNone(db.get(OrderPoints, 'ORDER_001'))
            self.assertIsNone(db.get(OrderPoints, 'ORDER_CUSTOM'))
            db.add(ProductPoints(project_id='DEMO_MALL', product_id='PRODUCT_003',
                                 points_price=8000, allowed_account_types=['general'], max_per_order=2))
            db.commit()
        self.migrate()
        data = self.get('/orders/ORDER_CUSTOM')['data']
        self.assertEqual((data['payment_type'], data['total_amount']), ('cash', 899))
        self.assertIsNone(data['total_points'])

    def test_conflicting_transaction_rolls_back(self):
        from ecommerce_service.points_models import PointsTransaction, OrderPoints
        with self.sessions() as db:
            db.add(PointsTransaction(id='TXN_MIGRATION_GRANT_ORDER_003',
                                    account_id='ACCOUNT_DEMO_001', event_type='grant', change_points=1))
            db.commit()
        with self.assertRaises(ValueError):
            self.migrate()
        with self.sessions() as db:
            self.assertIsNone(db.get(OrderPoints, 'ORDER_001'))

    def test_cached_write_responses_migrate_without_replaying_old_operations(self):
        address_headers = {**self.headers, 'Idempotency-Key': 'old-address'}
        cancel_headers = {**self.headers, 'Idempotency-Key': 'old-cancel'}
        first = self.client.patch('/api/v1/orders/ORDER_001/address', headers=address_headers,
                                  json={'address': '上海市旧地址一号'}).json()
        self.assertEqual(first['data']['total_amount'], 899)
        self.client.patch('/api/v1/orders/ORDER_001/address', headers=self.headers,
                          json={'address': '上海市最新地址二号'})
        cancelled = self.client.post('/api/v1/orders/ORDER_004/cancel',
                                     headers=cancel_headers).json()
        # 当前用户不能操作其他用户订单，不得把这种失败缓存误迁移。
        self.assertFalse(cancelled['success'])
        cancelled = self.client.post('/api/v1/orders/ORDER_001/cancel',
                                     headers=cancel_headers).json()
        self.assertEqual(cancelled['data']['total_amount'], 899)
        self.migrate()
        for method, suffix, headers, body, original in (
            ('patch', 'address', address_headers, {'address': '上海市重试地址三号'}, first),
            ('post', 'cancel', cancel_headers, None, cancelled),
        ):
            response = getattr(self.client, method)(
                '/api/v1/orders/ORDER_001/' + suffix, headers=headers,
                **({'json': body} if body else {})).json()
            expected = {**original, 'data': {**original['data'], 'payment_type': 'points',
                        'total_points': 5000, 'total_amount': None,
                        'items': [{**original['data']['items'][0], 'unit_price': None,
                                   'unit_points': 5000}]}}
            self.assertEqual(response, expected)
        current = self.get('/orders/ORDER_001')['data']
        self.assertEqual(current['address'], '上海市最新地址二号')
        self.assertEqual(current['status'], 'cancelled')
        self.assertEqual(self.migrate(), 0)


if __name__ == '__main__':
    unittest.main()
