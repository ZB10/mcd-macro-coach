"""营养数据加载与餐品名对齐。

麦当劳 MCP 里存在两套彼此独立的索引体系，这是本项目最主要的技术障碍：

* 菜单（``query-meals`` / ``query-meal-detail``）以 ``productCode`` 索引，
  名称是面向顾客的营销名，例如 ``麦咖啡™热美式中杯``、``纯牛奶(盒装)``。
* 营养（``list-nutrition-foods``）以中文品名索引，名称是标准化品名，
  例如 ``热美式中杯``、``纯牛奶（盒装）``。

两者既不同源也不同步：括号有全半角差异、品牌前缀时有时无、
规格词（中杯/大杯）在菜单侧会被省略。若直接把两边按名字 join，
命中率会低到无法使用；若强行模糊匹配，又会把「优品豆浆」错配成
「优品豆浆大杯」，导致热量算错 50 大卡——这比算不出来更糟。

因此本模块的原则是：**宁可返回 None，也不返回一个猜的**。
匹配按可信度分四级，低可信度结果会被显式标注，由上层决定是否采信。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "nutrition_raw.tsv"

# 全角 -> 半角。中英文括号混用是菜单侧最常见的命名差异来源。
_WIDTH_MAP = str.maketrans("（）［］｛｝，、：；　", "()[]{},,:; ")

# 品牌前缀。菜单侧会加「麦咖啡™」前缀，营养侧通常不加。
_BRAND_PREFIXES = ("麦咖啡", "麦咖啡™", "mccafe", "麦咖啡mcafe")

# 人工别名表：菜单侧名称 -> 营养侧标准名。
# 只收录经过实际调用核对过的映射，不做任何推测性填充。
_ALIASES = {
    "纯牛奶": "纯牛奶（盒装）",
    "纯牛奶(盒装)": "纯牛奶（盒装）",
    "100%苹果汁(盒装)": "100%苹果汁",
    "100% 苹果汁(盒装)": "100%苹果汁",
    "经典麦旋风": "奥利奥麦旋风",
    "新地": "朱古力新地",
    "灰焰圆筒": "圆筒冰淇淋",
}

# 菜单侧省略杯型时的默认规格。
# 麦当劳点单默认中杯，所以「可乐」在菜单上不带规格，实际对应「可乐中杯」。
# 只在「名称+中杯」确实存在于营养表时才启用，否则宁可返回未知。
_DEFAULT_SIZE = "中杯"

# 模糊匹配的接受阈值。0.90 意味着允许「热美式中杯」≈「冰美式中杯」这类
# 只差一个字的情况——但我们不希望在冷热之间猜错，所以再加一道长度校验。
_FUZZY_THRESHOLD = 0.90


@dataclass(frozen=True)
class Nutrition:
    """一份餐品的营养构成。热量单位 kcal，其余为克，钠为毫克。"""

    kcal: float
    protein: float
    fat: float
    carb: float
    sodium: float
    calcium: float

    def __add__(self, other: "Nutrition") -> "Nutrition":
        return Nutrition(
            kcal=self.kcal + other.kcal,
            protein=self.protein + other.protein,
            fat=self.fat + other.fat,
            carb=self.carb + other.carb,
            sodium=self.sodium + other.sodium,
            calcium=self.calcium + other.calcium,
        )

    @classmethod
    def zero(cls) -> "Nutrition":
        return cls(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def as_dict(self) -> dict[str, float]:
        return {
            "kcal": round(self.kcal, 1),
            "protein": round(self.protein, 1),
            "fat": round(self.fat, 1),
            "carb": round(self.carb, 1),
            "sodium": round(self.sodium, 1),
            "calcium": round(self.calcium, 1),
        }


@dataclass(frozen=True)
class Match:
    """一次名称匹配的结果。``confidence`` 为 exact / alias / fuzzy / none。"""

    nutrition: Nutrition | None
    matched_name: str | None
    confidence: str
    score: float

    @property
    def ok(self) -> bool:
        return self.nutrition is not None


def normalize(name: str) -> str:
    """把餐品名压成可比较的形式：去空白、全角转半角、去商标符号、统一大小写。"""
    if not name:
        return ""
    text = name.translate(_WIDTH_MAP)
    for symbol in ("™", "®", "©"):
        text = text.replace(symbol, "")
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    text = re.sub(r"\s+", "", text)
    return text.lower()


def _strip_brand_prefix(norm_name: str) -> str:
    for prefix in _BRAND_PREFIXES:
        p = normalize(prefix)
        if p and norm_name.startswith(p):
            return norm_name[len(p):]
    return norm_name


class NutritionTable:
    """营养表。一次性载入 TSV，之后按名称查询。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DATA_PATH
        self._by_norm: dict[str, tuple[str, Nutrition]] = {}
        self._alias_norm: dict[str, str] = {
            normalize(alias): target for alias, target in _ALIASES.items()
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"营养数据文件不存在: {self.path}")

        # 用 utf-8-sig：数据文件可能被 Excel / Windows 编辑器存成带 BOM 的
        # UTF-8，BOM 会粘在第一个字段名上导致表头校验失败。
        with self.path.open(encoding="utf-8-sig") as fh:
            header = fh.readline().rstrip("\n").split("\t")
            expected = ["name", "kj", "kcal", "protein", "fat", "carb", "sodium", "calcium"]
            if header != expected:
                raise ValueError(f"营养数据表头不符: {header}")

            for lineno, line in enumerate(fh, start=2):
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) != 8:
                    raise ValueError(f"第 {lineno} 行字段数为 {len(parts)}，应为 8")
                name = parts[0].strip()
                try:
                    nutrition = Nutrition(
                        kcal=float(parts[2]),
                        protein=float(parts[3]),
                        fat=float(parts[4]),
                        carb=float(parts[5]),
                        sodium=float(parts[6]),
                        calcium=float(parts[7]),
                    )
                except ValueError as exc:
                    raise ValueError(f"第 {lineno} 行数值解析失败: {line}") from exc
                self._by_norm[normalize(name)] = (name, nutrition)

    def __len__(self) -> int:
        return len(self._by_norm)

    def lookup(self, name: str) -> Match:
        """把菜单侧名称对齐到营养侧。

        按 exact -> alias -> 去品牌前缀 -> 模糊 的顺序逐级降级，
        任一级命中即返回；全部失败返回 confidence="none" 的空结果。
        """
        if not name:
            return Match(None, None, "none", 0.0)

        norm = normalize(name)

        # 1. 精确命中
        if norm in self._by_norm:
            canonical, nutrition = self._by_norm[norm]
            return Match(nutrition, canonical, "exact", 1.0)

        # 2. 人工别名
        alias_target = self._alias_norm.get(norm)
        if alias_target:
            target_norm = normalize(alias_target)
            if target_norm in self._by_norm:
                canonical, nutrition = self._by_norm[target_norm]
                return Match(nutrition, canonical, "alias", 0.99)

        # 3. 去掉品牌前缀后再试精确
        stripped = _strip_brand_prefix(norm)
        if stripped != norm and stripped in self._by_norm:
            canonical, nutrition = self._by_norm[stripped]
            return Match(nutrition, canonical, "exact", 0.98)

        # 4. 补默认杯型。菜单上「可乐」「雪碧」「卡布奇诺」都不带规格，
        #    而营养表按中杯/大杯/小杯分列。只有「名称+中杯」存在时才采信。
        sized = stripped + _DEFAULT_SIZE
        if sized in self._by_norm:
            canonical, nutrition = self._by_norm[sized]
            return Match(nutrition, canonical, "spec-default", 0.95)

        # 5. 模糊匹配。长度差超过 2 个字符直接放弃，
        #    避免「优品豆浆」被吃成「优品豆浆大杯」这类规格错配。
        best: tuple[float, str, Nutrition] | None = None
        for candidate_norm, (canonical, nutrition) in self._by_norm.items():
            if abs(len(candidate_norm) - len(stripped)) > 2:
                continue
            ratio = SequenceMatcher(None, stripped, candidate_norm).ratio()
            if ratio >= _FUZZY_THRESHOLD and (best is None or ratio > best[0]):
                best = (ratio, canonical, nutrition)

        if best:
            return Match(best[2], best[1], "fuzzy", best[0])

        return Match(None, None, "none", 0.0)

    def all_names(self) -> list[str]:
        return sorted(name for name, _ in self._by_norm.values())
