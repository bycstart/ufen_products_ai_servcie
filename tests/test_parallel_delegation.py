"""验证独立查询并行、请求级限流、重复委派与取消清理。"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.multi_agent.delegation import DelegationService
from atguigu.agent.multi_agent.state import RunExecution


class ParallelDelegationTests(unittest.IsolatedAsyncioTestCase):
    def context(self, **kwargs):
        return AgentRuntimeContext('r', 'c', 'u', 's', execution=RunExecution(**kwargs))

    async def test_duplicate_inflight_query_runs_once(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def invoke(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'structured_response': {'status': 'NEEDS_CLARIFICATION', 'clarification': '请提供订单号。'}}
        expert = SimpleNamespace(ainvoke=AsyncMock(side_effect=invoke))
        service, context = DelegationService({'order': expert}), self.context()
        first = asyncio.create_task(service.delegate('order', '查询订单', context, {}))
        second = asyncio.create_task(service.delegate('order', ' 查询订单 ', context, {}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            result = await second
            self.assertEqual(expert.ainvoke.await_count, 1)
            self.assertEqual(context.execution.results, [result])
            self.assertEqual(context.execution.pending_delegations, {})
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)

    async def test_concurrency_limit_allows_two_and_queues_third(self):
        entered, release = asyncio.Queue(), asyncio.Event()
        active, peak = 0, 0
        async def invoke(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.put_nowait(kwargs['context'].agent_code)
            try:
                await release.wait()
            finally:
                active -= 1
            return {'structured_response': {'status': 'NEEDS_CLARIFICATION', 'clarification': '请补充查询信息。'}}
        expert = SimpleNamespace(ainvoke=invoke)
        service = DelegationService({code: expert for code in ('order', 'logistics', 'product')})
        context = self.context(parallel_limit=2)
        tasks = [asyncio.create_task(service.delegate(code, code, context, {})) for code in service.agents]
        try:
            await asyncio.wait_for(entered.get(), 1)
            await asyncio.wait_for(entered.get(), 1)
            self.assertTrue(entered.empty())
            self.assertEqual(active, 2)
            release.set()
            await asyncio.gather(*tasks)
            self.assertEqual(peak, 2)
            self.assertEqual(len(context.execution.results), 3)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_read_tools_overlap_and_evidence_stays_scoped(self):
        from atguigu.agent.harness.tools.executor import ToolExecutor
        executor = ToolExecutor()
        executor._create_tool_call = AsyncMock()
        executor._save_tool_result = AsyncMock()
        entered, release = asyncio.Queue(), asyncio.Event()
        async def operation():
            entered.put_nowait(True)
            await release.wait()
            return {'success': True, 'code': 'OK', 'message': 'OK', 'data': {}}
        context = self.context()
        children = [context.for_task(code, ('get_order',)) for code in ('order', 'logistics')]
        tasks = [asyncio.create_task(executor.execute(child, 'same-id', 'get_order', {}, operation, dict))
                 for child in children]
        try:
            await asyncio.wait_for(entered.get(), 1)
            await asyncio.wait_for(entered.get(), 1)
            release.set()
            results = [json.loads(value) for value in await asyncio.gather(*tasks)]
            self.assertNotEqual(results[0]['evidence_id'], results[1]['evidence_id'])
            for child, result in zip(children, results):
                self.assertEqual(context.execution.evidence[child.task_id][0].tool_call_id, result['evidence_id'])
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_root_failure_cancels_pending_worker_before_return(self):
        from atguigu.agent.harness.run.executor import AgentExecutor
        from atguigu.agent.multi_agent.definition import DomainResult
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def invoke(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        service, context = DelegationService({'order': SimpleNamespace(ainvoke=invoke)}), self.context()
        waiter = None
        async def supervisor(*args, **kwargs):
            nonlocal waiter
            waiter = asyncio.create_task(service.delegate('order', '查订单', context, {}))
            await entered.wait()
            context.execution.results.append(DomainResult(task_id='done', agent_code='policy',
                status='SUCCESS', summary='已查询规则。'))
            raise RuntimeError('failed supervisor')
        executor = AgentExecutor(SimpleNamespace(ainvoke=supervisor), AsyncMock())
        executor.context_compiler.compile_messages = lambda _: []
        try:
            result = await executor.execute(None, context)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(context.execution.pending_delegations, {})
            self.assertEqual(len(result.domain_results), 1)
        finally:
            if waiter:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    async def test_only_supervisor_requests_parallel_tool_calls(self):
        from atguigu.agent.multi_agent.middleware import ExecutionBudgetMiddleware
        context = self.context()
        for child, expected in ((context, True), (context.for_task('order', ('get_order',)), False)):
            request = SimpleNamespace(runtime=SimpleNamespace(context=child), tools=['tool'],
                                      model_settings={}, override=lambda **kwargs: kwargs)
            handler = AsyncMock()
            await ExecutionBudgetMiddleware().awrap_model_call(request, handler)
            self.assertEqual(handler.call_args.args[0]['model_settings']['parallel_tool_calls'], expected)

    async def test_parallel_tools_share_one_budget_without_overshoot(self):
        from atguigu.agent.harness.tools.executor import ToolExecutor
        from atguigu.agent.multi_agent.budget import BudgetExceeded, RunBudget
        executor = ToolExecutor()
        executor._create_tool_call = AsyncMock()
        executor._save_tool_result = AsyncMock()
        context = self.context(budget=RunBudget(total_tool_limit=1))
        async def operation():
            await asyncio.sleep(0)
            return {'success': True, 'code': 'OK', 'message': 'OK', 'data': {}}
        outcomes = await asyncio.gather(*(executor.execute(context.for_task(code, ('get_order',)),
            'id', 'get_order', {}, operation, dict) for code in ('order', 'logistics')), return_exceptions=True)
        self.assertEqual(sum(isinstance(value, BudgetExceeded) for value in outcomes), 1)
        self.assertEqual(executor._create_tool_call.await_count, 1)

    async def test_root_cancellation_waits_for_worker_cleanup(self):
        from atguigu.agent.harness.run.executor import AgentExecutor
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def invoke(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        context = self.context()
        service = DelegationService({'order': SimpleNamespace(ainvoke=invoke)})
        async def supervisor(*args, **kwargs):
            await service.delegate('order', '查订单', context, {})
        executor = AgentExecutor(SimpleNamespace(ainvoke=supervisor), AsyncMock())
        executor.context_compiler.compile_messages = lambda _: []
        task = asyncio.create_task(executor.execute(None, context))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cancelled.is_set())
            self.assertEqual(context.execution.pending_delegations, {})
            self.assertEqual(context.execution.results, [])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failure_cleanup_can_finish_after_execution_deadline(self):
        from atguigu.agent.harness.run.executor import AgentExecutor
        from atguigu.agent.multi_agent.budget import RunBudget
        from atguigu.agent.multi_agent.definition import DomainResult
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def invoke(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                # 清理跨过执行期限，也必须完成，不能被第二个超时中断。
                await asyncio.sleep(.15)
                cleaned.set()
        context = self.context(budget=RunBudget(timeout_seconds=.1))
        service = DelegationService({'order': SimpleNamespace(ainvoke=invoke)})
        waiter = None
        async def supervisor(*args, **kwargs):
            nonlocal waiter
            waiter = asyncio.create_task(service.delegate('order', '查订单', context, {}))
            await entered.wait()
            context.execution.results.append(DomainResult(task_id='done', agent_code='policy',
                status='SUCCESS', summary='已查询规则。'))
            raise RuntimeError('root failed')
        executor = AgentExecutor(SimpleNamespace(ainvoke=supervisor), AsyncMock())
        executor.context_compiler.compile_messages = lambda _: []
        try:
            await executor.execute(None, context)
            self.assertTrue(cleaned.is_set())
            self.assertEqual(context.execution.pending_delegations, {})
        finally:
            if waiter:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
