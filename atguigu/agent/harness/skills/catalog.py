from atguigu.agent.harness.skills.definition import SkillCode, SkillDefinition


class SkillCatalog:
    """集中保存子智能体的领域指引与工具范围。"""

    def __init__(
            self,
            definitions: tuple[SkillDefinition, ...]
    ):
        self.definitions = {
            definition.code: definition
            for definition in definitions
        }

    def get_skill(self, skill_code: SkillCode) -> SkillDefinition:
        return self.definitions[skill_code]

    def get_skills(self) -> tuple[SkillDefinition, ...]:
        return tuple(self.definitions.values())


SKILL_CATALOG = SkillCatalog(
    (
        SkillDefinition(
            code=SkillCode.PRODUCT_SERVICE,
            description="商品搜索、商品详情、规格、兑换积分和实时库存",
            guidance=(
                "查询入口：用户没有提供商品编号时，先使用 search_products 搜索商品。",
                "积分选品：用户给出积分预算时，传入 max_points；仅使用明确返回的 points_price，不用现金价格换算。",
                "兑换边界：商品符合预算不等于可兑换，具体条件交由积分专家预校验。",
                "工具选择：商品基础信息使用 get_product，实时库存使用 get_product_stock。",
                "可信依据：商品信息和库存结论只能来自对应工具的成功结果。",
                "边界处理：工具失败或没有结果时不得猜测商品信息。"
            ),
            tools=("search_products", "get_product", "get_product_stock")
        ),
        SkillDefinition(
            code=SkillCode.ORDER_SERVICE,
            description="订单列表、订单详情、订单状态、兑换积分和订单页面引导",
            guidance=(
                "查询入口：用户没有提供订单编号时，先使用 list_orders 查询订单列表。",
                "工具选择：查询具体订单的状态、兑换积分和商品信息时使用 get_order。",
                "积分展示：积分订单只按 total_points 和商品 unit_points 回答，不把现金 total_amount、unit_price 换算成积分；缺失时明确未记录。",
                "可信依据：订单结论和具体订单页面动作必须来自 get_order 的成功结果。",
                "边界处理：只有用户明确要求页面引导或提出必须在页面完成的操作时才能生成订单页面动作；取消订单或修改地址不能声称已经执行。"
            ),
            tools=("list_orders", "get_order")
        ),
        SkillDefinition(
            code=SkillCode.LOGISTICS_SERVICE,
            description="具体订单的发货状态、承运公司、运单号和物流轨迹",
            guidance=(
                "查询入口：用户没有提供订单编号时，先使用 list_orders 查询订单列表。",
                "工具选择：订单基础信息使用 get_order，配送进度和物流轨迹使用 get_logistics。",
                "可信依据：物流公司、运单号、状态和轨迹只能来自 get_logistics 的成功结果。",
                "边界处理：物流工具失败或没有结果时不得猜测配送进度。"
            ),
            tools=("list_orders", "get_order", "get_logistics")
        ),
        SkillDefinition(
            code=SkillCode.POLICY_SERVICE,
            description="平台售后规则及企业商城积分使用、有效期和退款返还规则，不查询实际积分或售后状态",
            guidance=(
                "查询入口：用户询问平台规则、服务政策或帮助说明时必须先查询知识内容。",
                "工具选择：使用 search_knowledge 查询与用户问题直接相关的知识。",
                "积分规则：调用 search_knowledge 时指定 topic=points，查询当前用户所属商城的有效规则；未查到不得猜测。",
                "可信依据：平台规则只能来自 search_knowledge 返回的知识内容。",
                "边界处理：知识内容不用于判断用户实际售后申请状态。"
            ),
            tools=("search_knowledge",)
        ),
        SkillDefinition(
            code=SkillCode.POINTS_SERVICE,
            description="积分余额、流水、到期批次、商品兑换预校验、订单扣分与积分返还状态，不解释企业规则",
            guidance=(
                "余额、冻结与到期概况使用 get_points_account；需要到期时间或批次时设置 include_expiry=true，按需分页。",
                "积分减少使用 get_points_transactions，区分消费、过期、冻结和返还；分页不代表全部历史或全部批次。",
                "商品和数量明确后使用 check_points_eligibility，能否兑换以接口结论为准，不能只比较余额。",
                "订单扣分与积分返还统一查 get_order_points；分别读取实际扣分、应返还、已返还及返还状态，取消不代表到账。",
                "企业积分规则交给政策专家；不得承诺积分补发、解冻、自动延期或执行退款。",
                "账户、流水、批次编号按后端原值使用，不自行构造；账户或订单不明确时请求澄清。",
                "证据 resource_id：余额用 account_id，流水用 transaction_id，到期用 batch_id，预校验用 product_id，订单查询用 order_id。",
                "查询失败时明确无法确认；积分异常需要人工核查时交由主控沿用转人工流程。",
            ),
            tools=("get_points_account", "get_points_transactions", "check_points_eligibility", "get_order_points"),
        ),
        SkillDefinition(
            code=SkillCode.AFTER_SALES_SERVICE,
            description="用户实际售后申请记录、处理状态、进度和售后页面引导，不解释平台售后规则",
            guidance=(
                "查询入口：用户询问实际退款、退货或换货进度时，使用 list_after_sales 查询售后记录。",
                "工具选择：用户提供订单编号时按订单查询；生成具体订单页面动作前使用 get_order 确认订单。",
                "可信依据：售后状态和处理进度只能来自 list_after_sales 的成功结果。",
                "边界处理：售后记录不用于解释平台通用售后规则。"
            ),
            tools=("get_order", "list_after_sales")
        )
    )
)
