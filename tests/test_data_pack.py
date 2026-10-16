"""数据包 revive/编解码往返用例（纯本地，无网络）。"""

import io
import json
import os
import tempfile
import unittest
import zipfile

import msgpack

from dna_builder_sdk.data_pack import (
    DATA_PACK_FORMAT_MAJOR,
    DataPackStore,
    compare_data_pack_versions,
    is_data_pack_major_supported,
    normalize_data_pack_major,
    revive_packed_value,
)
from dna_builder_sdk.errors import DobHttpError


def _make_pack_bytes() -> bytes:
    manifest = {"version": "t1", "modules": {}, "rag": {"zh": {"kinds": {"story": "abc"}, "count": 1}}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", '{"version":"t1","modules":{},"rag":{}}')
        zf.writestr("modules/mod.msgpack", msgpack.packb({"a": 1}))
    assert manifest["version"] == "t1"
    return buf.getvalue()


class TestRevive(unittest.TestCase):
    def test_map_set_date_undefined(self):
        value = {
            "m": {"__dnaPackType": "Map", "value": [[{"__dnaPackType": "Undefined"}, 1]]},
            "s": {"__dnaPackType": "Set", "value": [1, 2]},
            "d": {"__dnaPackType": "Date", "value": "2026-01-01T00:00:00.000Z"},
            "u": {"__dnaPackType": "Undefined"},
            "n": [1, {"x": 2}],
        }
        out = revive_packed_value(value)
        self.assertEqual(out["m"], [(None, 1)])
        self.assertEqual(out["s"], [1, 2])
        self.assertEqual(out["u"], None)
        self.assertEqual(out["n"], [1, {"x": 2}])
        self.assertTrue(hasattr(out["d"], "year"))

    def test_zip_manifest_roundtrip(self):
        raw = _make_pack_bytes()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            self.assertIn("manifest.json", zf.namelist())
            mod = msgpack.unpackb(zf.read("modules/mod.msgpack"), raw=False)
            self.assertEqual(revive_packed_value(mod), {"a": 1})


def _make_pack_bytes_with_manifest(manifest: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("modules/mod.msgpack", msgpack.packb({"a": 1}))
    return buf.getvalue()


def _install_pack(store: DataPackStore, version: str, manifest: dict) -> None:
    dest_dir = os.path.join(store.cache_dir, version)
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, "package.zip"), "wb") as fh:
        fh.write(_make_pack_bytes_with_manifest(manifest))


class TestMajor(unittest.TestCase):
    def test_format_major_positive_int(self):
        self.assertIsInstance(DATA_PACK_FORMAT_MAJOR, int)
        self.assertGreater(DATA_PACK_FORMAT_MAJOR, 0)

    def test_explicit_major_wins(self):
        self.assertEqual(normalize_data_pack_major(1, "1.7.4.1"), 1)
        self.assertEqual(normalize_data_pack_major(2, "1.1.86.1"), 2)

    def test_missing_major_inferred_by_cutoff(self):
        self.assertEqual(normalize_data_pack_major(None, "1.6.208.6"), 1)
        self.assertEqual(normalize_data_pack_major(None, "1.6.208.7"), 2)
        self.assertEqual(normalize_data_pack_major(None, "1.7.4.1"), 2)
        self.assertEqual(normalize_data_pack_major(None, None), 1)

    def test_version_compare_numeric(self):
        self.assertEqual(compare_data_pack_versions("1.6.208.7", "1.6.208.7"), 0)
        self.assertEqual(compare_data_pack_versions("1.6.208.10", "1.6.208.7"), 1)
        self.assertEqual(compare_data_pack_versions("1.7.3.1", "1.6.208.7"), 1)
        self.assertEqual(compare_data_pack_versions("1.6.208.6", "1.6.208.7"), -1)

    def test_supported_range(self):
        self.assertTrue(is_data_pack_major_supported(1, "1.6.208.6"))
        self.assertTrue(is_data_pack_major_supported(2, "1.7.4.1"))
        self.assertFalse(is_data_pack_major_supported(DATA_PACK_FORMAT_MAJOR + 1, "9.9.9.9"))
        self.assertTrue(is_data_pack_major_supported(None, "1.6.208.6"))

    def test_activate_rejects_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DataPackStore(cache_dir=tmp)
            _install_pack(store, "new", {"version": "new", "major": 99, "modules": {}})
            with self.assertRaises(DobHttpError) as ctx:
                store.activate("new")
            self.assertEqual(ctx.exception.code, "unsupported_pack")

    def test_activate_allows_legacy_without_major(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DataPackStore(cache_dir=tmp)
            _install_pack(store, "t1", {"version": "t1", "modules": {}})
            manifest = store.activate("t1")
            self.assertEqual(manifest["version"], "t1")

    def test_ensure_falls_back_to_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DataPackStore(cache_dir=tmp)
            _install_pack(store, "old", {"version": "old", "major": 1, "modules": {}})
            _install_pack(store, "new", {"version": "new", "major": 99, "modules": {}})
            manifest = store.ensure()
            self.assertEqual(store.active_version, "old")
            self.assertEqual(manifest["version"], "old")


if __name__ == "__main__":
    unittest.main()
