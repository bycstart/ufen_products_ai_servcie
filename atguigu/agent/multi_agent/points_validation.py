"""积分结论按字段生成，防止模型混用余额、冻结数、现金价格或有效期。"""
from datetime import date
from zoneinfo import ZoneInfo

from atguigu.agent.harness.tools.points_output import (
    PointsAccountData, PointsTransactionsData, PointsEligibilityData, OrderPointsData,
)
from atguigu.agent.harness.tools.snapshot import ToolCallSnapshot


def _check_identity(record, arguments: dict, *fields: str) -> None:
    for name in fields:
        expected = arguments.get(name)
        if expected is not None and getattr(record, name) != expected:
            raise ValueError("积分接口返回的对象或查询条件不匹配")


def _time(value) -> str:
    return value.isoformat(sep=" ", timespec="seconds")


def render_points_finding(call: ToolCallSnapshot, resource_id: str | None) -> str:
    """先核对证据对象，再使用固定文案展示后端真实字段。"""
    data = call.result["data"]
    arguments = call.arguments
    if call.tool_name == "get_points_account":
        record = PointsAccountData.model_validate(data)
        _check_identity(record, arguments, "account_id", "page", "page_size")
        if record.expiry_included != arguments.get('include_expiry', False):
            raise ValueError('到期批次加载状态与请求不符')
        if resource_id == record.account_id:
            text = (f"账户{record.account_id}：可用积分{record.available_points}，冻结积分{record.frozen_points}，"
                    f"即将到期积分{record.expiring_points}；统计时间{_time(record.as_of)}。")
            if record.expiry_included:
                text += (f'到期批次第{record.page}页返回{len(record.expiry_batches)}条，共{record.total}条；'
                         '当前页不代表全部批次，未查到批次也不代表积分永久有效。')
            return text
        matches = [row for row in record.expiry_batches or () if row.batch_id == resource_id]
        if not record.expiry_included or len(matches) != 1:
            raise ValueError('未加载或未唯一匹配当前账户的到期批次')
        batch = matches[0]
        return (f'账户{record.account_id}批次{batch.batch_id}：{batch.points}积分将于{_time(batch.expires_at)}到期。'
                f'这里只展示第{record.page}页所选批次，共{record.total}条，不代表全部到期积分。')

    if call.tool_name == "get_points_transactions":
        page = PointsTransactionsData.model_validate(data)
        _check_identity(page, arguments, "account_id", "page", "page_size")
        records = [item for item in page.items if item.transaction_id == resource_id]
        if len(records) != 1 or records[0].account_id != page.account_id:
            raise ValueError("积分流水未唯一对应当前账户记录")
        record = records[0]
        _check_identity(record, arguments, "event_type")
        # 接口约定按上海业务日期筛选，不能直接使用 UTC 日期比较。
        business_date = record.occurred_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if ((arguments.get("date_from") and business_date < date.fromisoformat(arguments["date_from"]))
                or (arguments.get("date_to") and business_date > date.fromisoformat(arguments["date_to"]))):
            raise ValueError("积分流水不在请求的业务日期范围内")
        event = {"grant": "发放", "consume": "消费", "refund": "退回", "expire": "过期",
                 "freeze": "冻结", "unfreeze": "解冻"}[record.event_type]
        order = f"，关联订单{record.order_id}" if record.order_id else ""
        return (f"账户{record.account_id}流水{record.transaction_id}：{event}变动{record.change_points:+d}积分{order}，"
                f"时间{_time(record.occurred_at)}；本次展示第{page.page}页所选记录，共{page.total}条流水。")

    if call.tool_name == "check_points_eligibility":
        record = PointsEligibilityData.model_validate(data)
        _check_identity(record, arguments, "account_id", "product_id", "quantity")
        if resource_id != record.product_id:
            raise ValueError("兑换预校验的商品不匹配")
        if record.eligible and record.available_points < record.required_points:
            raise ValueError("兑换预校验结论与积分余额矛盾")
        result = "预校验通过" if record.eligible else "预校验未通过"
        return (f"商品{record.product_id}兑换{record.quantity}件：{result}，{record.reason_message}；"
                f"账户{record.account_id}所需积分{record.required_points}，可用积分{record.available_points}。"
                f"校验时间{_time(record.checked_at)}；这不是兑换成功，提交时仍需后端重新校验。")

    if call.tool_name == "get_order_points":
        record = OrderPointsData.model_validate(data)
        _check_identity(record, arguments, "order_id")
        if resource_id != record.order_id:
            raise ValueError("积分返还的订单不匹配")
        status = {"not_required": "无需返还", "pending": "待返还", "processing": "返还处理中",
                  "completed": "返还已完成", "failed": "返还失败"}[record.status]
        return (f"订单{record.order_id}、账户{record.account_id}：扣减积分{record.deducted_points}，"
                f"冻结积分{record.frozen_points}；{status}，应返还积分{record.expected_points}，"
                f"已返还积分{record.returned_points}；统计时间{_time(record.as_of)}，更新时间{_time(record.updated_at)}。")
    raise ValueError("不支持的积分证据类型")


def render_empty_points(call: ToolCallSnapshot) -> str | None:
    """空页有明确含义，不能因为分页对象非空而当成校验失败。"""
    if call.tool_name == "get_points_transactions":
        page = PointsTransactionsData.model_validate(call.result["data"])
        _check_identity(page, call.arguments, "account_id", "page", "page_size")
        if not page.items:
            return f"账户{page.account_id}第{page.page}页没有流水，本次查询共{page.total}条，请按需查询其他页。"
    return None
