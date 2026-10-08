"""按模块按需加载：经 gameData GraphQL 分页拉取数据集，磁盘 + 内存两级持久化。

与 DataPackStore 的区别：这里不下载整包 zip，而是按 dataset 粒度
（iter_all 分页，上限 500/页）拉取并缓存为 <cache>/gamedata/<dataset>.json，
适合只需要少量数据集的脚本。两套存储可并存，互不干扰。
"""

from __future__ import annotations

import json
import os
import time

from .game_data import GameDataClient, normalize_items

from .calc.gamedata import DATASET_DEFAULTS

_MAX_PAGE = 500


def _safe_name(dataset: str) -> str:
    return dataset.replace(":", "__").replace("/", "__")


def _default_cache_dir() -> str:
    override = os.environ.get("DNA_BUILDER_CACHE")
    if override:
        return os.path.join(override, "gamedata")
    return os.path.join(os.path.expanduser("~"), ".cache", "dna-builder", "gamedata")


class ModuleStore:
    """dataset 粒度缓存：内存热缓存 + 磁盘 JSON，fetch 即热重载。

    同时实现原始表数据源抽象接口（`load_tables()`），可直接给 `Engine` 使用：

        store = ModuleStore(gamedata, modules=["chars", "mods", "weapons"])
        tables = store.load_tables()          # -> GameDataTables
        engine = Engine(build_state(cid, settings, tables), tables)

    模块列表在声明时确定（`modules` 表名子集 / `mapping` 表→数据集全量映射，
    缺省 8 张全量表），`load_tables()` 只拉取声明过的表。
    """

    def __init__(
        self,
        game_data: GameDataClient,
        cache_dir: str | None = None,
        modules: list[str] | None = None,
        mapping: dict[str, str] | None = None,
    ):
        self.game_data = game_data
        self.cache_dir = cache_dir or _default_cache_dir()
        os.makedirs(self.cache_dir, exist_ok=True)
        if mapping is not None:
            self.mapping: dict[str, str] = dict(mapping)
        elif modules is not None:
            unknown = [m for m in modules if m not in DATASET_DEFAULTS]
            if unknown:
                raise ValueError(f"未知原始表: {unknown}（可选 {sorted(DATASET_DEFAULTS)}")
            self.mapping = {table: DATASET_DEFAULTS[table] for table in modules}
        else:
            self.mapping = dict(DATASET_DEFAULTS)
        self._memory: dict[str, dict] = {}
        self._mtimes: dict[str, float] = {}

    def path_for(self, dataset: str) -> str:
        return os.path.join(self.cache_dir, f"{_safe_name(dataset)}.json")

    # ---- 读取 ----
    def get(self, dataset: str, auto_fetch: bool = True, **query_kwargs) -> list:
        """取 dataset 全量 items：内存 → 磁盘 →（可选）远端分页拉取。"""
        if dataset in self._memory:
            return self._memory[dataset]["items"]
        cached = self._read_disk(dataset)
        if cached is not None:
            self._memory[dataset] = cached
            return cached["items"]
        if not auto_fetch:
            return []
        return self.fetch(dataset, **query_kwargs)

    def record(self, dataset: str, key: str, **query_kwargs):
        """按键取单条：先查本地缓存，缺失则走 gameDataRecord（不拉全量）。"""
        for item in self.get(dataset, auto_fetch=False):
            if str(item.get("key")) == str(key):
                return item
        if dataset not in self._memory and self._read_disk(dataset) is None:
            rec = self.game_data.record(dataset, key)
            return {"key": key, "data": (rec or {}).get("data")} if rec else None
        return None

    # ---- 写入 / 热重载 ----
    def fetch(self, dataset: str, kind: str = "array", **query_kwargs) -> list:
        """远端分页拉全量并持久化，内存热替换（运行时热重载入口）。

        payload 同时存 `items`（服务端原样）与归一化后的 `value`
       （见 game_data.normalize_items，与 datapack 模块值同形）。
        """
        items: list = []
        total = 0
        for page in self.game_data.iter_all(dataset, page_size=_MAX_PAGE, **query_kwargs):
            total = int(page.get("total") or 0)
            items.extend(page.get("items") or [])
        value = normalize_items(items, kind)
        payload = {
            "dataset": dataset,
            "kind": kind,
            "total": total or len(items),
            "items": items,
            "value": value,
            "updatedAt": time.time(),
        }
        self._write_disk(dataset, payload)
        self._memory[dataset] = payload
        return items

    def get_value(self, dataset: str, kind: str = "array", auto_fetch: bool = True, **query_kwargs):
        """取 dataset 归一化原始值（list/dict，与 datapack 模块值同形）。"""
        if dataset in self._memory:
            cached = self._memory[dataset]
            if "value" in cached and (cached.get("kind") or "array") == kind:
                return cached["value"]
            value = normalize_items(cached.get("items") or [], kind)
            cached["value"] = value
            cached["kind"] = kind
            return value
        cached = self._read_disk(dataset)
        if cached is not None:
            self._memory[dataset] = cached
            return self.get_value(dataset, kind, auto_fetch=False)
        if not auto_fetch:
            return [] if kind == "array" else {}
        self.fetch(dataset, kind, **query_kwargs)
        return self._memory[dataset]["value"]

    def reload(self, dataset: str, **query_kwargs) -> list:
        """强制重新拉取（丢弃内存与磁盘旧值）。"""
        return self.fetch(dataset, **query_kwargs)

    def reload_all(self, datasets: list[str] | None = None, **query_kwargs) -> dict[str, int]:
        targets = datasets if datasets is not None else self.cached_datasets()
        return {ds: len(self.fetch(ds, **query_kwargs)) for ds in targets}

    @property
    def datasets(self) -> list[str]:
        """声明过的 dataset id 列表（去重保序）。"""
        seen: dict[str, None] = {}
        for dataset in self.mapping.values():
            seen.setdefault(dataset)
        return list(seen)

    @property
    def tables(self) -> list[str]:
        """声明过的原始表名列表。"""
        return list(self.mapping)

    def preload(self, **query_kwargs) -> dict[str, int]:
        """预拉取声明过的所有模块（去重按 dataset），返回 {dataset: 条数}。"""
        out: dict[str, int] = {}
        for dataset in self.datasets:
            out[dataset] = len(self.fetch(dataset, **query_kwargs))
        return out

    def load_tables(self):
        """数据源抽象接口实现：转换为可直接给 `Engine` 用的 `GameDataTables`。

        只拉取声明时的模块列表（`self.mapping`），归一化形状与 datapack 值同形。
        """
        from .calc.gamedata import GameDataTables

        return GameDataTables({table: self.get_value(dataset, "array") for table, dataset in self.mapping.items()})

    def refresh_if_changed(self, dataset: str) -> bool:
        """磁盘文件被外部改写时重载内存，返回是否发生重载。"""
        try:
            mtime = os.path.getmtime(self.path_for(dataset))
        except OSError:
            return False
        if self._mtimes.get(dataset) != mtime:
            cached = self._read_disk(dataset)
            if cached is not None:
                self._memory[dataset] = cached
                return True
        return False

    def invalidate(self, dataset: str) -> None:
        self._memory.pop(dataset, None)
        try:
            os.remove(self.path_for(dataset))
        except OSError:
            pass
        self._mtimes.pop(dataset, None)

    def cached_datasets(self) -> list[str]:
        out = set(self._memory.keys())
        if os.path.isdir(self.cache_dir):
            for name in os.listdir(self.cache_dir):
                if name.endswith(".json"):
                    out.add(name[: -len(".json")].replace("__", ":"))
        return sorted(out)

    # ---- 内部 ----
    def _read_disk(self, dataset: str) -> dict | None:
        path = self.path_for(dataset)
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            self._mtimes[dataset] = os.path.getmtime(path)
            return payload
        except (OSError, ValueError):
            return None

    def _write_disk(self, dataset: str, payload: dict) -> None:
        path = self.path_for(dataset)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
        self._mtimes[dataset] = os.path.getmtime(path)
