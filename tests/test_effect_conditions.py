"""手造 fixture：mod 生效条件未命中 / 额外精通未命中（现有 golden 全是命中态）。

数据源走 mock 全量包（无网络），用最小装配精确断言“生效/未生效”分界。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from dna_builder_sdk import DataPackStore
from dna_builder_sdk.calc import Engine, build_state
from dna_builder_sdk.calc.entities import MOD_QUALITY_MAX_LEVEL, level_mod

MOCK_ZIP = Path(__file__).parent / "../../../mock/data-pack/1.6.208.6.zip"
CHAR_ID = 1501  # 莉兹贝尔（精通 重剑/霰弹枪）


def _scale(raw, level):
    top = MOD_QUALITY_MAX_LEVEL.get(raw.get("品质"), 1)
    lv = max(0, min(top, level))
    return (lv + 1) / (top + 1)


def _base(raw, level, key):
    """某 mod 在某等级下对 key 的基础贡献（不含生效块），小数。"""
    inst = level_mod(raw, level)
    value = inst.get(key)
    if isinstance(value, bool):
        return 0.0
    return float(value or 0.0)


class EffectConditionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MOCK_ZIP.is_file():
            raise unittest.SkipTest(f"mock 包缺失：{MOCK_ZIP}")
        cls._tmp = tempfile.mkdtemp()
        dest = Path(cls._tmp) / "1.6.208.6"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(MOCK_ZIP, dest / "package.zip")
        store = DataPackStore(cache_dir=cls._tmp)
        store.activate("1.6.208.6")
        cls.tables = store.load_tables()
        cls.byid = {m["id"]: m for m in cls.tables.mods}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _attrs(self, settings):
        base = {
            "charLevel": 80, "charSkillLevel": [10, 10, 10],
            "meleeWeapon": 0, "rangedWeapon": 0,
            "auraMod": 0,  # 无中枢（默认 31524 会污染极性计数，特例另传）
        }
        base.update(settings)
        state = build_state(CHAR_ID, base, self.tables)
        self._state = state
        return Engine(state, self.tables).calculate_attributes()

    def test_polarity_met(self):
        """D趋向>=4：4 个 D（含自身）→ 41735 背水 + 41715 技能耐久双双生效。"""
        mods = [[41735, 10], [31522, 10], [41002, 10], [41715, 10]]
        attrs = self._attrs({"charMods": mods})
        char_bonus = (self.tables.char_by_id[CHAR_ID].get("加成") or {})
        r735, r715 = self.byid[41735], self.byid[41715]
        self.assertAlmostEqual(
            attrs["背水"],
            sum(_base(self.byid[mid], lv, "背水") for mid, lv in mods) + 0.12 * _scale(r735, 10))
        self.assertAlmostEqual(
            attrs["技能耐久"],
            1 + sum(_base(self.byid[mid], lv, "技能耐久") for mid, lv in mods)
            + float(char_bonus.get("技能耐久") or 0) + 0.36 * _scale(r715, 10))

    def test_polarity_unmet(self):
        """D趋向>=4：只有 3 个 D → 41735 生效块被跳过（只剩基础值）。"""
        mods = [[41735, 10], [31522, 10], [41002, 10], [41411, 10]]
        attrs = self._attrs({"charMods": mods})
        self.assertAlmostEqual(
            attrs["背水"], sum(_base(self.byid[mid], lv, "背水") for mid, lv in mods))

    def test_id_met(self):
        """*id<=1：41716 只带一张 → 昂扬生效。"""
        mods = [[41716, 10]]
        attrs = self._attrs({"charMods": mods})
        raw = self.byid[41716]
        self.assertAlmostEqual(
            attrs["昂扬"],
            sum(_base(self.byid[mid], lv, "昂扬") for mid, lv in mods) + 0.36 * _scale(raw, 10))

    def test_id_unmet(self):
        """*id<=1：41716 带两张（最大重复 2>1）→ 生效块被跳过，只剩两张基础值。"""
        mods = [[41716, 10], [41716, 10]]
        attrs = self._attrs({"charMods": mods})
        self.assertAlmostEqual(
            attrs["昂扬"], sum(_base(self.byid[mid], lv, "昂扬") for mid, lv in mods))

    def test_threshold_unmet(self):
        """技能范围>=1.6：白板 1.0 → 56162 未生效（只剩基础值）。"""
        mods = [[56162, 10]]
        attrs = self._attrs({"charMods": mods})
        self.assertAlmostEqual(
            attrs["昂扬"], sum(_base(self.byid[mid], lv, "昂扬") for mid, lv in mods))

    def test_threshold_met(self):
        """技能范围>=1.6：41214(0.5)+41713(0.3) 顶到 1.8 → 56162 昂扬生效（不动点迭代）。"""
        mods = [[56162, 10], [41214, 10], [41713, 10]]
        attrs = self._attrs({"charMods": mods})
        raw = self.byid[56162]
        expect = (
            sum(_base(self.byid[mid], lv, "昂扬") for mid, lv in mods)
            + 0.66 * _scale(raw, 10)
        )
        self.assertAlmostEqual(attrs["昂扬"], expect)
        self.assertGreaterEqual(attrs["技能范围"], 1.6)

    def test_aura_counts_toward_polarity(self):
        """A趋向>=4：aura 自身计入极性（3A 槽 +aura=4 生效，2A 槽 +aura=3 不生效）。"""
        met = self._attrs({
            "charMods": [[31502, 10], [31513, 10], [31526, 10]],
            "auraMod": 51765,
        })
        unmet = self._attrs({
            "charMods": [[31502, 10], [31513, 10], [31522, 10]],
            "auraMod": 51765,
        })
        raw_aura = self.byid[51765]
        met_mods = [(31502, 10), (31513, 10), (31526, 10)]
        unmet_mods = [(31502, 10), (31513, 10), (31522, 10)]
        delta_base = sum(_base(self.byid[mid], lv, "昂扬") for mid, lv in met_mods) - sum(
            _base(self.byid[mid], lv, "昂扬") for mid, lv in unmet_mods)
        self.assertAlmostEqual(
            met["昂扬"] - unmet["昂扬"], 0.66 * _scale(raw_aura, 10) + delta_base)

    def test_forge_miss_without_extra_mastery(self):
        """20298（双枪熔炼）+ 无额外精通 → 特效层失效，武器加成全部丢弃。"""
        attrs = self._attrs({"meleeWeapon": 20298, "meleeWeaponRefine": 5, "meleeWeaponLevel": 80})
        weapon = self._state["meleeWeapon"]
        self.assertFalse(weapon["forgeEffective"])
        self.assertEqual(weapon["buffProps"], {})
        engine = Engine(self._state, self.tables)
        self.assertFalse(engine.mastered("双枪"))

    def test_forge_hit_with_extra_mastery(self):
        """extraMastery=双枪（单选）→ 特效层生效，生命/背水等加成计入。"""
        miss = self._attrs({
            "meleeWeapon": 20298, "meleeWeaponRefine": 5, "meleeWeaponLevel": 80,
        })
        hit = self._attrs({
            "meleeWeapon": 20298, "meleeWeaponRefine": 5, "meleeWeaponLevel": 80,
            "extraMastery": "双枪",
        })
        weapon = self._state["meleeWeapon"]
        self.assertTrue(weapon["forgeEffective"])
        self.assertGreater(len(weapon["buffProps"]), 0)
        engine = Engine(self._state, self.tables)
        self.assertTrue(engine.mastered("双枪"))
        # 未命中：锻造加成（生命 3、背水 0.2）全部丢弃；命中：全额计入
        self.assertAlmostEqual(miss["背水"], 0.0)
        self.assertAlmostEqual(hit["背水"], 0.2)
        from dna_builder_sdk.calc.entities import level_char

        base_hp = level_char(self.tables.char_by_id[CHAR_ID], 80)["基础生命"]
        self.assertAlmostEqual(miss["生命"] - base_hp * 4, 0.0, places=0)
        self.assertAlmostEqual(hit["生命"] - miss["生命"], base_hp * 3)


if __name__ == "__main__":
    unittest.main()
