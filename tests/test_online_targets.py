"""线上 BD 靶点 parity：真实分享构筑（modVariants/dotSettings/effectConfig/条件 BUFF 覆盖）。

输入 tests/golden/online/*.json（BD 原文，api.dna-builder.cn 拉取）+
tests/golden/online_data.json（union 原始表）；期望 tests/golden/online_expected.json。
复现链见 tests/golden/online/README.md（.tmp 脚本）。

DOT 分流：含 `DOT伤害` 的变量（传递闭包）跳过值断言（DOT 引擎超出本 SDK 范围），
其余变量、target、attrs、panels 全部必须一致——条件 BUFF（buff.技能）的过滤与
字段级合并正是由这些非 DOT 变量证明的。
"""

import json
import math
import os
import unittest

from dna_builder_sdk.calc.ast import tokenize_ast
from dna_builder_sdk.calc.build import build_state
from dna_builder_sdk.calc.engine import Engine
from dna_builder_sdk.calc.gamedata import GameDataTables

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")
ONLINE_DIR = os.path.join(GOLDEN_DIR, "online")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _tables():
    data = _load(os.path.join(GOLDEN_DIR, "online_data.json"))
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


def _referenced_vars(body: str, names: set) -> set:
    """表达式引用的自定义变量名（IDENT token 精确匹配）。"""
    try:
        tokens = tokenize_ast(body)
    except Exception:  # noqa: BLE001
        return set()
    idents = {t["value"] for t in tokens if t["kind"] == "IDENT"}
    return {n for n in names if n in idents}


def _dot_dependent(settings: dict) -> set:
    """传递引用 DOT伤害 的变量名集合。"""
    entries = list(settings.get("customVariables") or [])
    names = {k for k, _ in entries}
    bodies = dict(entries)
    direct = {name for name, body in entries if "DOT伤害" in (body or "")}
    closure = set(direct)
    changed = True
    while changed:
        changed = False
        for name, body in entries:
            if name in closure:
                continue
            if _referenced_vars(body or "", closure):
                closure.add(name)
                changed = True
    _ = names
    return closure


class TestOnlineTargets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = _tables()
        cls.expected = _load(os.path.join(GOLDEN_DIR, "online_expected.json"))["expected"]
        cls.files = sorted(f for f in os.listdir(ONLINE_DIR) if f.endswith(".json"))
        assert cls.files, "online 靶点缺失"

    def test_targets(self):
        for file in self.files:
            with self.subTest(target=file):
                payload = _load(os.path.join(ONLINE_DIR, file))
                key = file.replace(".json", "")
                want = self.expected[key]
                engine = Engine(build_state(payload["charId"], payload["settings"], self.tables), self.tables)
                skipped = _dot_dependent(payload["settings"])
                if skipped:
                    print(f"\n{key}: 跳过 {len(skipped)} 个 DOT 依赖变量")
                matched = 0
                for name, want_value in (want.get("vars") or {}).items():
                    if name in skipped or not isinstance(want_value, (int, float)):
                        continue
                    try:
                        got = engine.calculate(name)
                    except Exception as error:  # noqa: BLE001
                        self.fail(f"{file} 变量 {name} 求值抛错: {error}")
                    self.assertTrue(
                        isinstance(got, float) and math.isclose(got, want_value, rel_tol=1e-9, abs_tol=1e-12),
                        f"{file} 变量 {name}: mine={got!r} ref={want_value!r}",
                    )
                    matched += 1
                self.assertTrue(matched > 0, f"{file} 无有效变量断言")
                if payload["settings"].get("targetFunction") not in skipped and isinstance(want.get("target"), (int, float)):
                    got_target = engine.calculate()
                    self.assertTrue(
                        isinstance(got_target, float) and math.isclose(got_target, want["target"], rel_tol=1e-9, abs_tol=1e-12),
                        f"{file} target: mine={got_target!r} ref={want['target']!r}",
                    )
                got_attrs = engine.calculate_weapon_attributes()
                for attr, want_value in want["attrs"].items():
                    if attr == "weapon" or not isinstance(want_value, (int, float)):
                        continue
                    got = got_attrs.get(attr)
                    self.assertTrue(
                        isinstance(got, (int, float)) and math.isclose(got, want_value, rel_tol=1e-9, abs_tol=1e-12),
                        f"{file} attrs.{attr}: mine={got!r} ref={want_value!r}",
                    )

    def test_conditional_buff_filtering(self):
        """buff.技能 过滤专断：Svmqw3LGoY 的条件 BUFF 不进全局表，只进字段级。"""
        payload = _load(os.path.join(ONLINE_DIR, "Svmqw3LGoY.json"))
        engine = Engine(build_state(payload["charId"], payload["settings"], self.tables), self.tables)
        global_names = [b.get("名称") for b in engine.s["buffs"]]
        dynamic_names = [b.get("名称") for b in engine.s["dynamicBuffs"]]
        # 纯 code 条件 BUFF：只进 code 槽位，不进全局汇总槽位（对齐构造器两道 filter）
        self.assertIn("琳恩裂伤特性", dynamic_names)
        self.assertNotIn("琳恩裂伤特性", global_names)
        # 条件 code 经差分进字段级规则（getScopedCodeBuffConditionals 口径）
        rules = engine.conditional_list()
        self.assertTrue(any(r["技能"] == "裂伤" for r in rules), "裂伤条件规则缺失")
        # [引爆]单次走条件 code 路径，与真机一致即证明字段级合并生效
        want = self.expected["Svmqw3LGoY"]["vars"]["[引爆]单次"]
        got = engine.calculate("[引爆]单次")
        self.assertTrue(math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-12), f"mine={got!r} ref={want!r}")


if __name__ == "__main__":
    unittest.main()
