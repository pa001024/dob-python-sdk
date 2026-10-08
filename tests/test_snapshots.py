"""在线快照用例（无网：fake 后端 + fixture 原始表）。"""

import json
import math
import os
import unittest

from dna_builder_sdk.calc.gamedata import GameDataTables
from dna_builder_sdk.errors import DobApiError
from dna_builder_sdk.snapshots import BuildSnapshot, snapshot_from_online, snapshot_from_settings

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


class FakeBackend:
    def __init__(self, builds):
        self.builds = builds

    def build(self, build_id):
        return self.builds.get(build_id)


class TestSnapshots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = _tables()
        cls.bd = _load("shardBuild1_bd.json")
        cls.expr = _load("shardBuild1_expr.json")

    def _fake(self, settings=None):
        return FakeBackend(
            {
                "demo": {
                    "id": "demo",
                    "title": "演示构筑",
                    "charId": self.bd["charId"],
                    "charSettings": json.dumps(settings if settings is not None else self.bd["settings"], ensure_ascii=False),
                }
            }
        )

    def test_snapshot_from_online(self):
        """拉线上 JSON（fake）→ 快照可求值/看属性，与真机期望一致。"""
        snapshot = snapshot_from_online("demo", backend=self._fake(), source=self.tables)
        self.assertIsInstance(snapshot, BuildSnapshot)
        self.assertEqual(snapshot.bdid, "demo")
        self.assertEqual(snapshot.title, "演示构筑")
        target = next(c["ts"] for c in self.expr["cases"] if c["expr"] == self.bd["settings"]["targetFunction"])
        self.assertTrue(math.isclose(snapshot.calculate(), target, rel_tol=1e-9, abs_tol=1e-12))
        self.assertEqual(snapshot.attrs.get("攻击"), self.expr["expectedAttrs"]["攻击"])
        self.assertEqual(snapshot.evaluate("10 + 4 * 5"), 30)
        panel = snapshot.char_panel()
        self.assertNotIn("weapon", panel)
        self.assertIn("攻击", panel)
        self.assertEqual(len(snapshot.skill_levels_final()), 5)
        summary = snapshot.summary()
        self.assertEqual(summary["bdid"], "demo")
        self.assertTrue(math.isclose(summary["targetValue"], target, rel_tol=1e-9))

    def test_snapshot_from_settings(self):
        """本地设置直装快照（不联网），附 trio/traces 参数。"""
        snapshot = snapshot_from_settings(
            self.bd["charId"], self.bd["settings"], self.tables, skill_levels=[10, 10, 10], traces=7
        )
        by_name = dict(snapshot.skill_levels_final())
        self.assertEqual(by_name["以坚忍之名"], 12)
        self.assertIsNotNone(snapshot.weapon_panel("melee"))
        self.assertIsNone(snapshot.weapon_panel("不存在"))
        self.assertEqual(snapshot.skill_fields("不存在的技能"), [])

    def test_missing_build(self):
        with self.assertRaises(DobApiError):
            snapshot_from_online("nope", backend=FakeBackend({}), source=self.tables)

    def test_bad_settings(self):
        backend = FakeBackend({"bad": {"id": "bad", "charId": 1501, "charSettings": "{oops"}})
        with self.assertRaises(DobApiError):
            snapshot_from_online("bad", backend=backend, source=self.tables)


if __name__ == "__main__":
    unittest.main()
