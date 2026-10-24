from langchain.agents.middleware import AgentMiddleware


class ExecutionBudgetMiddleware(AgentMiddleware):
    """限制真实模型调用；纠错与子任务不能重置请求级预算。"""

    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        if context is None or context.execution is None:
            raise RuntimeError('Agent 必须通过带执行上下文的执行器调用')
        context.execution.budget.consume_model(context.task_id or 'supervisor',
                                                supervisor=context.agent_code == 'supervisor')
        return await handler(request.override(
            tools=[] if context.execution.finalizing else request.tools,
            model_settings={**request.model_settings,
                            'parallel_tool_calls': context.agent_code == 'supervisor'
                                                   and not context.execution.finalizing},
        ))
