"""积分领域的四个只读工具，统一复用现有客户端与工具执行器。"""
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import quote

from langchain.tools import ToolRuntime, tool
from pydantic import Field

from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.harness.tools.executor import ToolExecutor
from atguigu.agent.harness.tools.points_output import (
    PointsAccountData, PointsTransactionsData, PointsEligibilityData, OrderPointsData,
)
from atguigu.infrastructure.client import EcommerceClient

tool_executor = ToolExecutor()


async def _query_points(runtime: ToolRuntime[AgentRuntimeContext], tool_name: str,
                        path: str, arguments: dict[str, Any], output_schema: Any) -> str:
    """身份来自运行上下文；不允许模型传入用户或企业身份。"""
    if runtime.tool_call_id is None:
        raise RuntimeError("积分查询工具缺少调用编号")
    parameters = {key: value.isoformat() if isinstance(value, date) else value
                  for key, value in arguments.items() if value is not None}
    client = EcommerceClient(runtime.context.access_token)

    async def request_points() -> object:
        return await client.get_json(path, query_params=parameters)

    return await tool_executor.execute(
        runtime_context=runtime.context,
        tool_call_id=runtime.tool_call_id,
        tool_name=tool_name,
        arguments=parameters,
        operation=request_points,
        output_schema=output_schema,
    )


@tool
async def get_points_account(
        runtime: ToolRuntime[AgentRuntimeContext],
        account_id: Annotated[str | None, "用户明确选择的积分账户；为空时由后端确定默认账户"] = None,
        include_expiry: Annotated[bool, Field(strict=True), "用户需要到期时间或批次时设为 true"] = False,
        page: Annotated[int, Field(ge=1)] = 1,
        page_size: Annotated[int, Field(ge=1, le=100)] = 20
) -> str:
    """查询可用、冻结和即将到期积分；include_expiry=true 时分页返回到期批次。"""
    return await _query_points(runtime, "get_points_account", "/points/account", {
        "account_id": account_id, "include_expiry": include_expiry, "page": page, "page_size": page_size,
    }, PointsAccountData)


@tool
async def get_points_transactions(
        runtime: ToolRuntime[AgentRuntimeContext],
        account_id: str | None = None,
        event_type: Literal["grant", "consume", "refund", "expire", "freeze", "unfreeze"] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        page: Annotated[int, Field(ge=1)] = 1,
        page_size: Annotated[int, Field(ge=1, le=100)] = 20
) -> str:
    """分页查询积分发放、消费、退回、过期或冻结流水。时间范围为商城业务日期。"""
    if date_from and date_to and date_from > date_to:
        raise ValueError("开始日期不能晚于结束日期")
    return await _query_points(runtime, "get_points_transactions", "/points/transactions", {
        "account_id": account_id, "event_type": event_type,
        "date_from": date_from, "date_to": date_to, "page": page, "page_size": page_size,
    }, PointsTransactionsData)


@tool
async def check_points_eligibility(
        product_id: Annotated[str, Field(min_length=1), "需要兑换的商品编号"],
        runtime: ToolRuntime[AgentRuntimeContext],
        quantity: Annotated[int, Field(strict=True, gt=0, le=99)] = 1,
        account_id: str | None = None
) -> str:
    """由后端预校验余额、账户、库存和兑换限制；通过也不代表下单成功。"""
    return await _query_points(runtime, "check_points_eligibility", "/points/eligibility", {
        "product_id": product_id.strip().upper().replace("-", "_"),
        "quantity": quantity, "account_id": account_id,
    }, PointsEligibilityData)


@tool
async def get_order_points(
        order_id: Annotated[str, Field(min_length=1), "已明确的订单编号"],
        runtime: ToolRuntime[AgentRuntimeContext]
) -> str:
    """一次查询订单扣分、冻结、应返还、已返还及返还状态，不执行退款或把金额换算成积分。"""
    order_id = order_id.strip().upper().replace("-", "_")
    return await _query_points(runtime, "get_order_points", f"/orders/{quote(order_id, safe='')}/points",
                               {"order_id": order_id}, OrderPointsData)
