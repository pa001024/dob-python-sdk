"""面板示例：指定等级 + MOD 列表 → 角色/武器面板；技能等级 trio + 溯源 → 最终等级与字段面板。

运行（仓库根目录，离线 fixture 默认；联网可换通道）：
    PYTHONPATH=sdk/python/src python sdk/python/examples/panels.py
    PYTHONPATH=sdk/python/src python sdk/python/examples/panels.py datapack
    PYTHONPATH=sdk/python/src python sdk/python/examples/panels.py modules

数据源只走抽象接口（`TableSource.load_tables()` → `GameDataTables` → `Engine`）：

- fixture：`GameDataTables.from_dict`（离线演示，BD JSON 形状不变）；
- datapack：`DataPackStore().load_tables()`（全量包，本地缓存复用）；
- modules：`ModuleStore(gamedata, modules=[...]).load_tables()`
  （按需通道，模块列表在声明时确定）。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dna_builder_sdk.calc.build import build_state
from dna_builder_sdk.calc.engine import Engine
from dna_builder_sdk.calc.gamedata import GameDataTables

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "..", "tests", "golden")

# Engine 用的 8 张标准表（按需通道声明时即定死，顺序与 GameDataTables 一致）。
ENGINE_TABLES = ["chars", "mods", "buffs", "effects", "weapons", "pets", "pet_entries", "monsters"]


def load_tables(kind: str = "fixture") -> GameDataTables:
    """按通道加载原始表：统一返回可直接给 Engine 用的 GameDataTables。"""
    if kind == "datapack":
        from dna_builder_sdk import DataPackStore

        return DataPackStore().load_tables()
    if kind == "modules":
        from dna_builder_sdk import BackendClient, GameDataClient, ModuleStore

        # 按需通道：声明时就需要支持声明模块列表，load_tables() 只拉这些表。
        return ModuleStore(GameDataClient(BackendClient()), modules=ENGINE_TABLES).load_tables()
    if kind != "fixture":
        raise SystemExit(f"未知通道: {kind}（可选 fixture / datapack / modules）")
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


def minimal_settings(**override) -> dict:
    settings = {
        "charLevel": 80,
        "charSkillLevel": 10,
        "baseName": "萨麦尔",
        "hpPercent": 1,
        "resonanceGain": 0,
        "enemyId": 130,
        "enemyLevel": 80,
        "enemyResistance": 0,
        "imbalance": False,
        "targetFunction": "伤害",
        "customVariables": [],
        "auraMod": 0,
        "charMods": [],
        "meleeMods": [],
        "rangedMods": [],
        "skillWeaponMods": [],
        "meleeWeapon": 10399,
        "meleeWeaponLevel": 80,
        "meleeWeaponRefine": 5,
        "rangedWeapon": 20510,
        "rangedWeaponLevel": 80,
        "rangedWeaponRefine": 5,
        "buffs": [],
        "customBuff": [],
        "petId": 0,
        "petLevel": 3,
        "petCoverage": 1,
        "extraMastery": "",
        "modVariantIndex": 0,
        "modVariants": [],
        "effectConfig": {},
        "useGlobal": False,
    }
    settings.update(override)
    return settings


def scenario_char_panel(tables: GameDataTables) -> None:
    """场景 1：指定角色等级 + MOD 列表 → 角色面板。"""
    settings = minimal_settings(
        charLevel=70,
        charMods=[[51463, 10], [51326, 10]],
    )
    engine = Engine(build_state(1501, settings, tables), tables)
    panel = engine.char_panel()
    print("== 角色面板（70 级 + 2 MOD）==")
    for key in ("攻击", "生命", "护盾", "防御", "神智", "增伤", "暴击", "技能威力"):
        print(f"  {key}: {panel.get(key)}")


def scenario_weapon_panel(tables: GameDataTables) -> None:
    """场景 2：指定武器等级/精炼 + MOD 列表 → 武器面板。"""
    settings = minimal_settings(
        meleeWeapon=10399,
        meleeWeaponLevel=80,
        meleeWeaponRefine=5,
        meleeMods=[[52011, 10], [52007, 10]],
    )
    engine = Engine(build_state(1501, settings, tables), tables)
    panel = engine.weapon_panel("melee")
    print("== 近战武器面板（80 级/5 精炼 + 2 MOD）==")
    for key in ("攻击", "暴击", "暴伤", "触发", "攻速", "增伤"):
        print(f"  {key}: {(panel or {}).get(key)}")


def scenario_skill_levels(tables: GameDataTables) -> None:
    """场景 3：技能等级 trio + 溯源 → 最终等级与字段面板。"""
    settings = minimal_settings(charSkillLevel=12)
    engine = Engine(
        build_state(1501, settings, tables, skill_levels=[10, 10, 10], traces=7),
        tables,
    )
    print("== 最终技能等级（trio [10,10,10] + 满溯源 7）==")
    for name, level in engine.skill_levels_final()[:3]:
        print(f"  {name}: {level}")
    print("== 以坚忍之名 字段面板 ==")
    for field in engine.skill_fields("以坚忍之名")[:4]:
        print(f"  {field.get('名称')}: 值={field.get('值')} 值2={field.get('值2')}")


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "fixture"
    tables = load_tables(kind)
    print(f"-- 原始表通道: {kind} --")
    scenario_char_panel(tables)
    scenario_weapon_panel(tables)
    scenario_skill_levels(tables)
