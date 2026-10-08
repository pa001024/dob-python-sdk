"""在线 BD 快照：输入 bdid 拉线上 JSON，输出完整可计算快照。

快照 = BD 设置 + 原始表 + 聚合引擎，一次装配后可反复进行表达式求值、
属性/面板查看，无需重复拉取。原始表经抽象数据源（`TableSource`）接入，
与直接构造 `Engine` 用的是同一条数据通路。
"""

from __future__ import annotations

import json

from .backend import BackendClient
from .calc.build import build_state
from .calc.engine import Engine
from .calc.gamedata import GameDataTables, TableSource, resolve_tables
from .errors import DobApiError


class BuildSnapshot:
    """一次在线 BD 的完整快照：设置 + 引擎（属性/面板/求值入口）。"""

    def __init__(self, *, bdid: str, title: str | None, char_id: int, settings: dict, engine: Engine):
        self.bdid = bdid
        self.title = title
        self.char_id = char_id
        self.settings = settings
        self.engine = engine
        self._attrs = None

    @property
    def attrs(self) -> dict:
        """全量角色属性（含 weapon 面板；首次计算后缓存）。"""
        if self._attrs is None:
            self._attrs = self.engine.calculate_weapon_attributes()
        return self._attrs

    def calculate(self, target: str | None = None) -> float:
        """目标函数值（缺省取 BD 自带 targetFunction）。"""
        return self.engine.calculate(target)

    def evaluate(self, expr: str) -> float:
        """任意表达式求值（自定义变量/函数、技能字段、成员分支均可用）。"""
        return self.calculate(expr)

    def char_panel(self) -> dict:
        """角色面板（全量角色属性，不含 weapon 键）。"""
        return self.engine.char_panel()

    def weapon_panel(self, slot: str = "melee") -> dict | None:
        """武器面板（melee/ranged/skill 或武器技能名）。"""
        return self.engine.weapon_panel(slot)

    def skill_levels_final(self) -> list:
        """最终技能等级 [(技能名, 等级)]。"""
        return self.engine.skill_levels_final()

    def skill_fields(self, skill_name: str) -> list:
        """技能字段面板数值。"""
        return self.engine.skill_fields(skill_name)

    def summary(self) -> dict:
        """一页总览：标题/目标值/核心属性/最终技能等级。"""
        attrs = self.attrs
        return {
            "bdid": self.bdid,
            "title": self.title,
            "charId": self.char_id,
            "target": self.settings.get("targetFunction"),
            "targetValue": self.calculate(),
            "attack": attrs.get("攻击"),
            "skillLevels": self.skill_levels_final()[:3],
        }


def snapshot_from_online(
    bdid: str,
    *,
    source: GameDataTables | TableSource,
    backend: BackendClient | None = None,
    skill_levels: list | None = None,
    traces: int | None = None,
) -> BuildSnapshot:
    """拉线上 BD JSON 并经 `Engine` 通路装配完整快照。

    数据只走抽象数据源一条通路：`source`（`DataPackStore` / `ModuleStore`
    的 `load_tables()`，与直接构造 `Engine` 用的表完全同构）。
    `Engine(state, tables)` 是唯一计算装配点。

    :param bdid: 构筑 id（如 Svmqw3LGoY）
    :param source: 抽象数据源（`DataPackStore` / `ModuleStore` 或任意实现
        `load_tables()` 的对象；`GameDataTables` 亦可直接传入）
    :param backend: 自建后端客户端（缺省线上端点）
    :param skill_levels: 前三技能基础等级 trio（缺省 BD 自带 charSkillLevel）
    :param traces: 溯源等级（缺省 0）

    示例（与 Engine 共用同一份表）：

        store = DataPackStore()                      # 全量通道
        store = ModuleStore(gamedata, modules=[      # 按需通道：声明时定模块列表
            "chars", "mods", "weapons", "buffs", "effects", "pets", "pet_entries", "monsters",
        ])
        snapshot = snapshot_from_online(bdid, source=store)
        engine = Engine(build_state(cid, settings, store.load_tables()), store.load_tables())
    """
    client = backend or BackendClient()
    build = client.build(bdid)
    if not build:
        raise DobApiError(f"构筑不存在: {bdid}", code="build_not_found")
    raw_settings = build.get("charSettings")
    if isinstance(raw_settings, str):
        try:
            settings = json.loads(raw_settings)
        except ValueError as error:
            raise DobApiError(f"构筑设置解析失败: {bdid}", code="bad_settings", payload=str(error)) from error
    elif isinstance(raw_settings, dict):
        settings = raw_settings
    else:
        raise DobApiError(f"构筑缺少设置: {bdid}", code="bad_settings")
    resolved = resolve_tables(source)
    state = build_state(int(build.get("charId")), settings, resolved, skill_levels=skill_levels, traces=traces)
    engine = Engine(state, resolved)
    snapshot = BuildSnapshot(
        bdid=build.get("id", bdid),
        title=build.get("title"),
        char_id=int(build.get("charId")),
        settings=engine.s.get("settings", settings),
        engine=engine,
    )
    return snapshot


def snapshot_from_settings(
    char_id: int,
    settings: dict,
    tables: GameDataTables,
    skill_levels: list | None = None,
    traces: int | None = None,
) -> BuildSnapshot:
    """本地 BD JSON 装配快照（与 snapshot_from_online 同构，不联网）。"""
    state = build_state(char_id, settings, tables, skill_levels=skill_levels, traces=traces)
    return BuildSnapshot(bdid="", title=None, char_id=char_id, settings=state.get("settings", settings), engine=Engine(state, tables))
