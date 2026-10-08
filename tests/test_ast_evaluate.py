"""表达式求值与 TS 纯属性模式的一致性用例（纯本地，无网络）。"""

import math
import unittest

from dna_builder_sdk.calc import evaluate, parse_ast
from dna_builder_sdk.calc.ast import AstError


class TestEvaluate(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(evaluate("10 + 4 * 5"), 30)
        self.assertEqual(evaluate("(10 + 4) * 5"), 70)
        self.assertEqual(evaluate("-50"), -50)
        self.assertEqual(evaluate("7 // 2"), 3)
        self.assertEqual(evaluate("-7 // 2"), -4)  # 对齐 Math.floor
        self.assertEqual(evaluate("7 % 3"), 1)

    def test_div_zero(self):
        self.assertEqual(evaluate("1 / 0"), 0.0)
        self.assertEqual(evaluate("1 // 0"), 0.0)
        self.assertEqual(evaluate("1 % 0"), 0.0)

    def test_property_and_namespace(self):
        attrs = {"攻击": 100, "ns::攻击": 7}
        self.assertEqual(evaluate("攻击 * 2", attrs), 200)
        self.assertEqual(evaluate("ns::攻击 + 1", attrs), 8)
        self.assertEqual(evaluate("不存在 + 1", attrs), 1)

    def test_builtins(self):
        self.assertEqual(evaluate("min(3, 1, 2)"), 1)
        self.assertEqual(evaluate("max(3, 1, 2)"), 3)
        self.assertEqual(evaluate("floor(2.7)"), 2)
        self.assertEqual(evaluate("ceil(2.2)"), 3)
        self.assertEqual(evaluate("or(0, 0, 5)"), 5)
        self.assertEqual(evaluate("or(0, 0, 0)"), 0)
        self.assertAlmostEqual(evaluate("log(10)", {}), math.log(10))
        self.assertEqual(evaluate("power(2, 10)"), 1024)

    def test_js_parse_float_prefix(self):
        # 镜像 JS parseFloat（TS parseFactor 语义）：畸形数字取最长合法前缀
        # 线上真实用例：BD 自定义变量 "1+0.0.0039*0.3"（用户笔误多写了 0.）
        self.assertEqual(evaluate("0.0.0039"), 0.0)
        self.assertEqual(evaluate("1+0.0.0039*0.3"), 1.0)
        self.assertEqual(evaluate("3.14"), 3.14)
        # 以下与 TS 逐项一致：".5"/"1e3" 在 TS 里根本不是数字（抛错），
        # "Infinity" 是未定义标识符（按未知变量计 0）
        self.assertRaises(AstError, evaluate, ".5")
        self.assertRaises(AstError, evaluate, "1e3")
        self.assertEqual(evaluate("Infinity"), 0.0)

    def test_hp(self):
        attrs = {"昂扬": 0.5, "背水": 0.5}
        got = evaluate("hp(0.5)", attrs)
        want = (1 + 4 * 0.5 * 0.5 * 1.0) * (1 + 0.5 * 0.5)
        self.assertAlmostEqual(got, want)

    def test_temporary_attributes(self):
        attrs = {"攻击": 100, "增伤": 0.5}
        self.assertAlmostEqual(evaluate("[攻击]{增伤:0.1}", attrs), evaluate("[攻击]", {**attrs, "增伤": 0.6}))
        # 原 attrs 不被污染
        self.assertEqual(attrs["增伤"], 0.5)

    def test_scope_and_custom(self):
        self.assertEqual(evaluate("x * 2", {}, scope={"x": 21}), 42)
        self.assertEqual(
            evaluate("10 + 4 * [花刺]层数", {}, custom_variables={"[花刺]层数": "2 + 3"}),
            30,
        )
        self.assertEqual(evaluate("double(21)", {}, custom_functions={"double": (["n"], "n * 2")}), 42)
        # 环引用：内层自引用得 0（对齐 TS），a:=a+1 求值 a+1 得 0+1+1=2，不死循环
        self.assertEqual(evaluate("a + 1", {}, custom_variables={"a": "a + 1"}), 2)

    def test_member_access(self):
        attrs = {"攻击": 100, "buff": {"攻击": 55}}
        self.assertEqual(evaluate("buff.攻击", attrs), 55)
        self.assertEqual(evaluate("攻击.暴击", attrs), 100)  # 已知分支回退基值
        self.assertEqual(evaluate("攻击.不存在分支", attrs), 0)

    def test_macros_and_errors(self):
        node = parse_ast("A", {"A": "5 + 5"})
        self.assertEqual(node["type"], "binary")
        self.assertEqual(evaluate(node), 10)
        with self.assertRaises(AstError):
            parse_ast("")


if __name__ == "__main__":
    unittest.main()
