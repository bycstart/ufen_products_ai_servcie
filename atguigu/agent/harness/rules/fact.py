import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from atguigu.agent.harness.tools.snapshot import ToolCallSnapshot


class FactCategory(StrEnum):
    """定义需要工具数据支持的业务事实类别"""

    IDENTIFIER = "IDENTIFIER"
    NUMBER = "NUMBER"
    STATUS = "STATUS"
    POINTS = "POINTS"
    MONEY = "MONEY"


STATUS_CODE_MAP = {
    "已支付": "paid",
    "待发货": "pending_shipment",
    "已发货": "shipped",
    "已完成": "completed",
    "已取消": "cancelled",
    "已退款": "refunded",
    "运输中": "in_transit",
    "已签收": "delivered",
    "待处理": "pending",
    "已通过": "approved",
    "处理中": "processing",
    "已拒绝": "rejected",
}


@dataclass(frozen=True)
class BusinessFact:
    """保存从 Agent 回复中提取的一项业务事实"""

    category: FactCategory
    value: str


class FactExtractor:
    """从 Agent 回复中提取需要匹配的业务事实"""

    # 匹配以ORDER、PRODUCT 或 AS 的业务资源编号（如订单号、商品号、售后单号）
    resource_id_pattern = re.compile(
        r"\b(?:ORDER|PRODUCT|AS|ACCOUNT|TXN|TRANSACTION|BATCH)[_-][A-Z0-9_-]+\b",
        re.IGNORECASE
    )
    # 匹配金额、总价、价格、库存、数量 的数值
    labeled_number_pattern = re.compile(
        r"(?:金额|总价|价格|库存|数量)"
        r"(?:为|是|有|剩余|还剩|：|:)?\s*"
        r"[¥￥]?\s*(\d+(?:\.\d+)?)"
    )
    # 匹配带有货币符号或常用数量单位元、件、个、台、套的数值 （¥399 or 399元）
    unit_number_pattern = re.compile(
        r"(?:[¥￥]\s*(\d+(?:\.\d+)?)|"
        r"(\d+(?:\.\d+)?)\s*(?:元|件|个|台|套))"
    )
    status_labels = tuple(STATUS_CODE_MAP)
    points_pattern = re.compile(
        r"(?:可用积分|冻结积分|即将到期积分|扣减积分|已返还积分|应返还积分|所需积分|积分余额|积分价格|积分)"
        r"(?:为|是|有|：|:)?\s*([+-]?\d+(?:\.\d+)?)"
        r"|([+-]?\d+(?:\.\d+)?)\s*(?:个)?积分"
    )
    money_pattern = re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*元")
    money_label_pattern = re.compile(
        r"(?<!积分)(?:金额|总价|价格)(?:为|是|：|:)?\s*[¥￥]?\s*(\d+(?:\.\d+)?)"
    )

    @classmethod
    def extract(cls,reply_content: str) -> tuple[BusinessFact, ...]:
        """提取并去除重复的业务编号、数字和状态"""

        # 1. 创建按业务事实值去重的有序集合
        facts: dict[BusinessFact, None] = {}

        # 2. 提取并规范订单、商品和售后编号
        for resource_id in cls.resource_id_pattern.findall(reply_content):
            normalized_id = resource_id.upper().replace("-", "_")
            facts[BusinessFact(FactCategory.IDENTIFIER,normalized_id)] = None

        # 3. 提取带有金额、价格、库存或数量标签的数字
        for number in cls.labeled_number_pattern.findall(reply_content):
            facts[BusinessFact(FactCategory.NUMBER,number)] = None

        # 4. 提取带货币或商品数量单位的数字
        for first_number, second_number in (cls.unit_number_pattern.findall(reply_content)):
            number = first_number or second_number
            facts[BusinessFact(FactCategory.NUMBER,number)] = None

        # 5. 提取回复中出现的受支持业务状态
        for status_label in cls.status_labels:
            if status_label in reply_content:
                facts[BusinessFact(FactCategory.STATUS,status_label)] = None
        for before, after in cls.points_pattern.findall(reply_content):
            facts[BusinessFact(FactCategory.POINTS, before or after)] = None
        for before, after in cls.money_pattern.findall(reply_content):
            facts[BusinessFact(FactCategory.MONEY, before or after)] = None
        for match in cls.money_label_pattern.finditer(reply_content):
            if not reply_content[match.end():].lstrip().startswith(("积分", "个积分")):
                facts[BusinessFact(FactCategory.MONEY, match.group(1))] = None

        # 6. 返回去重后且保持提取顺序的业务事实
        return tuple(facts)


class FactChecker:
    """匹配 Agent 回复中的业务事实与成功工具数据"""

    # 匹配完整的纯数字文本
    number_pattern = re.compile(r"^[+-]?\d+(?:\.\d+)?$")

    def get_unsupported_facts(
        self,
        reply_content: str,
        successful_tool_calls: tuple[ToolCallSnapshot, ...]
    ) -> tuple[BusinessFact, ...]:
        """返回无法由成功工具参数或结果支持的业务事实"""

        # 1. 从最终回复中提取需要业务数据支持的事实
        facts = FactExtractor.extract(reply_content)

        # 2. 回复不包含业务事实时直接通过校验
        if not facts:
            return ()

        # 3. 创建数字值和规范化文本值的证据集合
        source_decimals: set[Decimal] = set()
        normalized_sources: set[str] = set()
        source_points: set[Decimal] = set()
        source_money: set[Decimal] = set()

        # 4. 收集成功工具参数及结果中的全部标量证据
        for tool_call in successful_tool_calls:
            for source in (
                tool_call.arguments,
                tool_call.result["data"],
            ):
                for scalar in self._iter_scalars(source):
                    normalized_sources.add(
                        self._normalize_text(scalar)
                    )
                    decimal_value = self._to_decimal(scalar)
                    if decimal_value is not None:
                        source_decimals.add(decimal_value)
            for value in self._iter_points(tool_call.result["data"],
                                           price_only=tool_call.tool_name in ("get_product", "search_products")):
                decimal_value = self._to_decimal(value)
                if decimal_value is not None:
                    source_points.add(decimal_value)
            for value in self._iter_money(tool_call.result["data"],
                                          price_only=tool_call.tool_name in ("get_product", "search_products")):
                decimal_value = self._to_decimal(value)
                if decimal_value is not None:
                    source_money.add(decimal_value)

        # 5. 返回无法在工具证据中找到等价值的业务事实
        return tuple(
            fact
            for fact in facts
            if not self._is_supported(
                fact,
                source_decimals,
                normalized_sources,
                source_points,
                source_money
            )
        )

    @classmethod
    def _is_supported(
        cls,
        fact: BusinessFact,
        source_decimals: set[Decimal],
        normalized_sources: set[str],
        source_points: set[Decimal],
        source_money: set[Decimal]
    ) -> bool:
        """判断一项业务事实是否存在等价工具数据"""

        # 1. 数字事实使用 Decimal 消除字符串格式差异
        if fact.category == FactCategory.NUMBER:
            return Decimal(fact.value) in source_decimals
        if fact.category == FactCategory.POINTS:
            return Decimal(fact.value) in source_points
        if fact.category == FactCategory.MONEY:
            return Decimal(fact.value) in source_money

        # 2. 规范化编号或状态文本后执行匹配
        normalized_value = cls._normalize_text(fact.value)

        # 3. 状态同时匹配中文标签和服务端状态编码
        if fact.category == FactCategory.STATUS:
            return (
                normalized_value in normalized_sources
                or STATUS_CODE_MAP[fact.value] in normalized_sources
            )

        # 4. 业务编号直接匹配规范化文本证据
        return normalized_value in normalized_sources

    @classmethod
    def _iter_points(cls, value: object, price_only: bool = False) -> Iterable[object]:
        """积分证据只取积分字段，不能拿现金价格或库存支持积分结论。"""
        if isinstance(value, dict):
            if price_only:
                # 商品兑换价格只认顶层字段，规格中的赠送积分不能充当兑换价格。
                yield value.get("points_price")
                return
            for key, item in value.items():
                if key.endswith("_points") or key in ("points", "points_price"):
                    yield item
                else:
                    yield from cls._iter_points(item)
        elif isinstance(value, list):
            for item in value:
                yield from cls._iter_points(item, price_only=price_only)

    @classmethod
    def _iter_money(cls, value: object, price_only: bool = False) -> Iterable[object]:
        """现金报价只取金额字段，不能由积分、库存或商品规格中的数字支持。"""
        if isinstance(value, dict):
            if price_only:
                yield value.get("price")
                return
            for key, item in value.items():
                if key in ("price", "total_amount", "unit_price"):
                    yield item
                elif key != "specs":
                    yield from cls._iter_money(item)
        elif isinstance(value, list):
            for item in value:
                yield from cls._iter_money(item, price_only=price_only)

    @classmethod
    def _to_decimal(cls, value: object) -> Decimal | None:
        """将工具数据中的纯数字转换为 Decimal"""

        # 1. 将标量值转换成去除首尾空格的文本
        text = str(value).strip()

        # 2. 非纯数字文本不参与数值事实比较
        if cls.number_pattern.fullmatch(text) is None:
            return None

        # 3. 使用 Decimal 保留准确的业务数值
        return Decimal(text)

    @staticmethod
    def _normalize_text(value: object) -> str:
        """统一文本宽窄字符、大小写和编号分隔符。"""
        # 1. 统一 Unicode、大小写和业务编号连接符
        return (
            unicodedata.normalize("NFKC", str(value))
            .strip()
            .casefold()
            .replace("-", "_")
        )

    @classmethod
    def _iter_scalars(
        cls,
        value: object
    ) -> Iterable[object]:
        """递归读取工具参数和结果中的标量值"""

        # 1. 字典递归读取每一个字段值
        if isinstance(value, dict):
            for nested_value in value.values():
                yield from cls._iter_scalars(nested_value)

        # 2. 列表递归读取每一个元素
        elif isinstance(value, list):
            for nested_value in value:
                yield from cls._iter_scalars(nested_value)

        # 3. 其他值作为可比较的标量证据返回
        else:
            yield value
