"""BUFF code 语料回归：全部 14 段真实 code 可解析、可执行（合成沙箱，只断言不抛 JsError）。"""

import json
import os
import unittest

from dna_builder_sdk.calc.jscode import JsError, _Parser, run_js_code

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "jscode_corpus.json")


def _sandbox():
    panel = {"攻击": 100.0, "暴击": 1.0, "暴伤": 2.0, "触发": 1.0, "增伤": 0.5}
    base = {"基础攻击": 200.0, "基础暴击": 0.2, "基础暴伤": 2.0, "基础触发": 0.5}
    return {
        "攻击": 1000.0, "生命": 5000.0, "神智": 300.0, "技能威力": 2.0, "技能范围": 1.5,
        "技能耐久": 1.2, "昂扬": 0.5, "属性穿透": 0.1, "技能无视防御": 0.0,
        "充盈威力": 1.0, "召唤物攻击速度": 0.5, "召唤物独立增伤": 0.0, "增伤": 0.5,
        "char": dict(base),
        "weapon": dict(base), "meleeWeapon": dict(base), "rangedWeapon": dict(base), "skillWeapon": dict(base),
        "weaponAttr": dict(panel), "meleeWeaponAttr": dict(panel),
        "rangedWeaponAttr": dict(panel), "skillWeaponAttr": dict(panel),
        "enemy": {"等级": 80},
        "charMods": {"攻击": 1.0, "暴击": 0.5, "触发": 0.5},
        "meleeMods": {"暴击": 0.5, "暴伤": 0.5, "触发": 0.5},
        "rangedMods": {}, "skillMods": {},
    }


class TestJsCodeCorpus(unittest.TestCase):
    def test_all_corpus_parses_and_runs(self):
        with open(GOLDEN, encoding="utf-8") as fh:
            corpus = json.load(fh)
        self.assertEqual(len(corpus), 14)
        for entry in corpus:
            with self.subTest(buff=entry["name"]):
                _Parser(entry["code"]).parse_program()  # 解析不抛错
                run_js_code(entry["code"], _sandbox())  # 合成沙箱执行不抛错

    def test_var_multi_declarators(self):
        sandbox = {"x": 0.0}
        run_js_code("var c=3,t=4;if(t>0){x+=c+t}", sandbox)
        self.assertEqual(sandbox["x"], 7.0)

    def test_math_nan_propagation(self):
        sandbox = {"x": 0.0, "u": float("nan")}
        run_js_code("x=Math.min(1,u)+Math.max(1,2)", sandbox)
        import math

        self.assertTrue(math.isnan(sandbox["x"]))


if __name__ == "__main__":
    unittest.main()
