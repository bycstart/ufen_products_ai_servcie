"""通过现有 AgentRun 执行客服问题，并基于工具审计检查结果。"""
from uuid import uuid4

from fastapi import HTTPException

from atguigu.agent.harness.run.coordinator import AgentRunCoordinator
from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
from atguigu.app.repositories.tool import ToolCallRepository
from atguigu.app.schemas.run import AgentRunRequest, CurrentMessage
from atguigu.app.schemas.scenario import ScenarioCase
from atguigu.models.models import AgentRun, AgentRunState


def case(number, category, question, *tools, **options):
    return ScenarioCase(case_id=f'{number:02}', category=category, question=question,
                        expected_tools=tools, **options)


SCENARIO_CASES = (
    case(1, '积分余额', '我目前有多少可用积分，还有多少积分被冻结了？', 'get_points_account'),
    case(2, '积分流水', '帮我看看最近的积分变动明细。', 'get_points_transactions'),
    case(3, '积分到期', '我的积分分别在什么时候到期？请列出到期批次。', 'get_points_account',
         expected_arguments={'get_points_account': {'include_expiry': True}}),
    case(4, '积分发放', '帮我查询积分发放流水，只看发放记录。', 'get_points_transactions',
         expected_arguments={'get_points_transactions': {'event_type': 'grant'}}),
    case(5, '兑换预校验', '我想用积分兑换一个 PRODUCT_005，现在满足兑换条件吗？', 'check_points_eligibility',
         expected_arguments={'check_points_eligibility': {'product_id': 'PRODUCT_005', 'quantity': 1}}),
    case(6, '缺货兑换', '我想兑换一个 PRODUCT_002，请检查当前是否可以兑换。', 'check_points_eligibility',
         expected_arguments={'check_points_eligibility': {'product_id': 'PRODUCT_002', 'quantity': 1}}),
    case(7, '兑换限量', '我想一次用积分兑换三个 PRODUCT_005，能兑换吗？', 'check_points_eligibility',
         expected_arguments={'check_points_eligibility': {'product_id': 'PRODUCT_005', 'quantity': 3}}),
    case(8, '订单扣分', 'ORDER_POINTS_DEMO_001 这个订单实际扣了多少积分？', 'get_order_points',
         expected_arguments={'get_order_points': {'order_id': 'ORDER_POINTS_DEMO_001'}}),
    case(9, '积分返还', '查一下 ORDER_POINTS_DEMO_001 的积分返还状态。', 'get_order_points',
         expected_arguments={'get_order_points': {'order_id': 'ORDER_POINTS_DEMO_001'}}),
    case(10, '积分转赠规则', '商城积分可以转赠给别人吗？请查询当前商城规则。', 'search_knowledge',
         expected_arguments={'search_knowledge': {'topic': 'points'}}),
    case(11, '积分有效期规则', '商城积分过期以后还能兑换吗？请查有效期规则。', 'search_knowledge',
         expected_arguments={'search_knowledge': {'topic': 'points'}}),
    case(12, '积分退款规则', '订单取消后就代表积分退回来了吗？请查积分返还规则。', 'search_knowledge',
         expected_arguments={'search_knowledge': {'topic': 'points'}}),
    case(13, '积分选品', '帮我找单件兑换价格不超过2500积分的商品。', 'search_products',
         expected_arguments={'search_products': {'max_points': 2500}}),
    case(14, '商品积分价格', '查询 PRODUCT_005 的商品详情和兑换积分价格。', 'get_product',
         expected_arguments={'get_product': {'product_id': 'PRODUCT_005'}}),
    case(15, '商品库存', 'PRODUCT_002 现在有货吗？请查实时库存。', 'get_product_stock',
         expected_arguments={'get_product_stock': {'product_id': 'PRODUCT_002'}}),
    case(16, '订单列表', '帮我查一下我的订单列表。', 'list_orders'),
    case(17, '订单详情', 'ORDER_001 现在是什么状态，订单金额是多少？', 'get_order',
         expected_arguments={'get_order': {'order_id': 'ORDER_001'}}),
    case(18, '物流进度', 'ORDER_002 到哪里了，物流公司和运单号是什么？', 'get_logistics',
         expected_arguments={'get_logistics': {'order_id': 'ORDER_002'}}),
    case(19, '售后进度', '帮我查一下 ORDER_003 有没有售后申请，处理到哪一步了？',
         'list_after_sales', expected_reply_types=('ANSWER', 'DECLINE'),
         allowed_failure_codes=('AFTER_SALE_NOT_FOUND',),
         expected_arguments={'list_after_sales': {'order_id': 'ORDER_003'}}),
    case(20, '跨领域查询', '我目前有多少可用积分？再帮我查 PRODUCT_005 的商品详情，'
         '并检查兑换一个是否满足条件。', 'get_points_account', 'get_product', 'check_points_eligibility',
         expected_arguments={'get_product': {'product_id': 'PRODUCT_005'},
                             'check_points_eligibility': {'product_id': 'PRODUCT_005', 'quantity': 1}}),
)


class ScenarioTestService:
    def __init__(self, coordinator: AgentRunCoordinator):
        self.coordinator = coordinator

    async def run(self, case_id: str, user_id: str, access_token: str) -> dict:
        scenario = next((item for item in SCENARIO_CASES if item.case_id == case_id), None)
        if scenario is None:
            raise HTTPException(status_code=422, detail='未知测试用例，请先查询测试问题列表')
        identifier = 'test_' + uuid4().hex
        request = AgentRunRequest(conversation_id=identifier, turn_id=identifier,
                                  messages=[CurrentMessage(message_id=identifier, type='text',
                                                            content={'text': scenario.question})])
        event = await self.coordinator.start_run(user_id, access_token, request)
        run_id = event['event_data']['run_id']
        run = await self.coordinator.session.get(AgentRun, run_id)
        state = run.state
        outcome = run.result or {}
        # 测试读取候选页面回复后取消决策，不确认动作、不发布用户聊天。
        if state == AgentRunState.DECISION_PREPARED:
            await self.coordinator.cancel_run(user_id, run_id)
        calls = await ToolCallRepository(self.coordinator.session).list_by_run_id(run_id)
        content = outcome.get('content') or {}
        reply = content.get('text') or outcome.get('message') or ''
        reply_type = content.get('reply_type') or (
            'REQUEST_HANDOFF' if state == AgentRunState.HANDED_OFF else None)
        domains = outcome.get('domain_results', [])
        # 任务编号来自执行上下文，不能通过工具名称判断所属专家（多个专家会共享工具）。
        task_agents = {call['task_id']: call.get('agent_code', 'unknown')
                       for call in outcome.get('model_metrics', {}).get('calls', []) if call.get('task_id')}
        task_agents.update({domain['task_id']: domain.get('agent_code', 'unknown') for domain in domains})

        def tool_agent(call):
            code = task_agents.get(call.tool_call_id.split(':', 1)[0], 'unknown')
            name = f'{DOMAIN_SPECS[code].label}子智能体' if code in DOMAIN_SPECS else '未知智能体'
            return {'agent_code': code, 'agent_name': name}

        tools = [{'tool_name': call.tool_name, 'success': call.success, **tool_agent(call),
                  'tool_call_id': call.tool_call_id,
                  'arguments': call.arguments,
                  'code': (call.result or {}).get('code'),
                  'failure_type': (call.result or {}).get('failure_type'),
                  'latency_ms': call.latency_ms} for call in calls]
        verified_refs = {ref for domain in domains if domain['status'] == 'SUCCESS'
                         for ref in domain['evidence_refs']}
        usable = {tool['tool_name'] for tool in tools if (
            (tool['success'] is True and tool['tool_call_id'] in verified_refs) or (
                tool['failure_type'] == 'BUSINESS' and tool['code'] in scenario.allowed_failure_codes
                and any(domain['status'] == 'BUSINESS_FAILED'
                        and tool['tool_call_id'].startswith(domain['task_id'] + ':') for domain in domains))) and all(
                tool['arguments'].get(key) == value
                for key, value in scenario.expected_arguments.get(tool['tool_name'], {}).items())}
        execution_succeeded = state in {
            AgentRunState.COMPLETED, AgentRunState.DECISION_PREPARED, AgentRunState.HANDED_OFF}
        checks = [
            {'name': '智能体执行完成', 'passed': execution_succeeded},
            {'name': '回复非空', 'passed': bool(reply.strip())},
            {'name': '回复类型符合预期', 'passed': reply_type in scenario.expected_reply_types},
            {'name': '无工具网络或契约失败', 'passed': not any(
                tool['failure_type'] in {'SERVICE_CALL', 'CONTRACT'} for tool in tools)},
            *[{'name': f'调用预期工具 {name}', 'passed': name in usable}
              for name in scenario.expected_tools],
        ]
        return {'case_id': case_id, 'category': scenario.category, 'question': scenario.question,
                'run_id': run_id, 'run_state': state, 'execution_succeeded': execution_succeeded,
                'reply_type': reply_type, 'reply_content': reply, 'tool_calls': tools,
                'domain_results': domains,
                'model_metrics': outcome.get('model_metrics', {}),
                'tool_call_count': len(tools),
                'tool_success_count': sum(tool['success'] is True for tool in tools),
                'tool_total_latency_ms': sum(tool['latency_ms'] or 0 for tool in tools),
                'latency_ms': run.latency_ms,
                'token_usage': {'input_tokens': run.input_tokens, 'output_tokens': run.output_tokens},
                'checks': checks, 'passed': all(check['passed'] for check in checks)}
