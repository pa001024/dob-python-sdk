"""gameData* GraphQL 封装：口径与服务端 game-data-api.md 一致，limit 上限 500。"""

from __future__ import annotations

from typing import Any

from .backend import BackendClient

MAX_LIMIT = 500
DEFAULT_LIMIT = 50


def unwrap_value(data):
    """解开原始值导出的 `{value}` 包装（对齐 gameDataRegistry 记录标准化）。"""
    if isinstance(data, dict) and set(data.keys()) == {"value"}:
        return data["value"]
    return data


def normalize_items(items: list, kind: str = "array"):
    """gameData `items[{key, data}]` 归一化为 datapack 同形的原始值。

    - array：按页顺序取 `[data...]`（元素自带 id 时 key 为 id，但顺序即原数组顺序）
    - object/map：还原为 `{key: data}`
    - 原始值（数字/字符串/数组字面量）导出统一解开 `{value}` 包装
    """
    if kind in ("object", "map"):
        return {str(item.get("key")): unwrap_value(item.get("data")) for item in items}
    return [unwrap_value(item.get("data")) for item in items]


class GameDataClient:
    def __init__(self, backend: BackendClient):
        self.backend = backend

    def modules(self) -> list:
        return (self.backend.graphql("{gameDataModules{id label file baseId locale}}").get("gameDataModules") or [])

    def sets(self, module: str | None = None) -> list:
        data = self.backend.graphql(
            "query($m:String){gameDataSets(module:$m){id exportName kind count locale}}", {"m": module}
        )
        return data.get("gameDataSets") or []

    def fields(self, dataset: str, limit: int = 200) -> list:
        data = self.backend.graphql(
            "query($d:String!,$l:Int){gameDataFields(dataset:$d,limit:$l)}", {"d": dataset, "l": limit}
        )
        return data.get("gameDataFields") or []

    def query(
        self,
        dataset: str,
        where: list[dict] | None = None,
        search: str | None = None,
        sort: list[dict] | None = None,
        fields: list[str] | None = None,
        offset: int = 0,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """单页查询，limit 自动 clamp 到 [1, 500]。"""
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        data = self.backend.graphql(
            "query($i:GameDataQuery!){gameData(input:$i){dataset total count offset limit items{key data}}}",
            {
                "i": {
                    "dataset": dataset,
                    "where": where or [],
                    "search": search,
                    "sort": sort or [],
                    "fields": fields,
                    "offset": offset,
                    "limit": limit,
                }
            },
        )
        return data.get("gameData") or {"dataset": dataset, "total": 0, "items": []}

    def record(self, dataset: str, key: str) -> dict | None:
        data = self.backend.graphql(
            "query($d:String!,$k:String!){gameDataRecord(dataset:$d,key:$k){key data}}",
            {"d": dataset, "k": key},
        )
        return data.get("gameDataRecord")

    def field_values(self, dataset: str, field: str, limit: int = 10) -> list:
        data = self.backend.graphql(
            "query($d:String!,$f:String!,$l:Int){gameDataFieldValues(dataset:$d,field:$f,limit:$l){value count}}",
            {"d": dataset, "f": field, "l": limit},
        )
        return data.get("gameDataFieldValues") or []

    def iter_all(
        self,
        dataset: str,
        where: list[dict] | None = None,
        search: str | None = None,
        sort: list[dict] | None = None,
        fields: list[str] | None = None,
        page_size: int = MAX_LIMIT,
    ):
        """按分页拉全量，逐页 yield Page 信封；total 以首页为准。"""
        offset = 0
        while True:
            page = self.query(dataset, where, search, sort, fields, offset, page_size)
            yield page
            items = page.get("items") or []
            total = int(page.get("total") or 0)
            offset += len(items)
            if not items or offset >= total:
                break

    def fetch_all(self, dataset: str, **kwargs: Any) -> list:
        """拉全量 items（大数据集慎用，优先走 ModuleStore 持久化）。"""
        out: list = []
        for page in self.iter_all(dataset, **kwargs):
            out.extend(page.get("items") or [])
        return out
