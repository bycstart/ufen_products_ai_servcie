"""验证本地 Docker 依赖、真实积分 HTTP 调用及 AI 审计入库，不调用大模型。"""
from datetime import UTC, datetime
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import httpx
import redis.asyncio as redis
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from configure import AI, CUSTOMER, ECOMMERCE, read_values, verify_local_configuration

verify_local_configuration()
sys.path.insert(0, str(AI))

from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.harness.tools import points, read
from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
from atguigu.agent.multi_agent.state import RunExecution
from atguigu.app.services.auth import AuthService
from atguigu.common.event_loop import run_async
from atguigu.common.config import get_settings
from atguigu.infrastructure.db import db_engine, session_factory
from atguigu.models.models import AgentRun, AgentRunState, AgentToolCall


async def check():
    # 配置核验限定为本地地址，防止测试记录写进原虚拟机数据库。
    verify_local_configuration()
    configuration = {name: read_values(root / '.env') for name, root in (
        ('ai', AI), ('customer', CUSTOMER), ('ecommerce', ECOMMERCE))}
    settings = get_settings()
    if (db_engine.url != make_url(configuration['ai']['AI_DATABASE_URL'])
            or settings.ecommerce_base_url != configuration['ai']['ECOMMERCE_BASE_URL']
            or settings.jwt_secret != configuration['ai']['JWT_SECRET']):
        raise SystemExit('实际生效的 AI 配置与本地目标不同，已停止联调')
    for name, key in (('ai', 'AI_DATABASE_URL'), ('customer', 'DATABASE_URL'),
                      ('ecommerce', 'ECOM_DATABASE_URL')):
        engine = create_engine(configuration[name][key], connect_args={'connect_timeout': 3})
        try:
            with engine.connect() as connection:
                connection.execute(text('SELECT 1'))
            print(f'PostgreSQL {name}: OK')
        finally:
            engine.dispose()
    client = redis.from_url(configuration['customer']['REDIS_URL'], socket_timeout=3)
    key = 'local-smoke:' + uuid4().hex
    try:
        await client.set(key, 'ok', ex=30)
        assert await client.get(key) == b'ok'
        await client.delete(key)
        print('Redis read/write: OK')
    finally:
        await client.aclose()

    async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
        response = await http.get('http://127.0.0.1:8002/openapi.json')
        response.raise_for_status()
        assert '/internal/v1/agent/runs' in response.json()['paths']
        print('AI HTTP service: OK')
        response = await http.post('http://127.0.0.1:8001/api/v1/auth/demo-token',
                                   json={'user_id': 'user_001'})
        response.raise_for_status()
        token = response.json()['access_token']
        AuthService().get_authorized_user('Bearer ' + token, 'customer')
        response = await http.post('http://127.0.0.1:8000/api/v1/conversations/current',
                                   headers={'Authorization': 'Bearer ' + token})
        response.raise_for_status()
        print('Ecommerce / AI / Customer JWT: OK')

    run_id = 'run_local_check_' + uuid4().hex
    async with session_factory() as session:
        session.add(AgentRun(id=run_id, conversation_id='local-check', user_id='user_001',
                             turn_id=run_id, model_name='local-http-check', prompt_version='smoke-v1',
                             input_context={'source': 'local-docker-check'}))
        await session.commit()
    context = AgentRuntimeContext(run_id, 'local-check', 'user_001', token, execution=RunExecution())
    tasks = {domain: context.for_task(domain, DOMAIN_SPECS[domain].tools)
             for domain in ('points', 'product', 'policy')}
    cases = (
        (points.get_points_account, {'include_expiry': True}, 'points'),
        (points.get_points_transactions, {}, 'points'),
        (points.check_points_eligibility, {'product_id': 'PRODUCT_005'}, 'points'),
        (points.get_order_points, {'order_id': 'ORDER_POINTS_DEMO_001'}, 'points'),
        (read.search_products, {'query': '', 'max_points': 2500}, 'product'),
        (read.get_product, {'product_id': 'PRODUCT_005', 'account_id': 'ACCOUNT_DEMO_001'}, 'product'),
        (read.search_knowledge, {'query': '积分转赠规则', 'topic': 'points',
                                 'account_id': 'ACCOUNT_DEMO_001'}, 'policy'),
    )
    try:
        for index, (tool, arguments, domain) in enumerate(cases):
            runtime = SimpleNamespace(context=tasks[domain], tool_call_id=f'local-{index}')
            result = json.loads(await tool.coroutine(runtime=runtime, **arguments))
            if not result['success']:
                raise RuntimeError(f'{tool.name} 未通过：{result["code"]}')
            print(f'{tool.name}: HTTP + schema + evidence + audit OK')
        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(AgentToolCall).where(
                AgentToolCall.run_id == run_id, AgentToolCall.success.is_(True)))
            if count != len(cases):
                raise RuntimeError('AI 工具审计记录数量不匹配')
            run = await session.get(AgentRun, run_id)
            run.state = AgentRunState.COMPLETED
            run.finished_at = datetime.now(UTC)
            run.result = {'local_check': True, 'successful_tools': count}
            await session.commit()
        print(f'Local Docker integration passed: {len(cases)} real tool calls; model inference not invoked.')
    finally:
        await db_engine.dispose()


if __name__ == '__main__':
    run_async(check())
