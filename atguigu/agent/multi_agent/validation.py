from dataclasses import replace

from atguigu.agent.harness.rules.fact import FactChecker, FactExtractor, FactCategory
from atguigu.agent.harness.tools.output import ToolFailureType
from atguigu.agent.harness.tools.snapshot import ToolCallSnapshot
from atguigu.agent.harness.validator.action import PageActionValidator
from atguigu.agent.llm.output import AgentOutput, ReplyType
from atguigu.agent.multi_agent.definition import DomainAgentOutput, DomainResult, DomainStatus
from atguigu.agent.multi_agent.registry import DOMAIN_SPECS
from atguigu.agent.multi_agent.points_validation import render_points_finding, render_empty_points


def _normalize(value: str) -> str:
    return value.strip().upper().replace('-', '_')


def _resource_data(data: object, resource_id: str) -> object:
    """只提取目标对象的记录，防止一笔订单借用另一笔订单的状态或金额。"""
    records = data if isinstance(data, list) else [data]
    # 售后记录优先按自身 id 匹配，不能把同订单的多条售后记录混成一个对象。
    matches = [record for record in records if isinstance(record, dict)
               and _normalize(str(record.get('id') or record.get('order_id') or record.get('product_id') or ''))
               == _normalize(resource_id)]
    if len(matches) != 1:
        raise ValueError('证据必须唯一对应一个业务对象')
    return matches


def validate_domain_output(output: DomainAgentOutput, calls: tuple[ToolCallSnapshot, ...],
                           agent_code: str, task_id: str) -> DomainResult:
    spec = DOMAIN_SPECS[agent_code]
    if any(call.tool_name not in spec.tools for call in calls):
        raise ValueError('子任务包含超出领域范围的工具')
    successful = {c.tool_call_id: c for c in calls if c.success is True}
    failed = [c for c in calls if c.success is not True]
    lines, refs = [], []
    for finding in output.findings:
        call = successful.get(finding.evidence_id)
        if call is None:
            raise ValueError('结论引用了本任务以外或失败的工具记录')
        data = call.result['data']
        if agent_code == 'points':
            # 积分由字段模板输出，模型只负责选择证据，不自行改写数值和状态。
            lines.append(render_points_finding(call, finding.resource_id))
            refs.append(call.tool_call_id)
            continue
        if agent_code == 'policy':
            if not isinstance(data, list) or not any(
                finding.text in item.get('content', '') for item in data if isinstance(item, dict)
            ):
                raise ValueError('政策结论必须引用返回知识中的原文片段')
        else:
            if not finding.resource_id:
                raise ValueError('业务结论必须指定对应对象')
            scoped_data = _resource_data(data, finding.resource_id)
            scoped = replace(call, arguments={}, result={'data': scoped_data})
            if FactChecker().get_unsupported_facts(finding.text, (scoped,)):
                raise ValueError('业务结论与目标对象证据不一致')
        lines.append(finding.text)
        refs.append(call.tool_call_id)

    status = output.status
    if status == DomainStatus.NEEDS_CLARIFICATION:
        # 澄清只询问缺失信息。状态、金额等事实应放入按对象校验的 findings。
        if any(fact.category != FactCategory.IDENTIFIER for fact in FactExtractor.extract(output.clarification or '')):
            raise ValueError('请将业务事实放入有证据的结论，澄清只保留问题')
        business_calls = tuple(c for c in successful.values() if c.tool_name != 'search_knowledge')
        if FactChecker().get_unsupported_facts(output.clarification or '', business_calls):
            raise ValueError('澄清包含未经查询确认的业务信息')
        lines.append(output.clarification)
    elif not lines and successful:
        empty_messages = [render_empty_points(c) for c in successful.values()] if agent_code == 'points' else []
        if empty_messages and all(empty_messages):
            lines.extend(empty_messages)
            refs.extend(successful)
        elif any(c.result.get('data') for c in successful.values()):
            raise ValueError('非空查询结果必须给出有证据的结论或澄清')
        else:
            lines.append('没有查询到相关记录。')
        status = DomainStatus.SUCCESS
    elif not successful and not failed:
        raise ValueError('未查询工具不能声称完成领域任务')

    for call in failed:
        if call.failure_type == ToolFailureType.BUSINESS:
            message = str((call.result or {}).get('message') or '业务查询未成功')[:200]
        else:
            message = '暂时无法取得可靠结果，请稍后重试。'
        lines.append(f'{call.tool_name}：{message}')
    if not successful and failed:
        status = (DomainStatus.BUSINESS_FAILED if all(c.failure_type == ToolFailureType.BUSINESS for c in failed)
                  else DomainStatus.SERVICE_FAILED)
    elif lines and output.status not in (DomainStatus.SUCCESS, DomainStatus.NEEDS_CLARIFICATION):
        status = DomainStatus.SUCCESS if refs else output.status

    action = output.page_action_request if status == DomainStatus.SUCCESS else None
    if action:
        PageActionValidator().validate(action, calls)
    return DomainResult(task_id=task_id, agent_code=agent_code, status=status,
                        summary='\n'.join(dict.fromkeys(lines)), evidence_refs=list(dict.fromkeys(refs)),
                        failed_tools=list(dict.fromkeys(call.tool_name for call in failed)),
                        page_action_request=action)


def compose_results(results: list[DomainResult], notice: str | None = None) -> AgentOutput:
    """按已校验的领域片段汇总，保留失败，避免主控改写导致对象和状态错配。"""
    lines = [f'【{DOMAIN_SPECS[r.agent_code].label}】{r.summary}' for r in results]
    if notice:
        lines.append(notice)
    actions = {r.page_action_request.model_dump_json(): r.page_action_request for r in results
               if r.page_action_request is not None and r.status == DomainStatus.SUCCESS}
    needs_clarification = any(r.status == DomainStatus.NEEDS_CLARIFICATION for r in results)
    if len(actions) > 1:
        needs_clarification = True
        lines.append('涉及多个页面操作，请明确您希望先处理的订单和操作。')
    reply_type = (ReplyType.CLARIFY if needs_clarification else
                  ReplyType.ANSWER if any(r.status == DomainStatus.SUCCESS for r in results) else ReplyType.DECLINE)
    # 超过协议长度时按任务平分空间，保留每个任务及其状态。
    text = '\n\n'.join(lines)
    if len(text) > 4000:
        tail = '结果较多，以上为摘要，请缩小查询范围查看详细结果。'
        if needs_clarification:
            tail += '\n请明确需要优先处理的对象和操作。'
        if notice:
            tail += '\n' + notice[:300]
        headers = [f'【{DOMAIN_SPECS[r.agent_code].label} / {r.status}】'
                   + (f'未完成：{", ".join(r.failed_tools)}。' if r.failed_tools else '') for r in results]
        limit = max(1, (3900 - len(tail) - sum(map(len, headers))) // max(1, len(results)))
        text = '\n\n'.join(f'{header}{r.summary[:limit]}…' for header, r in zip(headers, results)) + '\n\n' + tail
    return AgentOutput(reply_type=reply_type, reply_content=text or '暂时无法完成查询。',
                       page_action_request=next(iter(actions.values()))
                       if len(actions) == 1 and reply_type == ReplyType.ANSWER else None)
