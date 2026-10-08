"""技能等级 trio/溯源 + 面板 API 用例（纯本地；point2 由真机导出）。"""

import json
import math
import os
import unittest

from dna_builder_sdk.calc.build import build_state, parse_trace_levels
from dna_builder_sdk.calc.engine import Engine
from dna_builder_sdk.calc.gamedata import GameDataTables

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")


def _load(name):
    with open(os.path.join(GOLDEN_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def _tables():
    data = _load("shardBuild1_data.json")
    return GameDataTables.from_dict(
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


class TestSkillLevels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = _tables()
        cls.bd = _load("shardBuild1_bd.json")
        char_raw = cls.tables.char_by_id[cls.bd["charId"]]
        cls.traces = list(char_raw.get("溯源") or [])

    def test_parse_trace_bonus(self):
        """溯源等级解析：7 含 1-7（E+2、Q+2、P 各 +1）；5 恰含 3/5 溯；越界钳制。"""
        bonus = parse_trace_levels(self.traces, 7)
        self.assertEqual(bonus.get("以坚忍之名"), 2)
        self.assertEqual(bonus.get("我不忍啦！"), 2)
        self.assertEqual(bonus.get("哼！！"), 2)
        self.assertEqual(parse_trace_levels(self.traces, 5), bonus)
        self.assertEqual(parse_trace_levels(self.traces, 2), {})
        self.assertEqual(parse_trace_levels(self.traces, 0), {})
        self.assertEqual(parse_trace_levels(self.traces, 99), bonus)
        self.assertEqual(parse_trace_levels(self.traces, -3), {})

    def test_final_levels_trio_traces(self):
        """trio [10,10,10] + 溯源 7 → E/Q/P 全 12。"""
        engine = Engine(
            build_state(self.bd["charId"], self.bd["settings"], self.tables, skill_levels=[10, 10, 10], traces=7),
            self.tables,
        )
        finals = engine.skill_levels_final()
        by_name = dict(finals)
        self.assertEqual(by_name["以坚忍之名"], 12)
        self.assertEqual(by_name["我不忍啦！"], 12)
        self.assertEqual(by_name["哼！！"], 12)

    def test_final_levels_clamp(self):
        """11 级 base + 双溯叠加钳制在 12（13→12），未命中技能保持原值。"""
        engine = Engine(
            build_state(self.bd["charId"], self.bd["settings"], self.tables, skill_levels=[11, 9, 8], traces=7),
            self.tables,
        )
        by_name = dict(engine.skill_levels_final())
        self.assertEqual(by_name["以坚忍之名"], 12)  # 11+2 钳制
        self.assertEqual(by_name["我不忍啦！"], 11)  # 9+2
        self.assertEqual(by_name["哼！！"], 10)  # 8+1+1

    def test_default_matches_ts_single_level(self):
        """缺省 trio/traces 与 TS 单一 charSkillLevel 口径一致（12 级全技能）。"""
        engine = Engine(build_state(self.bd["charId"], self.bd["settings"], self.tables), self.tables)
        for _, level in engine.skill_levels_final()[:3]:
            self.assertEqual(level, 12)


class TestPanels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = _tables()
        cls.bd = _load("shardBuild1_bd.json")

    def test_char_panel(self):
        """角色面板 = 全量属性去 weapon 键；70 级点位与真机一致。"""
        point2 = _load("point2.json")
        settings = dict(self.bd["settings"], charLevel=70, charSkillLevel=10)
        engine = Engine(build_state(self.bd["charId"], settings, self.tables), self.tables)
        panel = engine.char_panel()
        self.assertNotIn("weapon", panel)
        for key, want in point2["attrs"].items():
            if key == "weapon" or not isinstance(want, (int, float)):
                continue
            got = panel.get(key)
            self.assertTrue(
                isinstance(got, (int, float)) and math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-12),
                f"char_panel.{key}: mine={got!r} ref={want!r}",
            )

    def test_weapon_panel(self):
        """武器面板与上下文面板同源；未知槽位返回 None。"""
        engine = Engine(build_state(self.bd["charId"], self.bd["settings"], self.tables), self.tables)
        attrs = engine.calculate_weapon_attributes()
        standalone = engine.weapon_panels()
        for slot, key in (("melee", "近战"), ("ranged", "远程"), ("skill", "同律")):
            self.assertEqual(engine.weapon_panel(slot), standalone[key])
        self.assertIsNone(engine.weapon_panel("不存在的槽位"))

    def test_skill_fields(self):
        """技能字段面板：最终等级下的结算字段；未知技能为空。"""
        engine = Engine(
            build_state(self.bd["charId"], self.bd["settings"], self.tables, skill_levels=[10, 10, 10], traces=7),
            self.tables,
        )
        fields = engine.skill_fields("以坚忍之名")
        self.assertTrue(len(fields) > 0)
        names = [f.get("名称") for f in fields]
        self.assertIn("伤害", names)
        damage = next(f for f in fields if f.get("名称") == "伤害")
        self.assertIsInstance(damage.get("值"), (int, float))
        self.assertEqual(engine.skill_fields("不存在的技能"), [])

    def test_point2_target(self):
        """第二验证点目标值（70 级/技能 10 级）与真机一致。"""
        point2 = _load("point2.json")
        settings = dict(self.bd["settings"], charLevel=70, charSkillLevel=10)
        engine = Engine(build_state(self.bd["charId"], settings, self.tables), self.tables)
        got = engine.calculate()
        self.assertTrue(math.isclose(got, point2["target"], rel_tol=1e-9, abs_tol=1e-12), f"mine={got!r} ref={point2['target']!r}")


if __name__ == "__main__":
    unittest.main()
