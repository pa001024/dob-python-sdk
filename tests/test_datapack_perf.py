"""真实数据包加载性能基线（纯本地，无网络）。

输入：仓库 `mock/data-pack/<PIN_VERSION>.zip`（钉住版本，条数即 checksum 锚点）。
DataPackStore 的磁盘布局是 `<cache>/<version>/package.zip`，而 mock 下是扁平
`<version>.zip`，setUpClass 用硬链（同盘秒级，不占额外空间；失败回退拷贝）
搭出布局——搭布局属 setup，不计入被测耗时。

方法（按 perf-hotspot-profiling 技能）：正确性锚点先行（钉住 8 表条数），
耗时取 best（min，多轮最稳），打印实测值供后人对比；预算只防病态劣化
（约实测 100 倍+），不做机器相关的精密门控。
"""

import os
import shutil
import tempfile
import time
import unittest

from dna_builder_sdk import DataPackStore

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(TESTS_DIR)))
MOCK_DIR = os.path.join(REPO_ROOT, "mock", "data-pack")

# 钉住的数据包版本（条数锚点与之绑定；换版需同步更新 PIN_COUNTS）
PIN_VERSION = "1.6.208.6"
PIN_COUNTS = {
    "chars": 33,
    "mods": 586,
    "buffs": 187,
    "effects": 65,
    "weapons": 71,
    "pets": 152,
    "pet_entries": 73,
    "monsters": 393,
}

# 病态预算（实测 activate ~0.2s / load_tables ~0.05s，预算取百倍以上只防挂死）
ACTIVATE_BUDGET = 60.0
LOAD_TABLES_BUDGET = 60.0
ROUNDS = 3


def _best(seconds_list: list[float]) -> float:
    return min(seconds_list)


class TestDatapackPerf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = os.path.join(MOCK_DIR, f"{PIN_VERSION}.zip")
        if not os.path.isfile(src):
            raise unittest.SkipTest(f"缺真实数据包: {src}")
        cls.tmp = tempfile.mkdtemp(prefix="dob-pack-perf-")
        staged = os.path.join(cls.tmp, PIN_VERSION, "package.zip")
        os.makedirs(os.path.dirname(staged))
        try:
            os.link(src, staged)  # 同盘硬链：秒级，不占额外空间
        except OSError:
            shutil.copy(src, staged)  # 跨盘回退：慢但只发生在 setup，不计入基线
        cls.cache_dir = cls.tmp

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _cold_pack(self) -> DataPackStore:
        return DataPackStore(cache_dir=self.cache_dir)

    def test_activate_baseline(self):
        """activate（manifest 解析 + 全包 zip 校验）基线。"""
        samples = []
        manifest = None
        for _ in range(ROUNDS):
            pack = self._cold_pack()
            start = time.perf_counter()
            manifest = pack.activate(PIN_VERSION)
            samples.append(time.perf_counter() - start)
        best = _best(samples)
        print(f"\n[perf] datapack activate({PIN_VERSION}) best-of-{ROUNDS}: {best * 1000:.1f}ms")
        self.assertEqual(manifest.get("version"), PIN_VERSION)
        self.assertLess(best, ACTIVATE_BUDGET, f"activate 基线击穿: {best:.2f}s")

    def test_load_tables_baseline(self):
        """全量 8 表加载（msgpack 解码 + 索引装配）基线，条数即正确性锚点。"""
        samples = []
        tables = None
        for _ in range(ROUNDS):
            pack = self._cold_pack()
            pack.activate(PIN_VERSION)
            start = time.perf_counter()
            tables = pack.load_tables()
            samples.append(time.perf_counter() - start)
        best = _best(samples)
        counts = {name: len(getattr(tables, name)) for name in PIN_COUNTS}
        print(f"\n[perf] datapack load_tables({PIN_VERSION}) best-of-{ROUNDS}: {best * 1000:.1f}ms counts={counts}")
        self.assertEqual(counts, PIN_COUNTS)
        # 派生索引与条数一致（装配没丢东西）
        self.assertEqual(len(tables.mod_by_id), counts["mods"])
        self.assertEqual(len(tables.weapon_by_id), counts["weapons"])
        self.assertLess(best, LOAD_TABLES_BUDGET, f"load_tables 基线击穿: {best:.2f}s")


if __name__ == "__main__":
    unittest.main()
