"""显式创建本地临期退款演示数据，不加入服务启动流程。"""
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ecommerce_service.models import Order, OrderItem, Product, User
from ecommerce_service.points_models import (
    OrderItemPoints, OrderPoints, PointsAccount, PointsBatch, PointsRule,
    PointsTransaction, ProductPoints,
)

USER_ID = 'user_points_expiry'
PROJECT = 'DEMO_REFUND_EXPIRY'
ACCOUNT_ID = 'ACCOUNT_REFUND_EXPIRY_DEMO'
ORDER_ID = 'ORDER_REFUND_EXPIRY_DEMO'
BATCH_ID = 'BATCH_REFUND_EXPIRY_DEMO'
RULE_ID = 'RULE_REFUND_EXPIRY_DEMO'
PRODUCT_ID = 'PRODUCT_005'
GRANT_ID = 'TXN_REFUND_EXPIRY_GRANT'
CONSUME_ID = 'TXN_REFUND_EXPIRY_CONSUME'


def seed_refund_expiry_demo(db: Session, *, now: datetime | None = None) -> bool:
    """原子创建独立测试数据；调用方提交事务，重复执行保留后续业务状态。"""
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('初始化时间必须包含时区')
    # 项目也是业务隔离边界，不能只检查固定主键而复用他人的项目。
    for model, column, expected in ((PointsAccount, PointsAccount.id, ACCOUNT_ID),
                                     (PointsRule, PointsRule.id, RULE_ID),
                                     (ProductPoints, ProductPoints.product_id, PRODUCT_ID)):
        occupied = db.scalar(select(model).where(model.project_id == PROJECT,
                                                 column != expected).limit(1))
        if occupied is not None:
            raise ValueError('演示项目已被其他业务数据占用，初始化已停止')
    keys = ((User, USER_ID), (PointsAccount, ACCOUNT_ID), (Order, ORDER_ID),
            (PointsBatch, BATCH_ID), (OrderPoints, ORDER_ID), (PointsRule, RULE_ID),
            (PointsTransaction, GRANT_ID), (PointsTransaction, CONSUME_ID),
            (ProductPoints, (PROJECT, PRODUCT_ID)))
    rows = [db.get(model, key) for model, key in keys]
    if any(row is not None for row in rows):
        if not all(row is not None for row in rows):
            raise ValueError('演示编号已占用或数据不完整，初始化已停止')
        user, account, order, batch, points, rule, grant, consume, price = rows
        if (account.user_id != user.id or account.project_id != PROJECT
                or order.user_id != user.id or batch.account_id != account.id
                or points.account_id != account.id or rule.project_id != PROJECT
                or grant.account_id != account.id or consume.account_id != account.id
                or grant.event_type != 'grant' or grant.change_points != 3000
                or consume.event_type != 'consume' or consume.change_points != -2000
                or consume.order_id != order.id or points.deducted_points != 2000
                or len(order.items) != 1 or order.items[0].product_id != PRODUCT_ID
                or order.items[0].quantity != 1):
            raise ValueError('演示数据归属或历史扣分冲突，初始化已停止')
        snapshot = db.get(OrderItemPoints, order.items[0].id)
        if snapshot is None or snapshot.unit_points != 2000:
            raise ValueError('演示订单积分快照不完整，初始化已停止')
        return False

    product = db.get(Product, PRODUCT_ID)
    if product is None:
        raise ValueError('请先初始化已有演示商品 PRODUCT_005')
    shanghai = ZoneInfo('Asia/Shanghai')
    tomorrow = now.astimezone(shanghai).date() + timedelta(days=1)
    expiry = datetime.combine(tomorrow, time(23, 59, 59), tzinfo=shanghai).astimezone(UTC)
    with db.begin_nested():
        db.add(User(id=USER_ID, name='临期积分退款测试用户', membership='演示账号'))
        db.flush()
        db.add(PointsAccount(id=ACCOUNT_ID, user_id=USER_ID, project_id=PROJECT,
                             account_type='general', is_default=True, status='active'))
        order = Order(id=ORDER_ID, user_id=USER_ID, status='cancelled',
                      total_amount=Decimal('0'), address='上海市（临期退款演示地址）',
                      created_at=now - timedelta(hours=2),
                      items=[OrderItem(product_id=product.id, product_name=product.name,
                                       quantity=1, unit_price=Decimal('0'))])
        db.add(order)
        db.flush()
        # 只登记待返还，绝不伪造积分到账流水或自动延长有效期。
        db.add_all([
            ProductPoints(project_id=PROJECT, product_id=PRODUCT_ID, points_price=2000,
                          allowed_account_types=['general'], max_per_order=2, max_per_user=5),
            PointsBatch(id=BATCH_ID, account_id=ACCOUNT_ID, available_points=1000,
                        frozen_points=0, expires_at=expiry),
            OrderPoints(order_id=ORDER_ID, account_id=ACCOUNT_ID, deducted_points=2000,
                        frozen_points=0, returned_points=0, expected_points=2000,
                        refund_status='pending', updated_at=now - timedelta(minutes=30)),
            OrderItemPoints(order_item_id=order.items[0].id, unit_points=2000),
            PointsTransaction(id=GRANT_ID, account_id=ACCOUNT_ID, event_type='grant',
                              change_points=3000, occurred_at=now - timedelta(days=10)),
            PointsTransaction(id=CONSUME_ID, account_id=ACCOUNT_ID, event_type='consume',
                              change_points=-2000, order_id=ORDER_ID,
                              occurred_at=now - timedelta(hours=2)),
            PointsRule(id=RULE_ID, project_id=PROJECT, title='演示：取消订单积分返还与有效期处理',
                       content='【演示规则】取消订单不等于积分已到账，返还金额和进度以订单积分记录为准。'
                               '积分有效期以账户批次为准。返还仍待处理且担心积分过期时，应联系人工核实；'
                               '本演示不承诺自动延期、保护有效期或具体到账时间。'
                               '现有查询不能证明订单扣分来源于哪个批次，不能擅自认定返还积分的到期日。',
                       source='本地临期退款演示配置，非企业正式制度', version='demo-v1',
                       effective_at=now - timedelta(days=1)),
        ])
        db.flush()
    return True


if __name__ == '__main__':
    from ecommerce_service.database import SessionLocal, engine

    # 显式执行且限定本地开发库，避免误写虚拟机或生产环境。
    url = engine.url
    if url.host not in {'127.0.0.1', 'localhost'} or url.port != 15432 or url.database != 'ecommerce':
        raise SystemExit('只允许在本地 Docker 的 15432/ecommerce 数据库初始化演示数据')
    with SessionLocal.begin() as session:
        created = seed_refund_expiry_demo(session)
        batch = session.get(PointsBatch, BATCH_ID)
        expiry = batch.expires_at.astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
    print(f'临期退款演示数据：{"已创建" if created else "已存在，保持原状态"}；'
          f'用户={USER_ID}；订单={ORDER_ID}；首次初始化批次到期时间={expiry}')
