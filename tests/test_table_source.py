"""数据源抽象接口用例（纯本地，无网络）。

覆盖：
- ModuleStore 声明时定模块列表（modules 子集 / mapping 全量映射 / 非法表名）
- DataPackStore / ModuleStore 均实现 TableSource.load_tables()（给 Engine 用）
- snapshot_from_online 经 source 抽象接入 Engine 通路
"""

import json
import os
import unittest

from dna_builder_sdk import DataPackStore, ModuleStore
from dna_builder_sdk.calc.build import build_state
from dna_builder_sdk.calc.engine import Engine
from dna_builder_sdk.calc.gamedata import DATASET_DEFAULTS, GameDataTables, resolve_tables
from dna_builder_sdk.snapshots import snapshot_from_online

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")


def _load(name):
    with open(os.path.join(GOLDEN_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def _fixture_tables():
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


class FakeGameData:
    """按 dataset 回放 fixture 切片（key/data 信封与服务端同形）。"""

    VALUES = None

    def __init__(self):
        if FakeGameData.VALUES is None:
            data = _load("shardBuild1_data.json")
            FakeGameData.VALUES = {
                "char": data["chars"],
                "mod": data["mods"],
                "buff": data["buffs"],
                "effect": [e for e in data["effects"] if e],
                "weapon": data["weapons"],
                "pet": data["pets"],
                "pet:petEntrys": data["petEntries"],
                "monster": [m for m in data["monsters"] if m],
            }

    def iter_all(self, dataset, page_size=500, **kwargs):
        values = FakeGameData.VALUES.get(dataset, [])
        yield {"total": len(values), "items": [{"key": str(i), "data": v} for i, v in enumerate(values)]}

    def record(self, dataset, key):
        return None


class FakeBackend:
    def __init__(self, builds):
        self.builds = builds

    def build(self, build_id):
        return self.builds.get(build_id)


class TestModuleDeclaration(unittest.TestCase):
    def test_default_is_full_mapping(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = ModuleStore(FakeGameData(), cache_dir=tmp)
            self.assertEqual(store.mapping, DATASET_DEFAULTS)
            self.assertEqual(store.tables, list(DATASET_DEFAULTS))

    def test_modules_subset(self):
        store = ModuleStore(FakeGameData(), cache_dir=os.path.join(os.getcwd(), ".tmp", "t"), modules=["chars", "mods"])
        self.assertEqual(store.mapping, {"chars": "char", "mods": "mod"})
        self.assertEqual(store.datasets, ["char", "mod"])

    def test_unknown_module_rejected(self):
        with self.assertRaises(ValueError):
            ModuleStore(FakeGameData(), modules=["nope"])

    def test_custom_mapping(self):
        store = ModuleStore(FakeGameData(), mapping={"chars": "char"})
        self.assertEqual(store.mapping, {"chars": "char"})


class TestTableSource(unittest.TestCase):
    def test_module_store_load_tables_for_engine(self):
        """ModuleStore.load_tables() → GameDataTables 可直接给 Engine 用。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = ModuleStore(FakeGameData(), cache_dir=tmp)
            tables = store.load_tables()
            self.assertIsInstance(tables, GameDataTables)
            self.assertTrue(len(tables.chars) > 0 and len(tables.mods) > 0)
            # 与 fixture 表同构：同一 BD 可装配 Engine 并求值
            bd = _load("shardBuild1_bd.json")
            engine = Engine(build_state(bd["charId"], bd["settings"], tables), tables)
            self.assertIsInstance(engine.calculate(), float)

    def test_module_store_subset_load_tables(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = ModuleStore(FakeGameData(), cache_dir=tmp, modules=["chars", "mods"])
            tables = store.load_tables()
            self.assertTrue(len(tables.chars) > 0 and len(tables.mods) > 0)
            self.assertEqual(tables.weapons, [])

    def test_load_tables_respects_declared_mapping(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = ModuleStore(FakeGameData(), cache_dir=tmp, modules=["chars"])
            tables = store.load_tables()
            self.assertTrue(len(tables.chars) > 0)
            self.assertEqual(tables.mods, [])

    def test_data_pack_load_tables(self):
        """DataPackStore.load_tables() 同接口（用内存模块桩，不碰网络）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pack = DataPackStore(cache_dir=tmp)
            pack._active_version = "stub"
            pack._module_cache = {
            "char.data": {"default": [{"id": 1, "名称": "a"}]},
            "mod.data": {"default": []},
            "buff.data": {"default": []},
            "effect.data": {"default": []},
            "weapon.data": {"default": []},
            "pet.data": {"petData": [], "petEntrys": []},
            "monster.data": {"default": []},
        }
            tables = pack.load_tables()
            self.assertEqual(len(tables.chars), 1)

    def test_resolve_tables(self):
        tables = _fixture_tables()
        self.assertIs(resolve_tables(tables), tables)

        class Src:
            def __init__(self, t):
                self._t = t

            def load_tables(self):
                return self._t

        self.assertIs(resolve_tables(Src(tables)), tables)
        with self.assertRaises(TypeError):
            resolve_tables(object())


class TestSnapshotSource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = _fixture_tables()
        cls.bd = _load("shardBuild1_bd.json")
        cls.expr = _load("shardBuild1_expr.json")

    def _fake(self):
        return FakeBackend(
            {
                "demo": {
                    "id": "demo",
                    "title": "演示构筑",
                    "charId": self.bd["charId"],
                    "charSettings": json.dumps(self.bd["settings"], ensure_ascii=False),
                }
            }
        )

    def test_snapshot_from_source(self):
        """source= 表源经 Engine 通路装配快照。"""

        class Src:
            def __init__(self, t):
                self._t = t

            def load_tables(self):
                return self._t

        snapshot = snapshot_from_online("demo", backend=self._fake(), source=Src(self.tables))
        target = next(c["ts"] for c in self.expr["cases"] if c["expr"] == self.bd["settings"]["targetFunction"])
        import math

        self.assertTrue(math.isclose(snapshot.calculate(), target, rel_tol=1e-9, abs_tol=1e-12))

    def test_snapshot_source_tables_object(self):
        snapshot = snapshot_from_online("demo", backend=self._fake(), source=self.tables)
        self.assertEqual(snapshot.bdid, "demo")


if __name__ == "__main__":
    unittest.main()
