from dataclasses import dataclass

from atguigu.agent.harness.skills.catalog import SKILL_CATALOG
from atguigu.agent.harness.skills.definition import SkillCode


@dataclass(frozen=True)
class DomainSpec:
    """复用原 Skill 的指引与工具范围，避免维护两份领域配置。"""

    code: str
    label: str
    skill_code: SkillCode

    @property
    def tools(self) -> tuple[str, ...]:
        return SKILL_CATALOG.get_skill(self.skill_code).tools

    @property
    def description(self) -> str:
        return SKILL_CATALOG.get_skill(self.skill_code).description


DOMAIN_SPECS = {
    spec.code: spec for spec in (
        DomainSpec('product', '商品', SkillCode.PRODUCT_SERVICE),
        DomainSpec('order', '订单', SkillCode.ORDER_SERVICE),
        DomainSpec('logistics', '物流', SkillCode.LOGISTICS_SERVICE),
        DomainSpec('aftersales', '售后', SkillCode.AFTER_SALES_SERVICE),
        DomainSpec('policy', '政策', SkillCode.POLICY_SERVICE),
        DomainSpec('points', '积分', SkillCode.POINTS_SERVICE),
    )
}
