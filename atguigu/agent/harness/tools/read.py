from typing import Annotated, Literal
from urllib.parse import quote

from langchain.tools import ToolRuntime, tool
from pydantic import Field, TypeAdapter

from atguigu.agent.harness.knowledge.output import KnowledgeItem
from atguigu.agent.harness.knowledge.service import KnowledgeQueryService
from atguigu.agent.harness.run.runtime import AgentRuntimeContext
from atguigu.agent.harness.tools.executor import ToolExecutor
from atguigu.agent.harness.tools.output import (
    AfterSaleData,
    LogisticsData,
    OrderData,
    ProductData,
    ProductStockData, ToolResult
)
from atguigu.infrastructure.client import EcommerceClient
from atguigu.agent.harness.tools.points_output import PointsRuleData


def _normalize_resource_id(resource_id: str) -> str:
    """统一订单和商品编号的格式。"""
    # 1. 清理空格、统一大小写并规范连接符
    return resource_id.strip().upper().replace("-", "_")


@tool
async def search_products(
        query: Annotated[str, "商品名称、类别、规格或用户描述的购买需求"],
        runtime: ToolRuntime[AgentRuntimeContext],
        max_points: Annotated[int | None, Field(strict=True, ge=0), "可选的单件商品积分预算"] = None,
        account_id: Annotated[str | None, "用户明确选择的积分账户，用于确定商城积分价"] = None
) -> str:
    """
    根据用户需求搜索当前在售商品。
    用户询问某类商品、商品名称或购买建议，但没有明确商品编号时调用。
    返回匹配商品的名称、类别、价格、状态、描述和规格。
    """
    # 1. 确认 LangChain 为本次调用生成的工具调用ID
    if runtime.tool_call_id is None:
        raise RuntimeError("商品搜索工具缺少调用编号")

    # 2. 整理搜索内容并创建当前用户的电商服务客户端
    ecommerce_client = EcommerceClient(runtime.context.access_token)

    # 3. 定义本次工具需要执行的电商服务请求
    async def request_products() -> object:
        parameters = {"query": query}
        if max_points is not None:
            parameters["max_points"] = max_points
        if account_id is not None:
            parameters["account_id"] = account_id
        response = await ecommerce_client.get_json(
            "/products",
            query_params=parameters
        )
        # 后端可能尚不支持积分筛选；在已校验的数据上复核，不用现金价格猜积分。
        if max_points is not None and response.get("success"):
            products = TypeAdapter(list[ProductData]).validate_python(response["data"])
            if any(product.points_price is None for product in products):
                return ToolResult(success=False, code="POINTS_PRICE_UNAVAILABLE",
                                  message="商品接口尚未提供完整积分价格，暂时无法按积分预算筛选").model_dump()
            response = {**response, "data": [product.model_dump(mode="json") for product in products
                                             if product.points_price <= max_points]}
        return response

    # 4. 交给工具执行器记录、执行并校验结果
    return await tool_executor.execute(
        runtime_context=runtime.context,
        tool_call_id=runtime.tool_call_id,
        tool_name="search_products",
        arguments={"query": query, **({"max_points": max_points} if max_points is not None else {}),
                   **({"account_id": account_id} if account_id is not None else {})},
        operation=request_products,
        output_schema=list[ProductData]
    )


@tool
async def get_product(
        product_id: Annotated[str, "用户明确提供或商品搜索结果返回的商品编号，例如 PRODUCT_001"],
        runtime: ToolRuntime[AgentRuntimeContext],
        account_id: Annotated[str | None, "用户明确选择的积分账户"] = None
) -> str:
    """
    查询一个商品的名称、价格、描述和规格。
    用户已经明确商品编号，并询问该商品的基础信息时调用。
    实时库存必须使用 get_product_stock 查询。
    """
    # 1. 确认 LangChain 为本次调用生成的工具调用ID
    if runtime.tool_call_id is None:
        raise RuntimeError("商品详情工具缺少调用编号")

    # 2. 规范商品编号并创建当前用户的电商服务客户端
    normalized_product_id = _normalize_resource_id(product_id)
    ecommerce_client = EcommerceClient(runtime.context.access_token)

    # 3. 定义本次工具需要执行的商品详情请求
    async def request_product() -> object:
        # a. 查询商品详情
        path = f"/products/{quote(normalized_product_id, safe='')}"
        if account_id is not None:
            return await ecommerce_client.get_json(path, query_params={"account_id": account_id})
        return await ecommerce_client.get_json(path)

    # 4. 交给工具执行器记录、执行并校验结果
    return await tool_executor.execute(
        runtime_context=runtime.context,
        tool_call_id=runtime.tool_call_id,
        tool_name="get_product",
        arguments={"product_id": normalized_product_id,
                   **({"account_id": account_id} if account_id is not None else {})},
        operation=request_product,
        output_schema=ProductData
    )


@tool
async def get_product_stock(
        product_id: Annotated[str, "用户明确提供或商品搜索结果返回的商品编号，例如 PRODUCT_001"],
        runtime: ToolRuntime[AgentRuntimeContext]
) -> str:
    """
    查询一个商品的实时库存和销售状态。
    用户询问具体商品是否有货或剩余库存时调用，不用于查询其他商品详情。
    """
    # 1. 确认 LangChain 为本次调用生成的工具调用ID
    if runtime.tool_call_id is None:
        raise RuntimeError("库存查询工具缺少调用编号")

    # 2. 规范商品编号并创建当前用户的电商服务客户端
    normalized_product_id = _normalize_resource_id(product_id)
    ecommerce_client = EcommerceClient(
        runtime.context.access_token
    )

    # 3. 定义本次工具需要执行的库存查询请求
    async def request_product_stock() -> object:
        # a. 查询实时库存
        return await ecommerce_client.get_json(
            f"/products/{quote(normalized_product_id, safe='')}/stock"
        )

    # 4. 交给工具执行器记录、执行并校验结果
    return await tool_executor.execute(
        runtime_context=runtime.context,
        tool_call_id=runtime.tool_call_id,
        tool_name="get_product_stock",
        arguments={"product_id": normalized_product_id},
        operation=request_product_stock,
        output_schema=ProductStockData
    )


@tool
async def list_orders(
        runtime: ToolRuntime[AgentRuntimeContext]
) -> str:
    """
    查询当前用户的订单列表。
    用户询问自己的订单但没有明确订单编号时调用。
    返回订单编号、状态、支付类型、兑换积分、创建时间和商品信息；积分订单按 total_points 回答，不执行订单操作。
    """
    # 1. 确认 LangChain 为本次调用生成的工具调用ID
    if runtime.tool_call_id is None:
        raise RuntimeError("订单列表工具缺少调用编号")

    # 2. 创建当前用户的电商服务客户端
    ecommerce_client = EcommerceClient(
        runtime.context.access_token
    )

    # 3. 定义本次工具需要执行的订单列表请求
    async def request_orders() -> object:
        # a. 查询当前用户的订单列表
        return await ecommerce_client.get_json("/orders")

    # 4. 工具执行器记录、执行并校验结果
    return await tool_executor.execute(
        runtime_context=runtime.context,
        tool_call_id=runtime.tool_call_id,
        tool_name="list_orders",
        arguments={},
        operation=request_orders,
        output_schema=list[OrderData]
    )


@tool
async def get_order(
        order_id: Annotated[str, "用户明确提供或订单列表返回的订单编号，例如 ORDER_001"],
        runtime: ToolRuntime[AgentRuntimeContext]
) -> str:
    """
    查询当前用户的一条订单详情。
    用户已经明确订单编号，并询问该订单的状态、兑换积分、创建时间或商品信息时调用。
    只读取数据，不取消订单、退款或修改地址。
    """
    # 1. 确认 LangChain 为本次调用生成的工具调用ID
    if runtime.tool_call_id is None:
        raise RuntimeError("订单详情工具缺少调用编号")

    # 2. 规范订单编号并创建当前用户的电商服务客户端
    normalized_order_id = _normalize_resource_id(order_id)
    ecommerce_client = EcommerceClient(
        runtime.context.access_token
    )

    # 3. 定义本次工具需要执行的订单详情请求
    async def request_order() -> object:
        # a. 查询订单详情
        return await ecommerce_client.get_json(
            f"/orders/{quote(normalized_order_id, safe='')}"
        )

    # 4. 执行器记录、执行并校验结果
    return await tool_executor.execute(
        runtime_context=runtime.context,
        tool_call_id=runtime.tool_call_id,
        tool_name="get_order",
        arguments={"order_id": normalized_order_id},
        operation=request_order,
        output_schema=OrderData
    )


@tool
async def get_logistics(
        order_id: Annotated[str, "用户明确提供或订单列表返回的订单编号，例如 ORDER_001"],
        runtime: ToolRuntime[AgentRuntimeContext]
) -> str:
    """
    查询一个订单的物流公司、运单号、状态和物流轨迹。
    用户已经明确订单编号，并询问该订单的配送进度或物流信息时调用。
    订单基础信息应使用 get_order 查询。
    """
    # 1. 确认 LangChain 为本次调用生成的工具调用ID
    if runtime.tool_call_id is None:
        raise RuntimeError("物流查询工具缺少调用编号")

    # 2. 规范订单编号并创建当前用户的电商服务客户端
    normalized_order_id = _normalize_resource_id(order_id)
    ecommerce_client = EcommerceClient(
        runtime.context.access_token
    )

    # 3. 定义本次工具需要执行的物流查询请求
    async def request_logistics() -> object:
        # a. 查询物流信息
        return await ecommerce_client.get_json(
            f"/orders/{quote(normalized_order_id, safe='')}/logistics"
        )

    # 4. 工具执行器记录、执行并校验结果
    return await tool_executor.execute(
        runtime_context=runtime.context,
        tool_call_id=runtime.tool_call_id,
        tool_name="get_logistics",
        arguments={"order_id": normalized_order_id},
        operation=request_logistics,
        output_schema=LogisticsData
    )


@tool
async def list_after_sales(
        runtime: ToolRuntime[AgentRuntimeContext],
        order_id: Annotated[str | None, "可选的订单编号；提供后只查询该订单的售后记录"] = None
) -> str:
    """
    查询当前用户的售后申请记录。
    用户询问退款、退货或换货申请的处理进度时调用。
    已经明确订单编号时按订单查询，否则返回当前客户的全部售后记录。
    """
    # 1. 确认 LangChain 为本次调用生成的工具调用ID
    if runtime.tool_call_id is None:
        raise RuntimeError("售后查询工具缺少调用编号")

    # 2. 规范可选订单编号并构建查询参数
    normalized_order_id = (
        _normalize_resource_id(order_id)
        if order_id is not None
        else None
    )
    query_params = (
        {"order_id": normalized_order_id}
        if normalized_order_id is not None
        else None
    )
    # 3. 创建当前用户的电商服务客户端
    ecommerce_client = EcommerceClient(
        runtime.context.access_token
    )

    # 4. 定义本次工具需要执行的售后记录请求
    async def request_after_sales() -> object:
        # a. 按可选订单编号查询当前用户的售后记录
        return await ecommerce_client.get_json(
            "/after-sales",
            query_params=query_params
        )

    # 5. 工具执行器记录、执行并校验结果
    return await tool_executor.execute(
        runtime_context=runtime.context,
        tool_call_id=runtime.tool_call_id,
        tool_name="list_after_sales",
        arguments=query_params or {},
        operation=request_after_sales,
        output_schema=list[AfterSaleData]
    )


@tool
async def search_knowledge(
        query: Annotated[
            str,
            "需要查询的平台规则、服务政策或帮助说明"
        ],
        runtime: ToolRuntime[AgentRuntimeContext],
        topic: Literal["platform", "points"] = "platform",
        account_id: Annotated[str | None, "积分规则所属账户；不用于平台通用政策"] = None
) -> str:
    """查询平台知识内容。

    用户询问售后规则、物流说明、发票规则或平台帮助说明时调用。
    询问积分使用、到期、转赠或返还规则时必须指定 topic=points。
    该工具不用于查询订单、商品、库存、物流进度等实时业务数据。
    """
    # 1. 确认 LangChain 已经为本次调用分配调用编号
    if runtime.tool_call_id is None:
        raise RuntimeError("知识查询工具缺少调用编号")

    # 2. 清理模型生成的查询内容
    normalized_query = query.strip()

    # 3. 定义本次工具需要执行的知识查询
    async def request_knowledge() -> object:
        if topic == "points":
            # 积分规则必须来自当前用户有权访问的商城，不拼接虚构规则。
            return await EcommerceClient(runtime.context.access_token).get_json(
                "/points/rules", query_params={"query": normalized_query,
                    **({"account_id": account_id} if account_id is not None else {})}
            )
        knowledge_items = await knowledge_query_service.search(
            normalized_query
        )
        return ToolResult[list[KnowledgeItem]](
            success=True,
            code="KNOWLEDGE_SEARCH_COMPLETED",
            message=(
                "已找到相关知识内容"
                if knowledge_items
                else "未找到相关知识内容"
            ),
            data=knowledge_items
        ).model_dump(mode="python")

    # 4. 交给统一执行器记录、执行并校验结果
    return await tool_executor.execute(
        runtime_context=runtime.context,
        tool_call_id=runtime.tool_call_id,
        tool_name="search_knowledge",
        arguments={"query": normalized_query, "topic": topic,
                   **({"account_id": account_id} if account_id is not None else {})},
        operation=request_knowledge,
        output_schema=list[PointsRuleData] if topic == "points" else list[KnowledgeItem]
    )


tool_executor = ToolExecutor()
knowledge_query_service = KnowledgeQueryService()
