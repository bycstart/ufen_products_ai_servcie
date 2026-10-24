from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy

from atguigu.agent.harness.dynamic_prompt import render_domain_prompt, render_supervisor_prompt
from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.harness.tools.catalog import TOOL_CATALOG
from atguigu.agent.llm.adapter import ModelAdapter
from atguigu.agent.llm.output import AgentOutput
from atguigu.agent.multi_agent.definition import DomainAgentOutput
from atguigu.agent.multi_agent.delegation import DelegationService
from atguigu.agent.multi_agent.middleware import ExecutionBudgetMiddleware
from atguigu.agent.multi_agent.registry import DOMAIN_SPECS


def create_support_agent():
    """应用启动时按领域配置构建主控和专家；请求状态通过 context 单独注入。"""
    experts = {
        code: create_agent(
            model=ModelAdapter.create_model(),
            tools=[TOOL_CATALOG.get_tool_definition(name).tool for name in spec.tools],
            system_prompt=render_domain_prompt(code),
            response_format=ToolStrategy(DomainAgentOutput),
            context_schema=AgentRuntimeContext,
            middleware=[ExecutionBudgetMiddleware()],
            name=f'{code}_specialist',
        )
        for code, spec in DOMAIN_SPECS.items()
    }
    return create_agent(
        model=ModelAdapter.create_model(),
        tools=DelegationService(experts).tools(),
        system_prompt=render_supervisor_prompt(),
        response_format=ToolStrategy(AgentOutput),
        context_schema=AgentRuntimeContext,
        middleware=[ExecutionBudgetMiddleware()],
        name='support_supervisor',
    )
