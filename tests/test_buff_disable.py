"""BUFF × MOD 互斥（数据 `禁用`）：装备命中的 MOD 后 BUFF 不参与计算。

对齐 TS `CharBuild.isBuffDisabled`：online_data.json 是旧快照（无 `禁用`），测试内注入字段。
"""

import json
import os
import unittest

from dna_builder_sdk.calc.build import build_state
from dna_builder_sdk.calc.engine import Engine
from dna_builder_sdk.calc.gamedata import GameDataTables

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _tables():
    data = _load(os.path.join(GOLDEN_DIR, "online_data.json"))
    # 巨鲸「激扬寒波」与 BUFF 同增益（固定攻击 +275）
    for buff in data["buffs"]:
        if buff.get("名称") == "激扬寒波":
            buff["禁用"] = [51922]
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


def _settings(buffs, char_mods):
    slots = [None] * 8
    if char_mods:
        slots[0] = char_mods
    return {
        "charLevel": 80,
        "charSkillLevel": [10, 10, 10],
        "meleeWeapon": 0,
        "rangedWeapon": 0,
        "auraMod": 0,
        "buffs": buffs,
        "charMods": slots,
        "targetFunction": "伤害",
    }


class BuffDisableTest(unittest.TestCase):
    def setUp(self):
        self.tables = _tables()

    def _state(self, buffs, char_mods):
        return build_state(2201, _settings(buffs, char_mods), self.tables)

    @staticmethod
    def _names(state):
        return {buff.get("名称") for buff in state["buffs"]}

    def test_disabled_buff_filtered(self):
        buff_only = self._state([["激扬寒波", 10]], None)
        self.assertIn("激扬寒波", self._names(buff_only))

        both = self._state([["激扬寒波", 10]], [51922, 10])
        self.assertNotIn("激扬寒波", self._names(both))

        mod_only = self._state([], [51922, 10])
        attrs_both = Engine(both, self.tables).calculate_attributes()
        attrs_mod_only = Engine(mod_only, self.tables).calculate_attributes()
        self.assertAlmostEqual(
            attrs_both.get("攻击", 0.0), attrs_mod_only.get("攻击", 0.0), places=9
        )
        attrs_buff_only = Engine(buff_only, self.tables).calculate_attributes()
        self.assertGreater(
            attrs_buff_only.get("攻击", 0.0), attrs_mod_only.get("攻击", 0.0)
        )


if __name__ == "__main__":
    unittest.main()
