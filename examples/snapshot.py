"""在线快照示例：bdid → 线上 JSON → 完整快照 → 求值/属性查看。

数据源只走抽象接口（`TableSource.load_tables()`，与直接构造 `Engine`
用的表完全同构），不再为快照维护独立分支：

    PYTHONPATH=sdk/python/src python sdk/python/examples/snapshot.py Svmqw3LGoY
    PYTHONPATH=sdk/python/src python sdk/python/examples/snapshot.py rJHVA0BVOM modules
    PYTHONPATH=sdk/python/src python sdk/python/examples/snapshot.py Svmqw3LGoY datapack "角色::攻击!"

第二个参数选通道：datapack（全量包，默认；本地有缓存即复用）/
modules（按需：声明时定模块列表，经 gameData 分页拉取）；其余参数为自定义表达式。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dna_builder_sdk import BackendClient, DataPackStore, GameDataClient, ModuleStore, snapshot_from_online

# Engine 用的 8 张标准表（与 GameDataTables 同序；按需通道声明时即定死）。
ENGINE_TABLES = ["chars", "mods", "buffs", "effects", "weapons", "pets", "pet_entries", "monsters"]


def make_source(kind: str, cache_dir: str | None = None):
    """按通道构造抽象数据源：全量包 / 按需模块（声明时定模块列表）。"""
    if kind == "modules":
        # 按需通道：声明时就需要支持声明模块列表，后续 load_tables() 只拉这些表。
        return ModuleStore(GameDataClient(BackendClient()), cache_dir=cache_dir, modules=ENGINE_TABLES)
    if kind != "datapack":
        raise SystemExit(f"未知通道: {kind}（可选 datapack / modules）")
    return DataPackStore(cache_dir=cache_dir)


def main() -> None:
    bdid = sys.argv[1] if len(sys.argv) > 1 else "Svmqw3LGoY"
    rest = sys.argv[2:]
    kind = "datapack"
    if rest and rest[0] in ("datapack", "modules"):
        kind, rest = rest[0], rest[1:]
    source = make_source(kind)
    # 快照经 Engine 通路装配：source.load_tables() 与 Engine 直用的表同构。
    snapshot = snapshot_from_online(bdid, source=source)
    print(f"== {snapshot.title}（{snapshot.bdid}）[{kind}] ==")
    print("目标函数:", snapshot.settings.get("targetFunction"))
    print("目标值:", snapshot.calculate())
    print("攻击:", snapshot.attrs.get("攻击"), "增伤:", snapshot.attrs.get("增伤"))
    print("最终技能等级:", snapshot.skill_levels_final()[:3])
    print("近战面板攻击:", (snapshot.weapon_panel("melee") or {}).get("攻击"))
    # 自定义表达式求值（快照内即 Engine，可反复求值无需重复拉取）
    print("表达式求值 [角色::攻击!]:", snapshot.evaluate("角色::攻击!"))
    for expr in rest:
        print(f"表达式求值 [{expr}]:", snapshot.evaluate(expr))


if __name__ == "__main__":
    main()
