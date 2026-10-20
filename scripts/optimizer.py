"""在预算约束下求解营养最贴近目标的点餐方案。

问题本质是一个带约束的多目标组合优化：

    约束：总价 <= 预算
    目标：min 热量偏差 + 蛋白缺口 + 钠超标 + 价格占用

三个目标互相拉扯——想高蛋白就得加钱，想压热量就得砍配餐，
想低钠就得放弃大部分炸物和芝士。所以这里用加权打分而不是硬性求解，
权重表达的是「健康优先级」，也方便用户按需调整。

搜索空间做了两点收窄，避免组合爆炸：

1. 套餐（combo）已经是「主餐+配餐+饮品」的打包价，拆开重组不会更便宜，
   所以整份作为一个候选，不再参与槽位拼装。
2. 单品按主餐 / 配餐 / 饮品分槽，只允许「1 主餐 + 0~1 配餐 + 0~1 饮品」，
   这既是真实点餐习惯，也把组合数从 10^5 量级压到 10^4 以内。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meal import SLOT_DRINK, SLOT_MAIN, SLOT_SIDE, Meal
from nutrition import Nutrition
from tdee import Targets

# 打分权重。热量偏差是主线，蛋白缺口次之，钠超标再次，价格只做轻微调节
# ——价格已经通过硬预算约束管住了，这里只是让同分方案优先选便宜的。
W_KCAL = 1.0
W_PROTEIN = 1.5
W_SODIUM = 0.8
W_PRICE = 0.3

# 热量允许区间。低于目标 30% 的方案基本不顶饱，高于 25% 对减脂不友好。
MIN_KCAL_RATIO = 0.70
MAX_KCAL_RATIO = 1.25


@dataclass
class Plan:
    """一份可执行的点餐方案。"""

    items: list[Meal]
    price: float
    original_price: float
    nutrition: Nutrition
    score: float
    kcal_gap_pct: float
    protein_pct: float
    sodium_pct: float

    @property
    def saved(self) -> float:
        return round(self.original_price - self.price, 2)

    @property
    def names(self) -> list[str]:
        return [f"{m.name} x1" for m in self.items]

    def as_dict(self) -> dict:
        return {
            "items": [
                {"code": m.code, "name": m.name, "price": m.price, "slot": m.slot}
                for m in self.items
            ],
            "price": round(self.price, 2),
            "original_price": round(self.original_price, 2),
            "saved": self.saved,
            "nutrition": self.nutrition.as_dict(),
            "score": round(self.score, 4),
            "kcal_gap_pct": round(self.kcal_gap_pct, 1),
            "protein_pct": round(self.protein_pct, 1),
            "sodium_pct": round(self.sodium_pct, 1),
        }


@dataclass
class SolveResult:
    plans: list[Plan] = field(default_factory=list)
    budget: float = 0.0
    candidates: int = 0
    feasible: int = 0
    note: str = ""


def _score(
    nutrition: Nutrition,
    price: float,
    targets: Targets,
    budget: float,
) -> tuple[float, float, float, float]:
    """返回 (总分, 热量偏差百分比, 蛋白达成率, 钠占上限百分比)。"""
    kcal_gap = (nutrition.kcal - targets.meal_kcal) / targets.meal_kcal
    protein_ratio = nutrition.protein / targets.protein_g if targets.protein_g else 0
    protein_shortfall = max(0.0, 1.0 - protein_ratio)
    sodium_ratio = nutrition.sodium / targets.sodium_mg if targets.sodium_mg else 0
    sodium_excess = max(0.0, sodium_ratio - 1.0)
    price_ratio = price / budget if budget else 0

    score = (
        W_KCAL * abs(kcal_gap)
        + W_PROTEIN * protein_shortfall
        + W_SODIUM * sodium_excess
        + W_PRICE * price_ratio
    )
    return score, kcal_gap * 100, protein_ratio * 100, sodium_ratio * 100


def _make_plan(
    items: list[Meal], targets: Targets, budget: float, use_card: bool = False
) -> Plan | None:
    total = Nutrition.zero()
    for item in items:
        if item.nutrition is None:
            return None
        total = total + item.nutrition

    price = round(sum(m.effective_price(use_card) for m in items), 2)
    original = round(sum(m.original_price for m in items), 2)
    score, kcal_gap, protein_pct, sodium_pct = _score(total, price, targets, budget)
    return Plan(
        items=items,
        price=price,
        original_price=original,
        nutrition=total,
        score=score,
        kcal_gap_pct=kcal_gap,
        protein_pct=protein_pct,
        sodium_pct=sodium_pct,
    )


def solve(
    meals: list[Meal],
    targets: Targets,
    budget: float,
    top_n: int = 5,
    require_drink: bool = False,
    use_card: bool = False,
) -> SolveResult:
    """枚举候选组合并返回得分最高的若干方案。

    结果按「主餐不同」去重——否则 Top5 会全是同一个汉堡配不同饮料，
    对用户没有参考价值。
    """
    if budget <= 0:
        raise ValueError("预算必须大于 0")

    usable = [m for m in meals if m.nutrition is not None]
    combos = [m for m in usable if m.is_combo and m.slot == SLOT_MAIN]
    mains = [m for m in usable if m.slot == SLOT_MAIN and not m.is_combo]
    sides = [m for m in usable if m.slot == SLOT_SIDE]
    drinks = [m for m in usable if m.slot == SLOT_DRINK]

    plans: list[Plan] = []
    candidate_count = 0

    def consider(items: list[Meal]) -> None:
        nonlocal candidate_count
        candidate_count += 1
        if sum(m.effective_price(use_card) for m in items) > budget:
            return
        plan = _make_plan(items, targets, budget, use_card)
        if plan is None:
            return
        # 热量落在可接受区间内才算可行解
        ratio = plan.nutrition.kcal / targets.meal_kcal
        if not (MIN_KCAL_RATIO <= ratio <= MAX_KCAL_RATIO):
            return
        plans.append(plan)

    # 路线一：整份套餐
    for combo in combos:
        consider([combo])

    # 路线二：单品拼装
    side_options: list[list[Meal]] = [[s] for s in sides] + [[]]
    drink_options: list[list[Meal]] = [[d] for d in drinks] + [[]]
    if require_drink:
        drink_options = [[d] for d in drinks]

    for main in mains:
        for side in side_options:
            for drink in drink_options:
                consider([main, *side, *drink])

    plans.sort(key=lambda p: p.score)

    # 按主餐去重，保留每个主餐的最优解
    seen_mains: set[str] = set()
    picked: list[Plan] = []
    for plan in plans:
        key = plan.items[0].code
        if key in seen_mains:
            continue
        seen_mains.add(key)
        picked.append(plan)
        if len(picked) >= top_n:
            break

    note = ""
    if not plans:
        note = (
            f"在 ¥{budget:.0f} 预算内没有找到热量落在 "
            f"{MIN_KCAL_RATIO:.0%}~{MAX_KCAL_RATIO:.0%} 目标区间的组合，"
            "建议放宽预算或调整目标。"
        )

    return SolveResult(
        plans=picked,
        budget=budget,
        candidates=candidate_count,
        feasible=len(plans),
        note=note,
    )


def _top_sodium_source(plan: Plan):
    """找出方案里钠贡献最大的那一件（套餐则细到子项）。

    泛泛建议「少放盐」没有意义，必须指到具体那一件，用户才知道该换什么。
    """
    pool = []
    for meal in plan.items:
        if meal.is_combo and meal.components:
            pool.extend([c for c in meal.components if c.nutrition is not None])
        elif meal.nutrition is not None:
            pool.append(meal)
    if not pool:
        return None
    return max(pool, key=lambda item: item.nutrition.sodium)


def gap_analysis(plan: Plan, targets: Targets) -> list[str]:
    """把方案与目标的差距翻译成人话。"""
    n = plan.nutrition
    out: list[str] = []

    kcal_diff = n.kcal - targets.meal_kcal
    if abs(kcal_diff) <= targets.meal_kcal * 0.08:
        out.append(f"热量 {n.kcal:.0f}kcal，与目标 {targets.meal_kcal:.0f}kcal 基本吻合。")
    elif kcal_diff > 0:
        out.append(f"热量超出目标 {kcal_diff:.0f}kcal（{plan.kcal_gap_pct:+.0f}%）。")
    else:
        out.append(f"热量低于目标 {abs(kcal_diff):.0f}kcal（{plan.kcal_gap_pct:+.0f}%），可能需要补一份加餐。")

    if n.protein >= targets.protein_g:
        out.append(f"蛋白质 {n.protein:.0f}g，达成目标的 {plan.protein_pct:.0f}%。")
    else:
        out.append(
            f"蛋白质 {n.protein:.0f}g，缺 {targets.protein_g - n.protein:.0f}g"
            f"（达成 {plan.protein_pct:.0f}%），可以考虑加一份高蛋白单品。"
        )

    if n.sodium > targets.sodium_mg:
        excess = n.sodium - targets.sodium_mg
        worst = _top_sodium_source(plan)
        if worst is not None:
            out.append(
                f"钠 {n.sodium:.0f}mg，超出单餐上限 {targets.sodium_mg:.0f}mg 约 {excess:.0f}mg；"
                f"主要来自「{worst.name}」（{worst.nutrition.sodium:.0f}mg），"
                "这是麦当劳餐品的普遍特征，若当日其他两餐清淡即可平衡。"
            )
        else:
            out.append(
                f"钠 {n.sodium:.0f}mg，超出单餐上限 {targets.sodium_mg:.0f}mg 约 {excess:.0f}mg。"
            )
    else:
        out.append(f"钠 {n.sodium:.0f}mg，在单餐上限 {targets.sodium_mg:.0f}mg 之内。")

    return out
