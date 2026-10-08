"""游戏原始表加载：datapack 模块与测试 fixture 的统一入口。

TS 出处：`src/data/d/index.ts` 的 `rebuildStaticIndexes`（派生索引全部由原始条目表推导）。
datapack 模块键 = 文件名去 `.ts` 后缀（如 `buff.data.ts` → `buff.data`），一律取 `default` 导出。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# datapack 模块键 → 表名
MODULE_DEFAULTS = {
    "chars": "char.data",
    "mods": "mod.data",
    "buffs": "buff.data",
    "effects": "effect.data",
    "weapons": "weapon.data",
    "pets": "pet.data",
    "pet_entries": "pet.data",
    "monsters": "monster.data",
}

# 表名 → pet.data 内的导出名（petEntrys 具名导出；魔灵表即 default 导出，对齐 gameData `pet` 数据集）
PET_EXPORTS = {"pets": "default", "pet_entries": "petEntrys"}

# 表名 → gameData 数据集 id（default 导出占用模块 id，具名导出用 模块:导出名）
DATASET_DEFAULTS = {
    "chars": "char",
    "mods": "mod",
    "buffs": "buff",
    "effects": "effect",
    "weapons": "weapon",
    "pets": "pet",
    "pet_entries": "pet:petEntrys",
    "monsters": "monster",
}


@runtime_checkable
class TableSource(Protocol):
    """原始表数据源抽象接口：两种加载通道的统一入口。

    - 全量通道：`DataPackStore`（versions.json + `<ver>.zip` 整包下载）
    - 按需通道：`ModuleStore`（经 gameData GraphQL 按 dataset 分页拉取）

    两者都实现 `load_tables()`，上层（`Engine` / `snapshot_from_online` /
    示例脚本）只依赖本接口。
    """

    def load_tables(self) -> "GameDataTables":
        """加载并返回可直接给 `Engine` 使用的原始表容器。"""
        ...


def resolve_tables(source: "GameDataTables | TableSource") -> "GameDataTables":
    """把 `GameDataTables | TableSource` 归一为 `GameDataTables`。

    - `GameDataTables`：原样返回；
    - 具 `load_tables` 者：调用并返回其结果；其余一律 `TypeError`。
    """
    if isinstance(source, GameDataTables):
        return source
    loader = getattr(source, "load_tables", None)
    if callable(loader):
        return loader()
    raise TypeError(f"无法解析原始表数据源: {type(source).__name__}（需 GameDataTables 或实现 load_tables()）")


class GameDataTables:
    """原始表容器：lists + 派生索引（对齐 d/index.ts 的 Map 系）。"""

    def __init__(self, raw: dict):
        self.raw = raw
        self.chars: list = list(raw.get("chars") or [])
        self.mods: list = list(raw.get("mods") or [])
        self.buffs: list = list(raw.get("buffs") or [])
        self.effects: list = list(raw.get("effects") or [])
        self.weapons: list = list(raw.get("weapons") or [])
        self.pets: list = list(raw.get("pets") or [])
        self.pet_entries: list = list(raw.get("pet_entries") or [])
        self.monsters: list = list(raw.get("monsters") or [])
        self.curves: dict = dict(raw.get("curves") or {})
        # 派生索引
        self.char_by_id: dict = {}
        self.char_by_name: dict = {}
        for char in self.chars:
            self.char_by_id[char.get("id")] = char
            self.char_by_name[char.get("名称")] = char
        self.mod_by_id = {m.get("id"): m for m in self.mods}
        self.buff_by_name = {b.get("名称"): b for b in self.buffs}
        self.weapon_by_id = {w.get("id"): w for w in self.weapons}
        self.weapon_by_name = {w.get("名称"): w for w in self.weapons}
        self.mod_effect_by_id = {b.get("id"): b for b in self.effects if b.get("id") is not None and b.get("id") in self.mod_by_id}
        self.weapon_effect_by_id = {b.get("id"): b for b in self.effects if b.get("id") is not None and b.get("id") in self.weapon_by_id}
        self.pet_by_id = {p.get("id"): p for p in self.pets}
        self.monster_by_id = {m.get("id"): m for m in self.monsters}
        # 潜质目录：(bid → level → entry)，对齐 petTrait.getTraitCatalog
        self.traits_by_level: dict = {}
        for entry in self.pet_entries:
            self.traits_by_level.setdefault(entry.get("bid"), {})[entry.get("level", entry.get("r", 3) - 2)] = entry

    @classmethod
    def from_dict(cls, raw: dict) -> "GameDataTables":
        """测试/离线入口：直接接受原始表 dict（不接受 None 条目）。"""
        for table in ("chars", "mods", "buffs", "effects", "weapons", "pets", "pet_entries", "monsters"):
            values = raw.get(table) or []
            if any(v is None for v in values):
                raise ValueError(f"原始表 {table} 含 null 条目，请先清理")
        return cls(raw)

    def trait_by_level(self, bid: int, level: int) -> dict | None:
        return self.traits_by_level.get(bid, {}).get(level)
