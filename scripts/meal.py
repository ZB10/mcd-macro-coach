"""餐品建模与套餐拆解。

营养表只提供**单品**数据，但菜单上卖的大多是套餐/组合。
一份「猪柳蛋麦满分套餐」在 ``query-meals`` 里只是一个 productCode 和
一个价格，没有任何营养信息。要算出它的热量，必须先通过
``query-meal-detail`` 拿到 rounds，还原出实际包含哪几件单品，
再逐件查营养表相加。

这个还原过程有两个坑：

1. ``rounds[].choices[]`` 里 ``isDefault`` 和 ``quantity`` 可能互相矛盾。
   实测「猪柳蛋麦满分套餐」的饮料轮次中，``大杯鲜萃咖啡`` 的 isDefault=1
   但 quantity=0，而 ``鲜萃冰咖`` 的 isDefault=0 但 quantity=1。
   说明 quantity 反映的是当前实际选择，isDefault 只是推荐项。
   因此取值优先级是 quantity > isDefault。
2. 一个 round 可能允许选多个（maxQuantity>1），必须按 quantity 累加，
   不能只取第一项。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nutrition import Nutrition, NutritionTable

# 分类关键词。顺序即优先级：先判饮品，再判配餐，最后落到主餐。
# 「麦旋风」「新地」这类词不含饮料名，必须显式列出，否则会被当成主餐。
_DRINK_KEYS = (
    "可乐", "雪碧", "怡泉", "咖啡", "美式", "奶铁", "拿铁", "卡布奇诺",
    "玛奇朵", "红茶", "伯爵", "豆浆", "牛奶", "苹果汁", "果汁", "橙橙",
    "柠柠", "雪冰", "黑巧", "燕麦奶", "抹茶牛奶", "纯悦", "阿芙佳朵",
    "阳光橙", "朱古力",
)

_SIDE_KEYS = (
    "薯条", "薯饼", "油条", "玉米杯", "麦乐鸡", "鸡翅", "鸡球", "鸡排",
    "派", "冰淇淋", "圆筒", "新地", "麦旋风", "香肠", "苹果片", "笋卷",
)

_MAIN_KEYS = ("堡", "麦满分", "松饼", "卷", "粥")

# 需要「随单购」才生效的优惠类型。
#
# 这是个容易踩的坑：query-meals 返回的 currentPrice 可能已经算进了
# 「随单购早餐卡优惠」，但那个价格的前提是先买一张早餐卡。
# 实测「原味板烧鸡腿炒双蛋堡组合」currentPrice=13.20、originalPrice=22.00，
# 而 calculate-price 实际算出来是 22.00 —— 说明 13.20 是有条件价。
# 若直接拿 currentPrice 当支付价做预算约束，用户结账时会发现钱不够。
_REQUIRES_CARD_HINTS = ("随单购",)

# 分类结果。unknown 的餐品不会进入推荐候选，只在报告里列出。
SLOT_MAIN = "main"
SLOT_SIDE = "side"
SLOT_DRINK = "drink"
SLOT_UNKNOWN = "unknown"


@dataclass
class Component:
    """套餐里的一个子项。

    ``swapped_from`` 非空表示这一项被替换过：原选项在营养表里查不到，
    于是换成了同一轮选配里能查到数据的选项。这会改变用户实际吃到的内容，
    所以必须显式记录并在输出中标注。
    """

    code: str
    name: str
    quantity: int
    nutrition: Nutrition | None
    confidence: str
    diff_price: float = 0.0
    swapped_from: str | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "quantity": self.quantity,
            "nutrition": self.nutrition.as_dict() if self.nutrition else None,
            "confidence": self.confidence,
            "diff_price": self.diff_price,
            "swapped_from": self.swapped_from,
        }


@dataclass
class Meal:
    """菜单上的一件商品，可能是单品也可能是套餐。

    ``price`` 是**实际支付价**：若该商品依赖随单购优惠，这里取原价，
    条件价放在 ``card_price``。这样预算约束才和收银台一致。
    """

    code: str
    name: str
    price: float
    original_price: float
    card_price: float | None = None
    requires_card: bool = False
    category: str = ""
    tags: list[str] = field(default_factory=list)
    discount_type: str | None = None
    with_order: dict | None = None
    nutrition: Nutrition | None = None
    match_confidence: str = "none"
    matched_name: str | None = None
    components: list[Component] = field(default_factory=list)
    slot: str = SLOT_UNKNOWN

    @property
    def is_combo(self) -> bool:
        return bool(self.components)

    @property
    def has_nutrition(self) -> bool:
        return self.nutrition is not None

    @property
    def discount(self) -> float:
        return round(self.original_price - self.price, 2)

    @property
    def swaps(self) -> list[Component]:
        """被替换过的子项。输出时必须提示用户。"""
        return [c for c in self.components if c.swapped_from]

    def effective_price(self, use_card: bool = False) -> float:
        """实际参与预算计算的单价。

        ``use_card=True`` 表示用户已持有早餐卡，可以享受随单购价。
        """
        if use_card and self.card_price is not None:
            return self.card_price
        return self.price

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "price": self.price,
            "original_price": self.original_price,
            "card_price": self.card_price,
            "requires_card": self.requires_card,
            "category": self.category,
            "slot": self.slot,
            "is_combo": self.is_combo,
            "nutrition": self.nutrition.as_dict() if self.nutrition else None,
            "match_confidence": self.match_confidence,
            "matched_name": self.matched_name,
            "components": [c.as_dict() for c in self.components],
        }


def classify_name(name: str) -> str:
    """按名称关键词判断餐品属于主餐 / 配餐 / 饮品。"""
    for key in _DRINK_KEYS:
        if key in name:
            return SLOT_DRINK
    for key in _SIDE_KEYS:
        if key in name:
            return SLOT_SIDE
    for key in _MAIN_KEYS:
        if key in name:
            return SLOT_MAIN
    return SLOT_UNKNOWN


def _classify(meal: Meal) -> str:
    """套餐按其主餐子项分类，单品直接按名称分类。

    例如「猪柳蛋麦满分套餐」名字里没有「堡」也没有饮品词，
    但它第一轮选的是猪柳蛋麦满分，所以应归为主餐。
    """
    if meal.is_combo and meal.components:
        first = meal.components[0]
        slot = classify_name(first.name)
        if slot != SLOT_UNKNOWN:
            return slot
    return classify_name(meal.name)


def _parse_diff_price(text: str | None) -> float:
    """把 ``'+ ¥1'`` / ``'- ¥0.5'`` 解析成浮点数。解析失败按 0 处理。"""
    if not text:
        return 0.0
    cleaned = text.replace("¥", "").replace("+", "").replace(" ", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _resolve_choice(
    choices: list[dict], table: NutritionTable
) -> tuple[dict | None, object, str | None, float]:
    """在一轮选配里确定实际子项，必要时替换成营养数据可查的选项。

    返回 ``(choice, match, swapped_from, extra_price)``。

    为什么要替换：套餐的默认饮料常常是「鲜萃冰咖」这类营养表里没有的品名，
    而同一轮选配里通常还摆着「大杯鲜萃咖啡」「优品豆浆大杯」等可查选项。
    与其整份套餐算不出来，不如换一款等价饮料——但必须记录换了什么、
    以及价格差多少，让用户自己决定是否接受。
    """
    picked = [c for c in choices if int(c.get("quantity") or 0) > 0]
    if not picked:
        picked = [c for c in choices if int(c.get("isDefault") or 0) == 1][:1]
    if not picked:
        return None, None, None, 0.0

    choice = picked[0]
    match = table.lookup(choice.get("name") or "")
    if match.ok:
        return choice, match, None, 0.0

    # 原选项查不到，找同轮的可查替代项。
    # 排序用 |差价| 而不是差价：优先选不加钱也不省钱的选项。
    # 若按差价升序，会优先挑最便宜的（常常是可乐），把一杯咖啡换成可乐，
    # 热量能翻十倍——那不是「替换」，是偷换。
    others = sorted(
        (c for c in choices if c is not choice),
        key=lambda c: abs(_parse_diff_price(c.get("diffPrice"))),
    )
    original_diff = _parse_diff_price(choice.get("diffPrice"))
    for cand in others:
        cand_match = table.lookup(cand.get("name") or "")
        if cand_match.ok:
            extra = _parse_diff_price(cand.get("diffPrice")) - original_diff
            return cand, cand_match, (choice.get("name") or ""), round(extra, 2)

    return choice, match, None, 0.0


def build_components(
    detail: dict, table: NutritionTable
) -> tuple[list[Component], float]:
    """从 ``query-meal-detail`` 的返回里还原套餐子项。

    每个 round 表示一次选配（主餐 / 小食 / 饮料）。
    返回子项列表与因替换产生的额外价格。
    """
    components: list[Component] = []
    extra_total = 0.0

    for rnd in detail.get("rounds") or []:
        choices = rnd.get("choices") or []
        if not choices:
            continue

        choice, match, swapped_from, extra = _resolve_choice(choices, table)
        if choice is None:
            continue

        name = (choice.get("name") or "").strip()
        if not name:
            continue

        extra_total += extra
        components.append(
            Component(
                code=str(choice.get("code") or ""),
                name=name,
                quantity=int(choice.get("quantity") or 1),
                nutrition=match.nutrition if match else None,
                confidence=match.confidence if match else "none",
                diff_price=_parse_diff_price(choice.get("diffPrice")),
                swapped_from=swapped_from,
            )
        )

    return components, round(extra_total, 2)


def _sum_components(components: list[Component]) -> Nutrition | None:
    """套餐营养 = 子项营养之和。任一子项缺失则整份套餐判为不可用。

    这里刻意不做部分求和：少算一项会让热量偏低几十到上百大卡，
    对减脂用户是实质性误导。宁可标记为「未知」。
    """
    if not components:
        return None
    total = Nutrition.zero()
    for comp in components:
        if comp.nutrition is None:
            return None
        for _ in range(max(comp.quantity, 1)):
            total = total + comp.nutrition
    return total


def build_meal(
    code: str,
    entry: dict,
    table: NutritionTable,
    category: str = "",
    detail: dict | None = None,
) -> Meal:
    """由 ``query-meals`` 的 meals 条目（可选附带 detail）构造 Meal。"""
    name = (entry.get("name") or "").strip()
    list_price = float(entry.get("originalPrice") or 0)
    current = float(entry.get("currentPrice") or list_price)
    discount_type = entry.get("discountType") or ""

    # 随单购优惠：currentPrice 是买卡后才生效的价，不能当作支付价。
    requires_card = any(hint in discount_type for hint in _REQUIRES_CARD_HINTS)
    if requires_card:
        price = list_price
        card_price = current
    else:
        price = current
        card_price = None

    meal = Meal(
        code=str(code),
        name=name,
        price=price,
        original_price=list_price,
        card_price=card_price,
        requires_card=requires_card,
        category=category,
        tags=list(entry.get("tags") or []),
        discount_type=discount_type or None,
        with_order=entry.get("withOrder"),
    )

    if detail and detail.get("rounds"):
        components, extra = build_components(detail, table)
        meal.components = components
        # 替换子项会带来差价，必须同步到价格，否则预算约束会失真
        meal.price = round(meal.price + extra, 2)
        meal.nutrition = _sum_components(components)
        if meal.nutrition is not None:
            meal.match_confidence = "combo"
    else:
        match = table.lookup(name)
        meal.nutrition = match.nutrition
        meal.match_confidence = match.confidence
        meal.matched_name = match.matched_name

    meal.slot = _classify(meal)
    return meal


def build_menu(
    payload: dict,
    table: NutritionTable,
    details: dict[str, dict] | None = None,
) -> list[Meal]:
    """把 ``query-meals`` 的完整返回转成 Meal 列表。

    ``details`` 是 code -> query-meal-detail 返回体的映射，
    由调用方按需抓取后传入。没有 detail 的套餐会因名称无法命中营养表
    而被标记为 unknown，这是预期行为。
    """
    data = payload.get("data") or payload
    meals_map = data.get("meals") or {}
    categories = data.get("categories") or []

    # 建立 code -> 分类名 的索引。同一商品可能出现在多个分类里，取第一个。
    code_to_category: dict[str, str] = {}
    for cat in categories:
        cat_name = (cat.get("name") or "").replace("\n", " ").strip()
        for item in cat.get("meals") or []:
            code = str(item.get("code") or "")
            if code and code not in code_to_category:
                code_to_category[code] = cat_name

    details = details or {}
    result: list[Meal] = []
    for code, entry in meals_map.items():
        meal = build_meal(
            code=str(code),
            entry=entry,
            table=table,
            category=code_to_category.get(str(code), ""),
            detail=details.get(str(code)),
        )
        result.append(meal)

    # 按分类顺序稳定排序，便于阅读与复现
    order = {c.get("name", ""): i for i, c in enumerate(categories)}
    result.sort(key=lambda m: (order.get(m.category, 999), -m.price))
    return result
