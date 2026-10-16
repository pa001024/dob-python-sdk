"""BUFF 结构化 attr 与 tag 条件单测（新格式 attr 子表 + 旧格式顶层折叠）。"""
import re
import unittest

from dna_builder_sdk.calc import entities
from dna_builder_sdk.calc.damage import DamageContext
from dna_builder_sdk.calc.entities import (
    buff_is_layer_prop,
    level_buff,
    level_skill_fields_with_attr,
    match_conditional,
)


class BuffAttrTest(unittest.TestCase):
    def test_statics_new_shape(self):
        raw = {
            "名称": "测试",
            "mx": 2,
            "attr": {"攻击": 0.5, "暴击": [0.7, 0.2], "技能伤害": "0.8*技能威力", "@近战增伤": 0.6},
        }
        inst = level_buff(raw, 1)
        self.assertAlmostEqual(inst["攻击"], 0.5)
        self.assertAlmostEqual(inst["暴击"], 0.7)
        self.assertNotIn("技能伤害", inst)
        self.assertAlmostEqual(inst["近战增伤"], 0.6)
        self.assertNotIn("@近战增伤", inst)
        self.assertTrue(buff_is_layer_prop(raw, "近战增伤"))
        self.assertFalse(buff_is_layer_prop(raw, "攻击"))

    def test_old_shape_fold(self):
        raw = {"名称": "旧", "mx": 2, "攻击": 0.5, "暴击": [0.1, 0.2]}
        inst = level_buff(raw, 1)
        self.assertAlmostEqual(inst["攻击"], 0.5)
        self.assertAlmostEqual(inst["暴击"], 0.1)
        self.assertTrue(buff_is_layer_prop({"@近战增伤": 0.6}, "近战增伤"))

    def test_attr_precedence(self):
        raw = {"名称": "冲突", "攻击": 0.1, "attr": {"攻击": 0.5}}
        self.assertAlmostEqual(level_buff(raw, 1)["攻击"], 0.5)

    def test_level_copies_tag(self):
        raw = {"名称": "充盈条件", "tag": ["充盈"], "attr": {"独立增伤": 0.2}}
        inst = level_buff(raw, 1)
        self.assertEqual(inst["tag"], ["充盈"])
        self.assertAlmostEqual(inst["独立增伤"], 0.2)


class MatchConditionalTest(unittest.TestCase):
    def test_matrix(self):
        pattern = re.compile("暗影奔袭")
        cases = [
            (None, None, "暗影奔袭", ["充盈"], False),
            (pattern, None, "暗影奔袭", None, True),
            (pattern, None, "暗影一刺", None, False),
            (None, ["充盈"], "附加伤害", ["技能", "充盈"], True),
            (None, ["充盈"], "伤害", ["技能"], False),
            (pattern, ["充盈"], "暗影奔袭", ["充盈"], True),
            (pattern, ["充盈"], "暗影奔袭", ["技能"], False),
            (pattern, ["充盈"], "附加伤害", ["充盈"], False),
        ]
        for p, t, name, tags, want in cases:
            self.assertEqual(match_conditional(p, t, name, tags), want, f"{name} {tags}")

    def test_fields_with_attr_tag(self):
        skill = {
            "safeName": "Q",
            "字段": [
                {"名称": "充盈伤害", "safeName": "充盈伤害", "tag": ["技能", "充盈"], "影响": "技能威力", "值": 1.0},
                {"名称": "伤害", "safeName": "伤害", "tag": ["技能"], "影响": "技能威力", "值": 1.0},
            ],
        }
        attrs = {"技能威力": 2.0, "技能耐久": 1, "技能效益": 1, "技能范围": 1, "技能倍率乘数": 0.0, "技能倍率加数": 0}
        rules = [(None, ["充盈"], {"技能倍率乘数": 0.5})]
        out = level_skill_fields_with_attr(skill, attrs, rules)
        by_name = {f["名称"]: f for f in out}
        self.assertAlmostEqual(by_name["充盈伤害"]["值"], 1.0 * (1 + 0.5) * 2.0)
        self.assertAlmostEqual(by_name["伤害"]["值"], 1.0 * 2.0)

    def test_cond_props_tag(self):
        ctx = DamageContext(
            attrs={}, panels={}, skill_tables={}, skill_names=[], aliases={},
            rules=[(None, ["充盈"], {"独立增伤": 0.2})],
            attack_bonus={}, weapons={}, weapon_bases={}, combat={}, base_name="Q",
        )
        hit = ctx.cond_props("附加伤害", ["技能", "充盈"], "Q")
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit["独立增伤"], 0.2)
        self.assertIsNone(ctx.cond_props("伤害", ["技能"], "Q"))
        # 同名字段在不同 base 下缓存隔离
        self.assertIsNone(ctx.cond_props("附加伤害", ["技能"], "E"))


META_KEYS = frozenset(
    [
        "id", "名称", "描述", "限定", "品质", "版本",
        "a", "b", "dx", "lx", "mx", "code",
        "技能", "tag", "attr", "display",
    ]
)


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _to_new_shape(row: dict) -> dict:
    """把旧格式 buff/effect 行（顶层直挂数值）转成新格式（数值进 attr）。"""
    converted = {k: v for k, v in row.items() if k in META_KEYS or k == "attr"}
    attr = dict(converted.get("attr") or {})
    for key, value in row.items():
        if key in META_KEYS or key in attr:
            continue
        if _is_num(value):
            attr[key] = value
        elif isinstance(value, list) and all(_is_num(v) for v in value):
            attr[key] = list(value)
    if attr:
        converted["attr"] = attr
    return converted


def _to_old_shape(row: dict) -> dict:
    """把新格式行（数值在 attr）的数值摊回顶层，还原旧格式形态（与 _to_new_shape 互逆）。"""
    old = {k: v for k, v in row.items() if k != "attr"}
    for key, value in (row.get("attr") or {}).items():
        old[key] = value
    return old


class BuffShapeParityTest(unittest.TestCase):
    """同一份新格式包数据：attr 形态与摊平旧形态在 Engine 侧必须逐位一致。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        import shutil
        import tempfile

        mock_zip = Path(__file__).parent / "../../../mock/data-pack/1.6.208.8.zip"
        if not mock_zip.is_file():
            raise unittest.SkipTest(f"mock 包缺失：{mock_zip}")
        from dna_builder_sdk import DataPackStore
        from dna_builder_sdk.calc.build import build_state
        from dna_builder_sdk.calc.engine import Engine
        from dna_builder_sdk.calc.gamedata import GameDataTables

        cls._tmp = tempfile.mkdtemp()
        dest = Path(cls._tmp) / "1.6.208.8"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(mock_zip, dest / "package.zip")
        store = DataPackStore(cache_dir=cls._tmp)
        store.activate("1.6.208.8")
        new_tables = store.load_tables()
        raw = new_tables.raw
        old_raw = dict(raw)
        old_raw["buffs"] = [_to_old_shape(b) for b in raw.get("buffs") or []]
        old_raw["effects"] = [_to_old_shape(e) for e in raw.get("effects") or []]
        old_tables = GameDataTables(old_raw)
        settings = {
            "charLevel": 80, "charSkillLevel": [10, 10, 10],
            "meleeWeapon": 0, "rangedWeapon": 0, "auraMod": 0,
            "buffs": [["榛子大冲击", 10], ["连击蓄力攻击加成", 5], ["羽翼·鼓舞·昂扬(雷)", 10], ["法露茜2溯", 1]],
        }
        cls.old_engine = Engine(build_state(1501, dict(settings), old_tables), old_tables)
        cls.new_engine = Engine(build_state(1501, dict(settings), new_tables), new_tables)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(getattr(cls, "_tmp", ""), ignore_errors=True)

    def test_converted_shape(self):
        """转换非空转：旧行顶层数值确实搬进了 attr。"""
        converted = _to_new_shape({"名称": "榛子大冲击", "id": 5101, "a": 11, "lx": 0, "mx": 10, "背水": 0.22})
        self.assertEqual(converted.get("attr"), {"背水": 0.22})
        self.assertNotIn("背水", {k for k in converted if k != "attr"})

    def test_attributes_identical(self):
        self.assertEqual(self.old_engine.calculate_attributes(), self.new_engine.calculate_attributes())

    def test_conditional_list_identical(self):
        old = {(r.get("pattern"), tuple(r.get("tag") or [])): r["props"] for r in self.old_engine.conditional_list()}
        new = {(r.get("pattern"), tuple(r.get("tag") or [])): r["props"] for r in self.new_engine.conditional_list()}
        self.assertEqual(old.keys(), new.keys())
        for key in old:
            self.assertEqual(old[key], new[key], key)


class NewPackBuffTest(unittest.TestCase):
    """新格式包（1.7.4.1）：行形态断言 + 落子引擎。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        import shutil
        import tempfile

        mock_zip = Path(__file__).parent / "../../../mock/data-pack/1.7.4.1.zip"
        if not mock_zip.is_file():
            raise unittest.SkipTest(f"mock 包缺失：{mock_zip}")
        from dna_builder_sdk import DataPackStore
        from dna_builder_sdk.calc import entities as _entities
        from dna_builder_sdk.calc.build import build_state
        from dna_builder_sdk.calc.engine import Engine
        from dna_builder_sdk.calc.gamedata import GameDataTables

        cls._tmp = tempfile.mkdtemp()
        dest = Path(cls._tmp) / "1.7.4.1"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(mock_zip, dest / "package.zip")
        store = DataPackStore(cache_dir=cls._tmp)
        store.activate("1.7.4.1")
        cls.tables = store.load_tables()
        cls._build_state = staticmethod(build_state)
        cls._Engine = Engine
        cls._entities = _entities
        cls._GameDataTables = GameDataTables

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(getattr(cls, "_tmp", ""), ignore_errors=True)

    def test_row_shapes(self):
        for table_name in ("buffs", "effects"):
            for row in getattr(self.tables, table_name):
                if not isinstance(row, dict):
                    continue
                for key, value in row.items():
                    if key in META_KEYS:
                        continue
                    self.fail(f"{table_name} {row.get('名称')} 顶层遗留键 {key}")
                attr = row.get("attr")
                if not isinstance(attr, dict):
                    continue
                for key, value in attr.items():
                    ok = _is_num(value) or isinstance(value, str) or (
                        isinstance(value, list) and all(_is_num(v) for v in value)
                    )
                    self.assertTrue(ok, f"{table_name} {row.get('名称')} attr 非法值 {key}: {value!r}")

    def test_lirico_tag_engine(self):
        lirico = self.tables.buff_by_name.get("莉莉蔻4溯")
        self.assertIsNotNone(lirico)
        self.assertEqual(lirico.get("tag"), ["充盈"])
        self.assertAlmostEqual((lirico.get("attr") or {}).get("独立增伤"), 0.2)
        inst = self._entities.level_buff(lirico, 1)
        self.assertEqual(inst.get("tag"), ["充盈"])
        self.assertAlmostEqual(inst.get("独立增伤"), 0.2)
        settings = {
            "charLevel": 80, "charSkillLevel": [10, 10, 10],
            "meleeWeapon": 0, "rangedWeapon": 0, "auraMod": 0,
            "buffs": [["莉莉蔻4溯", 1]],
        }
        engine = self._Engine(self._build_state(1501, settings, self.tables), self.tables)
        found = [
            r for r in engine.conditional_list()
            if (r.get("tag") or []) == ["充盈"] and abs((r["props"].get("独立增伤") or 0) - 0.2) < 1e-9
        ]
        self.assertTrue(found, "conditional_list 缺 tag[充盈] 独立增伤条目")


if __name__ == "__main__":
    unittest.main()
