"""测试场景模块契约，不调用外部模型。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx


class ScenarioRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_requires_customer_jwt_and_uses_current_identity(self):
        import jwt
        from atguigu.app.app import app
        from atguigu.app.dependencies import get_agent_coordinator
        from atguigu.common.config import get_settings
        settings = get_settings()
        headers = {'X-Internal-Service-Token': settings.internal_service_token}
        app.dependency_overrides[get_agent_coordinator] = lambda: SimpleNamespace()
        try:
            with patch('atguigu.app.routers.scenario.ScenarioTestService') as service:
                service.return_value.run = AsyncMock(return_value={'passed': True})
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    response = await client.post('/internal/v1/tests/run', headers=headers, json={'case_id': '01'})
                    self.assertEqual(response.status_code, 401)
                    service.assert_not_called()
                    token = jwt.encode({'user_id': 'user_001', 'role': 'customer'}, settings.jwt_secret,
                                       algorithm=settings.jwt_algorithm)
                    response = await client.post('/internal/v1/tests/run', json={'case_id': '01'},
                        headers={**headers, 'Authorization': 'Bearer ' + token})
                    self.assertEqual(response.status_code, 200)
                    service.return_value.run.assert_awaited_once_with('01', 'user_001', token)
        finally:
            app.dependency_overrides.pop(get_agent_coordinator, None)

    async def test_case_list_requires_internal_token_and_has_twenty_questions(self):
        from atguigu.app.app import app
        from atguigu.common.config import get_settings
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            denied = await client.get('/internal/v1/tests/cases')
            self.assertEqual(denied.status_code, 403)
            response = await client.get('/internal/v1/tests/cases', headers={
                'X-Internal-Service-Token': get_settings().internal_service_token})
        self.assertEqual(response.status_code, 200)
        cases = response.json()
        self.assertEqual(len(cases), 20)
        self.assertEqual(len({case['case_id'] for case in cases}), 20)
        self.assertTrue(all(case['question'] and case['expected_tools'] for case in cases))


class ScenarioServiceTests(unittest.IsolatedAsyncioTestCase):
    def coordinator(self, state='COMPLETED', reply_type='ANSWER'):
        record = SimpleNamespace(state=state, result={
            'content': {'text': '查询结果', 'reply_type': reply_type},
            'domain_results': [{'task_id': 'task', 'status': 'SUCCESS',
                                'evidence_refs': ['task:call-0'], 'failed_tools': []}]},
                                 input_tokens=100, output_tokens=50, latency_ms=123)
        return SimpleNamespace(session=SimpleNamespace(get=AsyncMock(return_value=record)),
                               start_run=AsyncMock(return_value={'event_data': {'run_id': 'run-test'}}),
                               cancel_run=AsyncMock())

    async def run_case(self, calls, *, state='COMPLETED', reply_type='ANSWER', case_id='01'):
        from atguigu.app.services.scenario import ScenarioTestService
        coordinator = self.coordinator(state, reply_type)
        for call in calls:
            if not hasattr(call, 'arguments'):
                call.arguments = {'order_id': 'ORDER_003'} if case_id == '19' else {}
            call.tool_call_id = 'task:call-0'
        if case_id == '19':
            coordinator.session.get.return_value.result['domain_results'][0]['status'] = 'BUSINESS_FAILED'
        reader = SimpleNamespace(list_by_run_id=AsyncMock(return_value=calls))
        with patch('atguigu.app.services.scenario.ToolCallRepository', return_value=reader):
            result = await ScenarioTestService(coordinator).run(case_id, 'user_001', 'secret-token')
        self.assertNotIn('secret-token', str(result))
        return result, coordinator

    async def test_expected_tool_success_passes_and_records_usage(self):
        result, coordinator = await self.run_case([SimpleNamespace(
            tool_name='get_points_account', success=True, result={'code': 'OK'}, latency_ms=20)])
        self.assertTrue(result['passed'])
        self.assertEqual(result['token_usage'], {'input_tokens': 100, 'output_tokens': 50})
        request = coordinator.start_run.call_args.args[2]
        self.assertIn('test_', request.conversation_id)

    async def test_http_success_without_expected_tool_does_not_pass(self):
        result, _ = await self.run_case([])
        self.assertFalse(result['passed'])

    async def test_service_failure_cannot_satisfy_expected_tool(self):
        result, _ = await self.run_case([SimpleNamespace(tool_name='get_points_account', success=False,
            result={'code': 'TOOL_CALL_FAILED', 'failure_type': 'SERVICE_CALL'}, latency_ms=20)])
        self.assertFalse(result['passed'])

    async def test_wrong_arguments_cannot_satisfy_expected_tool(self):
        result, _ = await self.run_case([SimpleNamespace(tool_name='check_points_eligibility', success=True,
            result={'code': 'OK'}, latency_ms=20, arguments={'product_id': 'PRODUCT_005', 'quantity': 1})],
            case_id='07')
        self.assertFalse(result['passed'])

    async def test_unexpected_business_error_must_fail(self):
        result, _ = await self.run_case([SimpleNamespace(tool_name='list_after_sales', success=False,
            result={'code': 'ORDER_FORBIDDEN', 'failure_type': 'BUSINESS'}, latency_ms=20)],
            case_id='19', reply_type='DECLINE')
        self.assertFalse(result['passed'])

    async def test_tool_success_without_verified_domain_evidence_must_fail(self):
        from atguigu.app.services.scenario import ScenarioTestService
        coordinator = self.coordinator()
        record = coordinator.session.get.return_value
        record.result['domain_results'] = [{'task_id': 'task', 'status': 'SERVICE_FAILED',
                                            'evidence_refs': [], 'failed_tools': []}]
        reader = SimpleNamespace(list_by_run_id=AsyncMock(return_value=[SimpleNamespace(
            tool_name='get_points_account', success=True, result={'code': 'OK'},
            latency_ms=20, arguments={}, tool_call_id='task:call-0')]))
        with patch('atguigu.app.services.scenario.ToolCallRepository', return_value=reader):
            result = await ScenarioTestService(coordinator).run('01', 'user_001', 'token')
        self.assertFalse(result['passed'])

    async def test_expected_missing_aftersales_is_business_result(self):
        result, _ = await self.run_case([SimpleNamespace(tool_name='list_after_sales', success=False,
            result={'code': 'AFTER_SALE_NOT_FOUND', 'failure_type': 'BUSINESS'}, latency_ms=20)],
            case_id='19', reply_type='DECLINE')
        self.assertTrue(result['passed'])

    async def test_prepared_decision_is_cancelled_after_reading(self):
        result, coordinator = await self.run_case([], state='DECISION_PREPARED')
        coordinator.cancel_run.assert_awaited_once_with('user_001', 'run-test')
        self.assertEqual(result['run_state'], 'DECISION_PREPARED')

    async def test_unknown_case_is_rejected_before_model_call(self):
        from fastapi import HTTPException
        from atguigu.app.services.scenario import ScenarioTestService
        coordinator = self.coordinator()
        with self.assertRaises(HTTPException) as error:
            await ScenarioTestService(coordinator).run('unknown', 'user_001', 'token')
        self.assertEqual(error.exception.status_code, 422)
        coordinator.start_run.assert_not_awaited()
