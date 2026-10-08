"""fixture BD 数值批量计算性能基线（纯本地，无网络）。

输入：tests/golden/ 真实 BD 原文 + 联合原始表。
- online/ 6 个线上 BD × online_data.json：build_state + Engine 装配 + 目标值 +
  全量属性一次批量（面板/配装助手线上链路的代表负载）；
- shardBuild1：42 表达式 + 全量属性（与 bun 真机逐位一致的 parity 负载）。

方法（按 perf-hotspot-profiling 技能）：正确性锚点先行——每轮都对期望断言
（online 走 online_expected.json，DOT 分流跳过规则与 test_online_targets 一致；
shard 走 shardBuild1_expr.json），耗时取 best（min），打印供后人对比；
预算只防病态劣化（约实测百倍），不做机器相关的精密门控。
"""

import json
import math
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_online_targets import ONLINE_DIR, _dot_dependent, _load as _online_load

from dna_builder_sdk.calc.build import build_state
from dna_builder_sdk.calc.engine import Engine
from dna_builder_sdk.calc.gamedata import GameDataTables

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")

# 病态预算（实测 online 六连批 ~0.03s / shard 42 式 ~0.08s，预算取百倍以上只防挂死）
ONLINE_BATCH_BUDGET = 5.0
SHARD_BATCH_BUDGET = 10.0
ONLINE_ROUNDS = 5
SHARD_ROUNDS = 3


def _best(samples: list[float]) -> float:
    return min(samples)


def _online_tables() -> GameDataTables:
    data = _online_load(os.path.join(GOLDEN_DIR, "online_data.json"))
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


def _shard_tables() -> GameDataTables:
    with open(os.path.join(GOLDEN_DIR, "shardBuild1_data.json"), encoding="utf-8") as fh:
        data = json.load(fh)
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


class TestOnlineBatchPerf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = _online_tables()
        cls.expected = _online_load(os.path.join(GOLDEN_DIR, "online_expected.json"))["expected"]
        cls.files = sorted(f for f in os.listdir(ONLINE_DIR) if f.endswith(".json"))
        assert cls.files, "online 靶点缺失"
        cls.payloads = [(f, _online_load(os.path.join(ONLINE_DIR, f))) for f in cls.files]
        cls.skipped = {f: _dot_dependent(p["settings"]) for f, p in cls.payloads}

    def _batch_once(self) -> None:
        """六 BD 一批：装配 + 目标值 + 全量属性，每值相对期望断言（DOT 分流跳过）。"""
        for fname, payload in self.payloads:
            key = fname.replace(".json", "")
            want = self.expected[key]
            engine = Engine(build_state(payload["charId"], payload["settings"], self.tables), self.tables)
            skipped = self.skipped[fname]
            if payload["settings"].get("targetFunction") not in skipped:
                got_target = engine.calculate()
                self.assertTrue(
                    isinstance(got_target, float)
                    and math.isclose(got_target, want["target"], rel_tol=1e-9, abs_tol=1e-12),
                    f"{fname} target: mine={got_target!r} ref={want['target']!r}",
                )
            got_attrs = engine.calculate_weapon_attributes()
            for attr, want_value in want["attrs"].items():
                if attr == "weapon" or not isinstance(want_value, (int, float)):
                    continue
                got = got_attrs.get(attr)
                self.assertTrue(
                    isinstance(got, (int, float)) and math.isclose(got, want_value, rel_tol=1e-9, abs_tol=1e-12),
                    f"{fname} attrs.{attr}: mine={got!r} ref={want_value!r}",
                )

    def test_online_batch_baseline(self):
        samples = []
        for _ in range(ONLINE_ROUNDS):
            start = time.perf_counter()
            self._batch_once()
            samples.append(time.perf_counter() - start)
        best = _best(samples)
        print(f"\n[perf] online batch x{len(self.payloads)}（装配+目标+全量属性）best-of-{ONLINE_ROUNDS}: {best * 1000:.1f}ms")
        self.assertLess(best, ONLINE_BATCH_BUDGET, f"online 批量基线击穿: {best:.2f}s")


class TestShardBatchPerf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(GOLDEN_DIR, "shardBuild1_bd.json"), encoding="utf-8") as fh:
            cls.bd = json.load(fh)
        with open(os.path.join(GOLDEN_DIR, "shardBuild1_expr.json"), encoding="utf-8") as fh:
            cls.expr = json.load(fh)
        cls.tables = _shard_tables()
        cls.engine = Engine(build_state(cls.bd["charId"], cls.bd["settings"], cls.tables), cls.tables)

    def _batch_once(self) -> None:
        """42 表达式 + 全量属性一批，逐式相对真机期望断言。"""
        for case in self.expr["cases"]:
            got = self.engine.calculate(case["expr"])
            self.assertTrue(
                isinstance(got, float) and math.isclose(got, case["ts"], rel_tol=1e-9, abs_tol=1e-12),
                f"parity 破裂: {case['expr'][:80]}\n  ts={case['ts']!r} py={got!r}",
            )
        attrs = self.engine.calculate_weapon_attributes()
        ref = self.expr["expectedAttrs"]
        for key, want in ref.items():
            if key == "weapon" or not isinstance(want, (int, float)):
                continue
            got = attrs.get(key)
            self.assertTrue(
                isinstance(got, (int, float)) and math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-12),
                f"attrs.{key}: mine={got!r} ref={want!r}",
            )

    def test_shard_batch_baseline(self):
        samples = []
        for _ in range(SHARD_ROUNDS):
            start = time.perf_counter()
            self._batch_once()
            samples.append(time.perf_counter() - start)
        best = _best(samples)
        n = len(self.expr["cases"])
        print(
            f"\n[perf] shard batch {n}expr+attrs best-of-{SHARD_ROUNDS}: {best * 1000:.1f}ms "
            f"({n / best:.0f} expr/s)"
        )
        self.assertLess(best, SHARD_BATCH_BUDGET, f"shard 批量基线击穿: {best:.2f}s")


if __name__ == "__main__":
    unittest.main()
