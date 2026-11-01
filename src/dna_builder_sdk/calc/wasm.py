"""wasm 计算后端：把 `dob-calc-core` 编译产物直接嵌入 Python 进程。

与纯 Python `Engine` 的分工：数据加载（`DataPackStore` / `ModuleStore` /
`GameDataTables`）仍走 Python，只把最热的「装配 + 属性 + 面板 + 目标函数」
求值 offload 到 wasm（wasmtime 即时实例化，无 imports，空环境可跑）。

等价性：7 个线上真实 BD（target + 全量 attrs/panels + 全部自定义变量）与
42 个 shard 表达式在容差内逐位一致（见 tests/test_wasm_parity.py）；
批量实测约 15 倍加速（见 tests/test_calc_perf.py 注释）。

线程模型：每个 `WasmBackend` 拥有独立 wasmtime `Store`，不可跨线程共享；
多线程请每线程各建一个 `Engine`。
"""

from __future__ import annotations

import json
import struct

_WASM_PACKAGE_VERSION = "dob-calc-core 0.2.2"


class WasmEvaluateError(RuntimeError):
    """wasm 求值返回 `ok:false` 信封（请求非法/装配缺资源等）。"""


def is_available() -> bool:
    """wasmtime 是否可导入（wasm 后端的前置条件）。"""
    try:
        import wasmtime  # noqa: F401
    except ImportError:
        return False
    return True


def _require_wasmtime():
    try:
        import wasmtime

        return wasmtime
    except ImportError as error:
        raise ImportError(
            "wasm 计算需要 wasmtime：pip install wasmtime（或 pip install dna-builder-sdk）"
        ) from error


def load_wasm_bytes() -> bytes:
    """读取随包发布的 wasm 二进制（与 `_WASM_PACKAGE_VERSION` 对应）。"""
    try:
        from importlib.resources import files
    except ImportError:  # Python < 3.9（本包要求 >=3.10，到不了）
        from importlib_resources import files  # type: ignore[no-redef]

    data = (files("dna_builder_sdk") / "_wasm" / "dob_calc_core.wasm").read_bytes()
    if not data:
        raise WasmEvaluateError("随包 wasm 为空，请重新安装 dna-builder-sdk")
    return data


def tables_to_modules_json(tables) -> bytes:
    """`GameDataTables` → Rust `ModulesJson` 口径的模块 JSON（UTF-8 编码）。

    键映射见 `calc/gamedata.py`（datapack 模块键）→ Rust `session.rs#ModulesJson`；
    effects 取 MOD/武器特效合集（与 Rust `TableBuilder` 的 effect 索引同源）。
    """
    raw = tables.raw if hasattr(tables, "raw") else {}
    modules = {
        "char.data": list(raw.get("chars") or []),
        "mod.data": list(raw.get("mods") or []),
        "weapon.data": list(raw.get("weapons") or []),
        "buff.data": list(raw.get("buffs") or []),
        "effect.data": [e for e in (raw.get("effects") or []) if e],
        "monster.data": [m for m in (raw.get("monsters") or []) if m],
        "pet.data": list(raw.get("pets") or []),
        "petTraits": list(raw.get("pet_entries") or []),
    }
    return json.dumps(modules, ensure_ascii=False).encode("utf-8")


class WasmBackend:
    """一次 wasm 会话：建表一次，多次求值（`calc` 会话复用装配）。

    :param tables: `GameDataTables`（`build_state` 用的同一份表）
    """

    def __init__(self, tables) -> None:
        wasmtime = _require_wasmtime()
        self._wasmtime = wasmtime
        self._store = wasmtime.Store()
        module = wasmtime.Module(self._store.engine, load_wasm_bytes())
        instance = wasmtime.Linker(self._store.engine).instantiate(self._store, module)
        exports = instance.exports(self._store)
        self._mem = exports["memory"]
        self._alloc = exports["dobcc_alloc"]
        self._dealloc = exports["dobcc_dealloc"]
        self._open = exports["dobcc_open_session"]
        self._evaluate = exports["dobcc_evaluate"]
        self._close = exports["dobcc_close_session"]
        self._calc_open = exports["dobcc_calc_open"]
        self._calc_evaluate = exports["dobcc_calc_evaluate"]
        self._calc_close = exports["dobcc_calc_close"]
        self._skill_fields = exports["dobcc_skill_fields"]
        self._panels = exports["dobcc_panels"]
        blob = tables_to_modules_json(tables)
        ptr = self._call_alloc(len(blob))
        self._write(ptr, blob)
        self._session = self._open(self._store, ptr, len(blob))
        self._call_dealloc(ptr, len(blob))
        if not self._session:
            raise WasmEvaluateError("wasm 建表失败（open_session 返回 0）")
        self._calc = self._calc_open(self._store, self._session)
        if not self._calc:
            raise WasmEvaluateError("wasm 计算会话创建失败（calc_open 返回 0）")
        self._closed = False

    # ---------- 内存原语 ----------

    def _write(self, ptr: int, data: bytes) -> None:
        self._mem.write(self._store, data, ptr)

    def _read(self, ptr: int, size: int) -> bytes:
        return bytes(self._mem.read(self._store, ptr, ptr + size))

    def _call_alloc(self, size: int) -> int:
        return self._alloc(self._store, size)

    def _call_dealloc(self, ptr: int, size: int) -> None:
        if ptr:
            self._dealloc(self._store, ptr, size)

    def _call(self, func, session: int, payload: bytes) -> dict:
        """调 `dobcc_*evaluate` 系函数，返回解包后的信封 dict。"""
        req_ptr = self._call_alloc(len(payload))
        self._write(req_ptr, payload)
        len_ptr = self._call_alloc(8)
        self._write(len_ptr, b"\x00" * 8)
        try:
            out_ptr = func(self._store, session, req_ptr, len(payload), len_ptr)
            (out_len,) = struct.unpack_from("<I", self._read(len_ptr, 8))
            out = self._read(out_ptr, out_len) if out_ptr and out_len else b""
        finally:
            self._call_dealloc(req_ptr, len(payload))
            self._call_dealloc(len_ptr, 8)
        if out_ptr:
            self._call_dealloc(out_ptr, out_len)
        if not out:
            raise WasmEvaluateError("wasm 返回空结果")
        try:
            return json.loads(out.decode("utf-8"))
        except ValueError as error:
            raise WasmEvaluateError(f"wasm 结果解析失败: {error}") from error

    # ---------- 求值 ----------

    def evaluate(self, char_id: int, settings: dict, target: str | None = None) -> dict:
        """求值一份构筑，返回结果 `result` 段（attributes/weapon/targetValue/…）。

        :param target: 覆盖 `settings["targetFunction"]` 的单次表达式
        :raises WasmEvaluateError: 信封 `ok:false` 时抛出（信息透出）
        """
        payload = dict(settings or {})
        if target is not None:
            payload["targetFunction"] = target
        request = json.dumps({"charId": char_id, "settings": payload}, ensure_ascii=False).encode("utf-8")
        envelope = self._call(self._calc_evaluate, self._calc, request)
        if not envelope.get("ok"):
            raise WasmEvaluateError(str(envelope.get("error") or "wasm 求值失败"))
        return envelope.get("result") or {}

    def skill_fields(self, char_id: int, settings: dict, skill: str) -> list:
        """技能结算字段表（`skill` 按名称或 safeName 匹配；未知返回 []）。"""
        payload = json.dumps(
            {"charId": char_id, "settings": dict(settings or {}), "skill": skill},
            ensure_ascii=False,
        ).encode("utf-8")
        envelope = self._call(self._skill_fields, self._session, payload)
        if not envelope.get("ok"):
            raise WasmEvaluateError(str(envelope.get("error") or "wasm 求值失败"))
        return (envelope.get("result") or {}).get("fields") or []

    def panels(self, char_id: int, settings: dict) -> dict:
        """全槽武器面板（键集与上游 `weapon_panels` 一致）。"""
        payload = json.dumps(
            {"charId": char_id, "settings": dict(settings or {})}, ensure_ascii=False
        ).encode("utf-8")
        envelope = self._call(self._panels, self._session, payload)
        if not envelope.get("ok"):
            raise WasmEvaluateError(str(envelope.get("error") or "wasm 求值失败"))
        return (envelope.get("result") or {}).get("panels") or {}

    def close(self) -> None:
        """释放计算会话与表会话（重复关闭安全）。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self._calc:
                self._calc_close(self._store, self._calc)
        finally:
            self._calc = 0
            if self._session:
                self._close(self._store, self._session)
            self._session = 0

    def __del__(self):  # noqa: D105
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
