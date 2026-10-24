from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from atguigu.models.models import AgentRun, AgentToolCall


def _redact(value):
    """审计信息递归隐藏认证字段，Token 计量字段保留。"""
    if isinstance(value, dict):
        return {key: ('[已隐藏]' if str(key).lower() in {
            'authorization', 'password', 'api_key', 'apikey', 'access_token',
            'refresh_token', 'jwt', 'secret', 'cookie', 'token',
        } else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _run_summary(run) -> dict:
    question = None
    context = run.input_context or {}
    for message in reversed(context.get('messages', [])):
        # 当前请求的 CurrentMessage 不含 role，历史消息才显式携带角色。
        if not isinstance(message, dict) or message.get('role', 'user') != 'user':
            continue
        content = message.get('content')
        text = content.get('text') if isinstance(content, dict) else content
        if isinstance(text, str) and text.strip():
            question = ' '.join(text.split())[:160]
            break
    return {
        'id': run.id, 'conversation_id': run.conversation_id,
        'user_question': question, 'state': run.state, 'model_name': run.model_name,
        'latency_ms': run.latency_ms, 'input_tokens': run.input_tokens,
        'output_tokens': run.output_tokens,
        'total_tokens': (run.input_tokens + run.output_tokens
                         if run.input_tokens is not None and run.output_tokens is not None else None),
        'started_at': run.started_at.isoformat() if run.started_at else None,
        'finished_at': run.finished_at.isoformat() if run.finished_at else None,
    }


class AdminService:
    """提供管理员观测 AI Run 的查询能力。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_metrics(self) -> dict:
        """返回 AI Run 汇总指标。"""
        agent_runs = await self.session.scalar(
            select(func.count()).select_from(AgentRun)
        )
        average_latency_ms = await self.session.scalar(
            select(func.avg(AgentRun.latency_ms))
        )
        input_tokens = await self.session.scalar(
            select(func.sum(AgentRun.input_tokens))
        )
        output_tokens = await self.session.scalar(
            select(func.sum(AgentRun.output_tokens))
        )
        return {
            "agent_runs": agent_runs,
            "average_latency_ms": average_latency_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens
        }

    async def list_runs(self, limit: int) -> list[dict]:
        """返回最近的 AI Run。"""
        runs = (
            await self.session.scalars(
                select(AgentRun)
                .order_by(AgentRun.started_at.desc())
                .limit(limit)
            )
        ).all()
        return [_run_summary(run) for run in runs]

    async def get_run(self, run_id: str) -> dict:
        """返回一个 AI Run 及其工具调用记录。"""
        run = await self.session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run 不存在")
        tool_calls = (
            await self.session.scalars(
                select(AgentToolCall)
                .where(AgentToolCall.run_id == run_id)
                .order_by(AgentToolCall.created_at)
            )
        ).all()
        return {
            "run": {
                **_run_summary(run),
                "prompt_version": run.prompt_version,
                "outcome": _redact(run.result),
            },
            "tool_calls": [
                {
                    "tool_call_id": call.tool_call_id,
                    "tool_name": call.tool_name,
                    "arguments": _redact(call.arguments),
                    "latency_ms": call.latency_ms,
                    "success": call.success,
                    "code": (call.result or {}).get("code"),
                }
                for call in tool_calls
            ]
        }
