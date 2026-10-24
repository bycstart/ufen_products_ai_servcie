"""积分接口的数据约定；数值来自业务后端，不在 AI 服务中记账。"""
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StrictBool, StrictInt, model_validator

from atguigu.agent.harness.knowledge.output import KnowledgeItem


PointAmount = Annotated[int, Field(strict=True, ge=0)]


class PointsBalanceData(BaseModel):
    """一个积分账户的实时余额。"""

    account_id: str = Field(min_length=1)
    available_points: PointAmount
    frozen_points: PointAmount
    expiring_points: PointAmount
    as_of: AwareDatetime


class PointsTransactionData(BaseModel):
    """单条积分变动，扣减使用负数，返还使用正数。"""

    transaction_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    event_type: Literal["grant", "consume", "refund", "expire", "freeze", "unfreeze"]
    change_points: StrictInt
    occurred_at: AwareDatetime
    order_id: str | None = None


class PointsTransactionsData(BaseModel):
    """分页流水，不把当前页当作全部历史。"""

    account_id: str = Field(min_length=1)
    items: list[PointsTransactionData]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class PointsExpiryData(BaseModel):
    """单批积分及其真实到期时间。"""

    batch_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    points: PointAmount
    expires_at: AwareDatetime


class PointsAccountData(PointsBalanceData):
    """未请求批次时为 None，请求后的空页为 []，不能混为没有到期积分。"""

    expiry_included: StrictBool
    expiry_batches: list[PointsExpiryData] | None
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int | None = Field(ge=0)

    @model_validator(mode='after')
    def check_expiry(self):
        if self.expiring_points > self.available_points:
            raise ValueError('即将到期积分不能超过可用积分')
        if not self.expiry_included:
            if self.expiry_batches is not None or self.total is not None:
                raise ValueError('未加载批次时不应返回批次或总数')
        else:
            if self.expiry_batches is None or self.total is None:
                raise ValueError('已加载批次必须返回当前页及总数')
            if len(self.expiry_batches) > min(self.page_size, self.total):
                raise ValueError('到期批次数量与分页信息不符')
            if len({row.batch_id for row in self.expiry_batches}) != len(self.expiry_batches):
                raise ValueError('到期批次不能重复')
            if any(row.account_id != self.account_id for row in self.expiry_batches):
                raise ValueError('到期批次不属于当前账户')
        return self


class PointsEligibilityData(BaseModel):
    """后端计算的兑换预校验结果，不代表已经兑换。"""

    account_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    quantity: int = Field(strict=True, gt=0, le=99)
    eligible: StrictBool
    reason_code: str = Field(min_length=1)
    reason_message: str = Field(min_length=1, max_length=300)
    required_points: PointAmount
    available_points: PointAmount
    checked_at: AwareDatetime


class PointsDeductionData(BaseModel):
    """订单在一个积分账户上的扣减及返还汇总。"""

    order_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    deducted_points: PointAmount
    frozen_points: PointAmount
    returned_points: PointAmount
    as_of: AwareDatetime


class PointsRefundData(BaseModel):
    """积分返还独立于订单取消及售后状态。"""

    order_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    status: Literal["not_required", "pending", "processing", "completed", "failed"]
    expected_points: PointAmount
    returned_points: PointAmount
    updated_at: AwareDatetime


class OrderPointsData(PointsRefundData):
    """订单实际扣分与返还汇总，不能从现金金额或订单状态推算。"""

    deducted_points: PointAmount
    frozen_points: PointAmount
    as_of: AwareDatetime

    @model_validator(mode='after')
    def check_refund(self):
        if max(self.returned_points, self.expected_points) > self.deducted_points:
            raise ValueError('已返还或应返还积分不能超过实际扣分')
        if self.status == 'completed' and self.returned_points < self.expected_points:
            raise ValueError('返还完成状态与实际返还数量矛盾')
        return self


class PointsRuleData(KnowledgeItem):
    """由当前用户身份范围内查询到的有效积分规则。"""

    version: str = Field(min_length=1)
    effective_at: AwareDatetime
