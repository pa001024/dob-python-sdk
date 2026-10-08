"""gameData ↔ datapack 形状归一化用例（纯本地，无网络）。"""

import os
import tempfile
import unittest

from dna_builder_sdk.game_data import normalize_items
from dna_builder_sdk.module_store import ModuleStore


class FakeGameData:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def iter_all(self, dataset, page_size=500, **kwargs):
        self.calls += 1
        yield from self.pages


class TestNormalize(unittest.TestCase):
    def test_array_keeps_order(self):
        items = [{"key": "1101", "data": {"id": 1101, "名称": "a"}}, {"key": "1102", "data": {"id": 1102}}]
        self.assertEqual(normalize_items(items), [{"id": 1101, "名称": "a"}, {"id": 1102}])

    def test_object_shape(self):
        items = [{"key": "a", "data": {"x": 1}}, {"key": "b", "data": {"x": 2}}]
        self.assertEqual(normalize_items(items, "object"), {"a": {"x": 1}, "b": {"x": 2}})

    def test_value_wrapper(self):
        items = [{"key": "0", "data": {"value": 5}}, {"key": "1", "data": {"value": "s"}}]
        self.assertEqual(normalize_items(items), [5, "s"])

    def test_store_value_roundtrip(self):
        pages = [{"total": 2, "items": [{"key": "1", "data": {"id": 1}}, {"key": "2", "data": {"id": 2}}]}]
        with tempfile.TemporaryDirectory() as tmp:
            store = ModuleStore(FakeGameData(pages), cache_dir=tmp)
            self.assertEqual(store.get_value("mod"), [{"id": 1}, {"id": 2}])
            # 新实例只读磁盘，不再请求远端
            store2 = ModuleStore(FakeGameData([]), cache_dir=tmp)
            self.assertEqual(store2.get_value("mod", auto_fetch=False), [{"id": 1}, {"id": 2}])
            # items 原样保留，value 同形
            payload = store2._memory["mod"]
            self.assertEqual(len(payload["items"]), 2)
            self.assertEqual(payload["kind"], "array")

    def test_store_object_kind(self):
        pages = [{"total": 1, "items": [{"key": "k", "data": {"v": 1}}]}]
        with tempfile.TemporaryDirectory() as tmp:
            store = ModuleStore(FakeGameData(pages), cache_dir=tmp)
            self.assertEqual(store.get_value("cfg", kind="object"), {"k": {"v": 1}})


if __name__ == "__main__":
    unittest.main()
