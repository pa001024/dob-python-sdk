"""纯 BD JSON parity：仅导入 BD 原文 + 原始表，全程 Python 自算，与 bun 真机比对。

输入（tests/golden/）：
- shardBuild1_bd.json ……纯 BD JSON 原文（CharSettings + charId），唯一构筑输入
- shardBuild1_data.json …最小原始表（角色/MOD/武器/BUFF/魔灵/怪物的原始条目；
  生产环境由 DataPackStore / ModuleStore 的 load_tables() 提供同形数据）

期望（shardBuild1_expr.json）：42 个表达式的真机值 + 全量 attrs/panels 快照。
attrs/面板/技能表/伤害乘区全部由引擎自算，测试不消费任何 oracle 中间量。

复现链：
    bun .tmp/dump-shardbuild-golden.ts     # 42 表达式期望 + attrs/panels 快照
    bun .tmp/extract-shardbuild-data.ts    # BD 原文 + 最小原始表
"""

import json
import math
import os
import unittest

from dna_builder_sdk.calc.build import build_state
from dna_builder_sdk.calc.engine import Engine
from dna_builder_sdk.calc.gamedata import GameDataTables

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")


def _load(name):
    with open(os.path.join(GOLDEN_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def _make_engine():
    bd = _load("shardBuild1_bd.json")
    data = _load("shardBuild1_data.json")
    tables = GameDataTables.from_dict(
        {
            "chars": data["chars"],
            "mods": data["mods"],
            "buffs": data["buffs"],
            "effects": [e for e in data["effects"] if e],
            "weapons": data["weapons"],
            "pets": data["pets"],
            "pet_entries": data["petEntries"],
            "monsters": [m for m in data["monsters"] if m],
        }
    )
    return Engine(build_state(bd["charId"], bd["settings"], tables), tables)


class TestPureBuildParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _make_engine()
        cls.expr = _load("shardBuild1_expr.json")

    def test_bd_json_shape(self):
        """输入确为纯 BD JSON：只有 CharSettings 原文字段，无 attrs/面板/技能表。"""
        bd = _load("shardBuild1_bd.json")
        self.assertEqual(bd["charId"], 1501)
        settings = bd["settings"]
        for banned in ("attrs", "weapon", "skillTables", "panels", "skillAttrs"):
            self.assertNotIn(banned, settings)
        self.assertEqual(settings["baseName"], "萨麦尔")
        self.assertEqual(len(settings["customVariables"]), 17)

    def test_expression_parity_42(self):
        matched = 0
        for case in self.expr["cases"]:
            try:
                py = self.eng.calculate(case["expr"])
            except Exception as error:  # noqa: BLE001
                self.fail(f"求值抛错: {case['expr'][:60]}: {error}")
            self.assertTrue(
                isinstance(py, float) and math.isclose(py, case["ts"], rel_tol=1e-9, abs_tol=1e-12),
                f"parity 破裂: {case['expr'][:80]}\n  ts={case['ts']!r} py={py!r}",
            )
            matched += 1
        self.assertEqual(matched, 42)
        print(f"\nparity: matched={matched}/42")

    def test_attrs_parity(self):
        """全量属性表（含 code/条件不动点后）与真机一致。"""
        mine = self.eng.calculate_weapon_attributes()
        ref = self.expr["expectedAttrs"]
        self.assertEqual(set(mine.keys()) - {"weapon"}, set(ref.keys()) - {"weapon"})
        for key, want in ref.items():
            if key == "weapon" or not isinstance(want, (int, float)):
                continue
            got = mine.get(key)
            self.assertTrue(
                isinstance(got, (int, float)) and math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-12),
                f"attrs.{key}: mine={got!r} ref={want!r}",
            )

    def test_panels_parity(self):
        """各槽位武器面板与真机一致（含选中武器 code 修正回填）。"""
        current = self.eng.calculate_weapon_attributes()
        mine = self.eng.context_panels(current)
        for key, want_panel in self.expr["expectedPanels"].items():
            got_panel = mine.get(key) or {}
            for attr, want in (want_panel or {}).items():
                got = got_panel.get(attr)
                self.assertTrue(
                    isinstance(got, (int, float)) and math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-12),
                    f"panel.{key}.{attr}: mine={got!r} ref={want!r}",
                )


if __name__ == "__main__":
    unittest.main()
