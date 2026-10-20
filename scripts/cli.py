"""命令行入口。

设计原则：这个脚本**不发网络请求**。MCP 调用由 Agent 侧完成，
脚本只吃 JSON、出方案。好处是零依赖、可离线复现、可单测，
也避免了把 MCP Token 写进仓库——参赛声明的信息安全要求。

用法：

    # 1) 体检：看菜单里有哪��餐品的营养能对上
    python scripts/cli.py audit --menu menu.json

    # 2) 出方案
    python scripts/cli.py plan --menu menu.json \\
        --weight 70 --height 175 --age 28 --sex male \\
        --goal cut --meal lunch --budget 35

menu.json 为 query-meals 的原始返回；若含套餐，可另用 --details 传入
code -> query-meal-detail 返回体的映射文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 保证 Windows 终端下中文不乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meal import SLOT_DRINK, SLOT_MAIN, SLOT_SIDE, SLOT_UNKNOWN, build_menu  # noqa: E402
from nutrition import NutritionTable  # noqa: E402
from optimizer import gap_analysis, solve  # noqa: E402
from tdee import ACTIVITY_LEVELS, GOALS, MEAL_SPLIT, Profile, compute_targets  # noqa: E402


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_menu_and_details(menu_path: str, details_path: str | None) -> tuple[dict, dict | None]:
    """同时支持两种输入形态：

    * ``fetch_mcp.py`` 产出的 bundle：``{"menu": {...}, "details": {...}}``
    * ``query-meals`` 的原始返回，此时 details 由 ``--details`` 单独提供
    """
    data = _load_json(menu_path)
    if isinstance(data, dict) and "menu" in data:
        details = dict(data.get("details") or {})
        if details_path:
            details.update(_load_json(details_path))
        return data["menu"], details
    details = _load_json(details_path) if details_path else None
    return data, details


def _fmt_money(value: float) -> str:
    return f"¥{value:.2f}"


def _print_item(item, indent: str = "   ", use_card: bool = False) -> None:
    """打印一件餐品。套餐展开子项，替换过的子项单独标注。

    必须展开：用户看到「猪柳蛋麦满分套餐 546kcal」是没法判断这 546 从哪来的，
    更没法知道自己到底会吃到什么。营养师如果说不清构成，就没有可信度。
    """
    n = item.nutrition
    shown_price = item.effective_price(use_card)
    print(f"{indent}- {item.name:<24} {_fmt_money(shown_price):>8}   "
          f"{n.kcal:>5.0f}kcal  蛋白 {n.protein:>4.0f}g")

    if item.requires_card and item.card_price is not None and not use_card:
        print(f"{indent}  ↳ 随单购价 {_fmt_money(item.card_price)}"
              f"（需先购早餐卡，此处按原价计入预算）")

    if not item.is_combo:
        return

    for comp in item.components:
        cn = comp.nutrition
        kcal = f"{cn.kcal:>5.0f}kcal" if cn else "   未知"
        protein = f"蛋白 {cn.protein:>3.0f}g" if cn else ""
        print(f"{indent}    · {comp.name:<20} {kcal}  {protein}")
        if comp.swapped_from:
            sign = f"{comp.diff_price:+.2f}".rstrip("0").rstrip(".")
            print(f"{indent}      ↳ 已由「{comp.swapped_from}」替换（{sign} 元），"
                  f"因原选项无公开营养数据")


def cmd_audit(args: argparse.Namespace) -> int:
    table = NutritionTable(args.nutrition)
    menu_payload, details = _load_menu_and_details(args.menu, args.details)
    meals = build_menu(menu_payload, table, details)

    total = len(meals)
    covered = [m for m in meals if m.has_nutrition]
    missing = [m for m in meals if not m.has_nutrition]

    print(f"营养数据表：{len(table)} 条")
    print(f"菜单餐品：{total} 件")
    print(f"营养可算：{len(covered)} 件（{len(covered) / total:.1%}）")
    print(f"营养缺失：{len(missing)} 件")

    swapped = [m for m in covered if m.swaps]
    if swapped:
        print(f"其中 {len(swapped)} 件套餐含选项替换（原选项无公开营养数据）")
    print()

    by_slot: dict[str, list] = {}
    for meal in covered:
        by_slot.setdefault(meal.slot, []).append(meal)
    labels = {SLOT_MAIN: "主餐", SLOT_SIDE: "配餐", SLOT_DRINK: "饮品", SLOT_UNKNOWN: "未分类"}
    for slot, items in sorted(by_slot.items()):
        print(f"  {labels.get(slot, slot):<4} {len(items):>3} 件可算")

    if missing:
        print()
        print("以下餐品缺少营养数据，不会进入推荐：")
        for meal in missing:
            print(f"  - {meal.name}（{meal.code}，{_fmt_money(meal.price)}）")

    if args.json:
        print()
        print(json.dumps(
            {
                "total": total,
                "covered": len(covered),
                "coverage": round(len(covered) / total, 4) if total else 0,
                "missing": [{"code": m.code, "name": m.name} for m in missing],
            },
            ensure_ascii=False,
            indent=2,
        ))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    table = NutritionTable(args.nutrition)
    menu_payload, details = _load_menu_and_details(args.menu, args.details)
    meals = build_menu(menu_payload, table, details)

    profile = Profile(
        weight_kg=args.weight,
        height_cm=args.height,
        age=args.age,
        sex=args.sex,
        activity=args.activity,
        goal=args.goal,
    )
    targets = compute_targets(profile, args.meal)
    result = solve(
        meals,
        targets,
        args.budget,
        top_n=args.top,
        require_drink=args.require_drink,
        use_card=args.with_card,
    )

    if args.json:
        print(json.dumps(
            {
                "targets": targets.as_dict(),
                "budget": args.budget,
                "feasible": result.feasible,
                "plans": [p.as_dict() for p in result.plans],
                "note": result.note,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    goal_label = GOALS[profile.goal][2]
    print("=" * 62)
    print(f"  麦门营养师 · {targets.meal_label}方案")
    print("=" * 62)
    print(f"身体数据：{profile.weight_kg:.0f}kg / {profile.height_cm:.0f}cm / {profile.age}岁 / "
          f"{'男' if profile.sex == 'male' else '女'}")
    print(f"活动水平：{ACTIVITY_LEVELS[profile.activity][1]}")
    print(f"目标：{goal_label}　预算：{_fmt_money(args.budget)}"
          + ("　（已持早餐卡，按卡价计算）" if args.with_card else ""))
    print()
    print(f"BMR {targets.bmr:.0f} kcal　→　TDEE {targets.tdee:.0f} kcal　"
          f"→　每日 {targets.daily_kcal:.0f} kcal")
    print(f"本餐目标：{targets.meal_kcal:.0f} kcal　蛋白 {targets.protein_g:.0f}g　"
          f"脂肪 {targets.fat_g:.0f}g　碳水 {targets.carb_g:.0f}g　钠 ≤{targets.sodium_mg:.0f}mg")
    print()

    for note in targets.notes:
        print(f"⚠ {note}")
    if targets.notes:
        print()

    if not result.plans:
        print(result.note)
        return 1

    print(f"共评估 {result.candidates} 个组合，{result.feasible} 个满足预算与热量约束。")
    print()

    for idx, plan in enumerate(result.plans, start=1):
        print(f"【方案 {idx}】{_fmt_money(plan.price)}"
              + (f"（原价 {_fmt_money(plan.original_price)}，省 {_fmt_money(plan.saved)}）"
                 if plan.saved > 0.01 else ""))
        for item in plan.items:
            _print_item(item, use_card=args.with_card)
        print(f"   合计：{plan.nutrition.kcal:.0f}kcal　蛋白 {plan.nutrition.protein:.0f}g　"
              f"脂肪 {plan.nutrition.fat:.0f}g　碳水 {plan.nutrition.carb:.0f}g　"
              f"钠 {plan.nutrition.sodium:.0f}mg")
        for line in gap_analysis(plan, targets):
            print(f"   · {line}")
        codes = ", ".join(m.code for m in plan.items)
        print(f"   下单 code：{codes}")
        print()

    print("提示：营养值为官方公开数据的估算结果，不构成医疗或营养建议。")
    print("      实际餐品与价格以麦当劳官方渠道为准。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcd-macro-coach",
        description="麦当劳 MCP 营养配餐求解器",
    )
    parser.add_argument("--nutrition", default=None, help="营养数据 TSV 路径，默认用内置快照")

    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="检查菜单营养数据覆盖率")
    audit.add_argument("--menu", required=True, help="query-meals 返回的 JSON 文件")
    audit.add_argument("--details", default=None, help="套餐详情 JSON（code -> detail）")
    audit.add_argument("--json", action="store_true", help="输出 JSON")
    audit.set_defaults(func=cmd_audit)

    plan = sub.add_parser("plan", help="生成点餐方案")
    plan.add_argument("--menu", required=True, help="query-meals 返回的 JSON 文件")
    plan.add_argument("--details", default=None, help="套餐详情 JSON（code -> detail）")
    plan.add_argument("--weight", type=float, required=True, help="体重 kg")
    plan.add_argument("--height", type=float, required=True, help="身高 cm")
    plan.add_argument("--age", type=int, required=True, help="年龄")
    plan.add_argument("--sex", choices=["male", "female"], required=True)
    plan.add_argument("--activity", choices=list(ACTIVITY_LEVELS), default="light")
    plan.add_argument("--goal", choices=list(GOALS), default="maintain")
    plan.add_argument("--meal", choices=list(MEAL_SPLIT), default="lunch")
    plan.add_argument("--budget", type=float, required=True, help="预算（元）")
    plan.add_argument("--top", type=int, default=3, help="返回方案数")
    plan.add_argument("--require-drink", action="store_true", help="方案必须含饮品")
    plan.add_argument("--with-card", action="store_true",
                      help="按随单购价计算（用户已持有早餐卡）")
    plan.add_argument("--json", action="store_true", help="输出 JSON")
    plan.set_defaults(func=cmd_plan)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
