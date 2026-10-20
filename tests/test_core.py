"""核心逻辑测试。

重点覆盖三类容易悄悄出错的地方：

1. 名称对齐——错配比匹配不上更危险，会把热量算歪还看不出来。
2. 套餐拆解——子项选错或漏算，整份套餐的营养就失真。
3. 价格口径——把「随单购价」当成支付价，用户结账时会发现钱不够。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from meal import SLOT_DRINK, SLOT_MAIN, build_menu, build_meal  # noqa: E402
from nutrition import Nutrition, NutritionTable, normalize  # noqa: E402
from optimizer import solve  # noqa: E402
from tdee import Profile, basal_metabolic_rate, compute_targets  # noqa: E402

BUNDLE_PATH = ROOT / "examples" / "demo_store.json"


class TestNormalize(unittest.TestCase):
    def test_fullwidth_and_symbols(self):
        self.assertEqual(normalize("纯牛奶（盒装）"), normalize("纯牛奶(盒装)"))
        self.assertEqual(normalize("麦咖啡™美式"), "麦咖啡美式")

    def test_whitespace_removed(self):
        self.assertEqual(normalize("100% 苹果汁"), normalize("100%苹果汁"))


class TestLookup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = NutritionTable()

    def test_exact(self):
        match = self.table.lookup("巨无霸")
        self.assertTrue(match.ok)
        self.assertEqual(match.confidence, "exact")
        self.assertEqual(match.nutrition.kcal, 513)

    def test_fullwidth_paren_alias(self):
        # 菜单写「纯牛奶(盒装)」，营养表写「纯牛奶（盒装）」
        match = self.table.lookup("纯牛奶(盒装)")
        self.assertTrue(match.ok)
        self.assertEqual(match.nutrition.kcal, 129)

    def test_brand_prefix_stripped(self):
        # 菜单写「麦咖啡™热美式中杯」，营养表写「热美式中杯」
        match = self.table.lookup("麦咖啡™热美式中杯")
        self.assertTrue(match.ok)
        self.assertEqual(match.matched_name, "热美式中杯")

    def test_default_cup_size(self):
        # 菜单省略杯型，按中杯对齐
        match = self.table.lookup("可乐")
        self.assertTrue(match.ok)
        self.assertEqual(match.confidence, "spec-default")
        self.assertEqual(match.matched_name, "可乐中杯")

    def test_rejects_size_mismatch(self):
        # 「优品豆浆」只有大杯/小杯，没有中杯，必须判为未知而不是猜一个
        match = self.table.lookup("优品豆浆")
        self.assertFalse(match.ok, "不应把无规格名称猜成某个杯型")

    def test_unknown_returns_none(self):
        self.assertFalse(self.table.lookup("旋旋水手帽").ok)
        self.assertFalse(self.table.lookup("派").ok)
        self.assertFalse(self.table.lookup("").ok)


class TestTDEE(unittest.TestCase):
    def test_male_bmr(self):
        profile = Profile(weight_kg=70, height_cm=175, age=28, sex="male")
        # 10*70 + 6.25*175 - 5*28 + 5
        self.assertAlmostEqual(basal_metabolic_rate(profile), 1658.75, places=2)

    def test_female_bmr(self):
        profile = Profile(weight_kg=55, height_cm=165, age=25, sex="female")
        # 10*55 + 6.25*165 - 5*25 - 161
        self.assertAlmostEqual(basal_metabolic_rate(profile), 1295.25, places=2)

    def test_meal_split_conserves_energy(self):
        profile = Profile(weight_kg=70, height_cm=175, age=28, sex="male", goal="cut")
        lunch = compute_targets(profile, "lunch")
        self.assertAlmostEqual(lunch.meal_kcal, lunch.daily_kcal * 0.35, places=4)

    def test_invalid_profile_rejected(self):
        with self.assertRaises(ValueError):
            compute_targets(Profile(weight_kg=10, height_cm=175, age=28, sex="male"))


class TestMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = NutritionTable()
        bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
        cls.meals = build_menu(bundle["menu"], cls.table, bundle["details"])
        cls.by_code = {m.code: m for m in cls.meals}

    def test_combo_detected(self):
        meal = self.by_code["9900003424"]
        self.assertTrue(meal.is_combo)
        self.assertGreaterEqual(len(meal.components), 2)

    def test_combo_nutrition_is_component_sum(self):
        for meal in self.meals:
            if not meal.is_combo or meal.nutrition is None:
                continue
            total = Nutrition.zero()
            for comp in meal.components:
                for _ in range(max(comp.quantity, 1)):
                    total = total + comp.nutrition
            self.assertAlmostEqual(meal.nutrition.kcal, total.kcal, places=4,
                                   msg=f"{meal.name} 营养合计与子项不符")

    def test_card_price_not_used_as_payable(self):
        # 该商品是随单购早餐卡优惠，支付价必须取原价
        meal = self.by_code["9900003424"]
        self.assertTrue(meal.requires_card)
        self.assertAlmostEqual(meal.price, 22.0, places=2)
        self.assertAlmostEqual(meal.card_price, 13.2, places=2)
        self.assertAlmostEqual(meal.effective_price(use_card=True), 13.2, places=2)

    def test_plain_discount_uses_current_price(self):
        # 「促销优惠」是直接生效的，不走卡
        meal = self.by_code["507368"]
        self.assertFalse(meal.requires_card)
        self.assertAlmostEqual(meal.price, 11.9, places=2)

    def test_slot_classification(self):
        self.assertEqual(self.by_code["9900003424"].slot, SLOT_MAIN)
        self.assertEqual(self.by_code["507368"].slot, SLOT_DRINK)
        self.assertEqual(self.by_code["3757"].slot, SLOT_DRINK)


class TestSolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = NutritionTable()
        bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
        cls.meals = build_menu(bundle["menu"], cls.table, bundle["details"])
        cls.profile = Profile(weight_kg=70, height_cm=175, age=28, sex="male", goal="cut")

    def test_respects_budget(self):
        targets = compute_targets(self.profile, "breakfast")
        for budget in (15, 20, 30):
            result = solve(self.meals, targets, budget, top_n=5)
            for plan in result.plans:
                self.assertLessEqual(plan.price, budget + 1e-6,
                                     f"方案 {plan.names} 超出预算 {budget}")

    def test_no_duplicate_mains(self):
        targets = compute_targets(self.profile, "breakfast")
        result = solve(self.meals, targets, 40, top_n=5)
        codes = [p.items[0].code for p in result.plans]
        self.assertEqual(len(codes), len(set(codes)))

    def test_infeasible_budget_returns_empty(self):
        targets = compute_targets(self.profile, "breakfast")
        result = solve(self.meals, targets, 1.0, top_n=5)
        self.assertEqual(result.plans, [])
        self.assertTrue(result.note)

    def test_every_plan_has_nutrition(self):
        targets = compute_targets(self.profile, "breakfast")
        result = solve(self.meals, targets, 40, top_n=5)
        for plan in result.plans:
            self.assertGreater(plan.nutrition.kcal, 0)
            for item in plan.items:
                self.assertIsNotNone(item.nutrition)


if __name__ == "__main__":
    unittest.main(verbosity=2)
