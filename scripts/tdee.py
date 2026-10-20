"""能量需求推算与单餐营养目标分配。

计算链：身体数据 -> BMR（Mifflin-St Jeor）-> TDEE -> 目标热量 -> 单餐目标。

Mifflin-St Jeor 是当前临床与运动营养领域最常用的 BMR 公式，
比 Harris-Benedict 在超重人群上偏差更小。公式本身是公开的，
这里只是把它和「麦当劳一顿饭」这个具体场景接起来。

需要注意：本模块产出的是**估算值**，不是医学建议。
所有输出都会带 disclaimer。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 活动系数。取值参考 FAO/WHO 与常见运动营养实践。
ACTIVITY_LEVELS = {
    "sedentary": (1.2, "久坐，几乎不运动"),
    "light": (1.375, "轻度活动，每周 1-3 次运动"),
    "moderate": (1.55, "中度活动，每周 3-5 次运动"),
    "active": (1.725, "高强度，每周 6-7 次运动"),
    "athlete": (1.9, "运动员或体力劳动"),
}

# 目标 -> (热量系数, 每公斤体重蛋白需求克数)
GOALS = {
    "cut": (0.80, 2.0, "减脂"),
    "maintain": (1.00, 1.6, "维持"),
    "bulk": (1.10, 1.8, "增肌"),
}

# 餐段热量分配比例。合计 1.0。
MEAL_SPLIT = {
    "breakfast": (0.25, "早餐"),
    "lunch": (0.35, "午餐"),
    "dinner": (0.30, "晚餐"),
    "snack": (0.10, "加餐"),
}

# 中国居民膳食指南建议每日钠摄入不超过 2000mg。
DAILY_SODIUM_LIMIT = 2000.0


@dataclass
class Profile:
    """用户身体数据与目标。"""

    weight_kg: float
    height_cm: float
    age: int
    sex: str  # "male" | "female"
    activity: str = "light"
    goal: str = "maintain"

    def validate(self) -> None:
        if not 25 <= self.weight_kg <= 300:
            raise ValueError("体重需在 25-300kg 之间")
        if not 100 <= self.height_cm <= 250:
            raise ValueError("身高需在 100-250cm 之间")
        if not 10 <= self.age <= 100:
            raise ValueError("年龄需在 10-100 岁之间")
        if self.sex not in ("male", "female"):
            raise ValueError("sex 只能是 male 或 female")
        if self.activity not in ACTIVITY_LEVELS:
            raise ValueError(f"activity 只能是 {list(ACTIVITY_LEVELS)}")
        if self.goal not in GOALS:
            raise ValueError(f"goal 只能是 {list(GOALS)}")


@dataclass
class Targets:
    """单餐营养目标。"""

    bmr: float
    tdee: float
    daily_kcal: float
    meal_kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    sodium_mg: float
    meal_label: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "bmr": round(self.bmr),
            "tdee": round(self.tdee),
            "daily_kcal": round(self.daily_kcal),
            "meal_kcal": round(self.meal_kcal),
            "protein_g": round(self.protein_g, 1),
            "fat_g": round(self.fat_g, 1),
            "carb_g": round(self.carb_g, 1),
            "sodium_mg": round(self.sodium_mg),
            "meal_label": self.meal_label,
        }


def basal_metabolic_rate(profile: Profile) -> float:
    """Mifflin-St Jeor 基础代谢率（kcal/天）。"""
    base = 10 * profile.weight_kg + 6.25 * profile.height_cm - 5 * profile.age
    return base + 5 if profile.sex == "male" else base - 161


def total_daily_energy(profile: Profile) -> float:
    factor, _ = ACTIVITY_LEVELS[profile.activity]
    return basal_metabolic_rate(profile) * factor


def compute_targets(profile: Profile, meal: str = "lunch") -> Targets:
    """推算某一餐的营养目标。

    脂肪按总热量的 30% 反推，碳水吃掉剩余热量，这是最不容易出错的
    分配方式——先锁蛋白和脂肪，碳水做配平。
    """
    profile.validate()
    if meal not in MEAL_SPLIT:
        raise ValueError(f"meal 只能是 {list(MEAL_SPLIT)}")

    bmr = basal_metabolic_rate(profile)
    tdee = total_daily_energy(profile)
    kcal_factor, protein_per_kg, _ = GOALS[profile.goal]
    daily_kcal = tdee * kcal_factor

    ratio, label = MEAL_SPLIT[meal]
    meal_kcal = daily_kcal * ratio

    # 蛋白按体重算的是**全天**需求，单餐按热量占比切分。
    daily_protein = profile.weight_kg * protein_per_kg
    protein_g = daily_protein * ratio

    # 脂肪锁 30% 热量，碳水配平。
    fat_g = meal_kcal * 0.30 / 9
    carb_kcal = meal_kcal - protein_g * 4 - fat_g * 9
    carb_g = max(carb_kcal / 4, 0)

    notes: list[str] = []
    if profile.goal == "cut" and daily_kcal < bmr:
        notes.append(
            f"目标热量 {daily_kcal:.0f}kcal 已低于基础代谢 {bmr:.0f}kcal，"
            "长期如此不利于保持肌肉，建议提高预算热量或增加活动量。"
        )
    if carb_g < 20:
        notes.append("碳水目标偏低，可能是蛋白目标过高，请复核。")

    return Targets(
        bmr=bmr,
        tdee=tdee,
        daily_kcal=daily_kcal,
        meal_kcal=meal_kcal,
        protein_g=protein_g,
        fat_g=fat_g,
        carb_g=carb_g,
        sodium_mg=DAILY_SODIUM_LIMIT * ratio,
        meal_label=label,
        notes=notes,
    )
