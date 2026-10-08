"""数据包 revive/编解码往返用例（纯本地，无网络）。"""

import io
import unittest
import zipfile

import msgpack

from dna_builder_sdk.data_pack import DataPackStore, revive_packed_value


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


if __name__ == "__main__":
    unittest.main()
