import importlib
import unittest
from unittest.mock import patch


class RunBudgetTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('atguigu.agent.multi_agent.budget')
        except ModuleNotFoundError:
            self.fail('RunBudget module has not been implemented')
        self.Budget = self.module.RunBudget
        self.Exceeded = self.module.BudgetExceeded

    def assert_exhausted(self, code, operation):
        with self.assertRaises(self.Exceeded) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_six_delegations_share_one_run_limit(self):
        budget = self.Budget()
        for _ in range(6):
            budget.consume_delegation()
        self.assert_exhausted('delegation_budget_exceeded', budget.consume_delegation)

    def test_task_rejection_does_not_consume_global_allowance(self):
        budget = self.Budget(task_tool_limit=1, total_tool_limit=2)
        budget.consume_tool('first')
        self.assert_exhausted('task_tool_budget_exceeded', lambda: budget.consume_tool('first'))
        budget.consume_tool('second')
        self.assert_exhausted('total_tool_budget_exceeded', lambda: budget.consume_tool('third'))

    def test_default_task_tool_limit_is_eight(self):
        budget = self.Budget()
        for _ in range(8):
            budget.consume_tool('task')
        self.assert_exhausted('task_tool_budget_exceeded', lambda: budget.consume_tool('task'))

    def test_global_limit_shared_by_three_tasks(self):
        budget = self.Budget()
        for task in ('one', 'two', 'three'):
            for _ in range(8):
                budget.consume_tool(task)
        self.assert_exhausted('total_tool_budget_exceeded', lambda: budget.consume_tool('four'))

    def test_supervisor_model_allowance_shared_across_scopes(self):
        budget = self.Budget()
        for i in range(8):
            budget.consume_model(str(i), supervisor=True)
        self.assert_exhausted('supervisor_model_budget_exceeded', lambda: budget.consume_model('correction', supervisor=True))
        budget.consume_model('specialist')

    def test_specialist_models_count_per_task(self):
        budget = self.Budget()
        for _ in range(10):
            budget.consume_model('one')
        self.assert_exhausted('specialist_model_budget_exceeded', lambda: budget.consume_model('one'))
        budget.consume_model('two')

    def test_request_instances_have_independent_counters(self):
        first = self.Budget(delegation_limit=1, task_tool_limit=1, specialist_model_limit=1)
        second = self.Budget(delegation_limit=1, task_tool_limit=1, specialist_model_limit=1)
        for budget in (first, second):
            budget.consume_delegation()
            budget.consume_tool('same')
            budget.consume_model('same')

    def test_monotonic_deadline_and_remaining_seconds(self):
        with patch.object(self.module.time, 'monotonic', return_value=100) as clock:
            budget = self.Budget()
            clock.return_value = 219
            budget.check_deadline()
            self.assertEqual(budget.remaining_seconds, 1)
            clock.return_value = 220
            self.assert_exhausted('run_timeout', budget.check_deadline)
            clock.return_value = 230
            self.assertEqual(budget.remaining_seconds, 0)

    def test_every_consumption_checks_deadline(self):
        with patch.object(self.module.time, 'monotonic', return_value=10) as clock:
            budget = self.Budget(timeout_seconds=1)
            clock.return_value = 11
            for operation in (budget.consume_delegation, lambda: budget.consume_tool('secret'), lambda: budget.consume_model('secret')):
                self.assert_exhausted('run_timeout', operation)
            clock.return_value = 10
            budget.consume_delegation()

    def test_exception_does_not_expose_task_identifier(self):
        budget = self.Budget(task_tool_limit=0)
        error = self.assert_exhausted('task_tool_budget_exceeded', lambda: budget.consume_tool('token=very-secret'))
        self.assertNotIn('very-secret', str(error))
        self.assertNotIn('very-secret', repr(error))


if __name__ == '__main__':
    unittest.main()
