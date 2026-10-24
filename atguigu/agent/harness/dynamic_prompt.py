from atguigu.agent.harness.rules.action import ACTION_CATALOG
from atguigu.agent.harness.skills.catalog import SKILL_CATALOG
from atguigu.agent.llm.prompt import BASE_PROMPT


def render_domain_prompt(agent_code: str) -> str:
    """专家领域固定，复用原 Skill 的说明而不再动态加载 Skill。"""
    from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
    spec = DOMAIN_SPECS[agent_code]
    skill = SKILL_CATALOG.get_skill(spec.skill_code)
    return '\n'.join([
        BASE_PROMPT,
        f'你是{spec.label}领域专家，只完成主控交给你的任务。',
        *skill.guidance,
        '你输出 DomainAgentOutput，而不是面向用户的 AgentOutput。',
        '每条 findings 必须引用对应工具返回的 evidence_id；业务结论必须指定 resource_id。',
        '积分专家的最终正文由服务端根据证据字段生成，不得用金额、冻结积分或其他批次代替当前可用积分。',
        '每条结论只描述该证据内的一个业务对象，不混用不同订单、商品的金额和状态。',
        '售后结论的 resource_id 使用售后记录自身 id，不使用订单号代替售后单号。',
        '政策 findings.text 必须是知识 content 中的原文片段，不能自行补充规则。',
        '需要用户选择订单或补充参数时返回 NEEDS_CLARIFICATION 和 clarification，不猜测目标。',
        'clarification 只写需要用户回答的问题；候选对象的状态和金额放入 findings。',
        '查询成功用 SUCCESS；失败用 BUSINESS_FAILED 或 SERVICE_FAILED。',
        '工具成功但 data 为空时，可以返回 SUCCESS 和空 findings。',
        '同一工具同一参数失败后不要重复查询。最多输出四条简洁结论。',
        f'允许的页面动作：\n{ACTION_CATALOG.render_action_index()}',
    ])


def render_supervisor_prompt() -> str:
    from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
    return '\n'.join([
        BASE_PROMPT,
        '你是客服主控，业务事实必须委派给领域专家查询。你没有直接查询工具。',
        '先理解用户目标；互不依赖的任务应在同一轮发出多个专家工具调用，并行委派。',
        '若后一个任务需要前一个任务的查询结果，必须先等待结果，再在下一轮委派；不可猜测依赖参数。',
        '传递清晰子任务，包括必要的用户原话、订单号、已确认的选择；不要传整段无关历史。',
        '用户目标明确但需要发现订单时，可委派专家查询；多笔候选不能擅自选择。',
        '跨领域问题要分别委派，保留各专家成功、澄清、失败的信息。',
        '积分预算选品先委派商品专家取得积分价格和商品编号，再委派积分专家做兑换预校验。',
        '订单取消与积分退回分开查询；积分使用和有效期规则交给政策专家，实时余额和流水交给积分专家。',
        '相同任务不要重复委派；普通寒暄、明确转人工和非业务问题可直接回复。',
        '所有任务完成或需要澄清后输出 AgentOutput，不要无限继续调用。',
        '最终业务内容由服务端按已验证的专家结果汇总，不要增加未经查询的结论。',
        *[f'consult_{code}: {spec.description}' for code, spec in DOMAIN_SPECS.items()],
    ])
