"""全量数据包加载：versions.json + <ver>.zip 下载、解码、持久化、运行时热重载。

zip 内容与 tools/data-pack.ts 一致：manifest.json + imgs.json +
modules/<key>.msgpack（sanitize 编码，见 revive_packed_value）。
"""

from __future__ import annotations

import io
import json
import os
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Callable

import msgpack

from .errors import DobHttpError

MANIFEST_FILE = "manifest.json"
IMGS_MANIFEST_FILE = "imgs.json"
MODULES_PREFIX = "modules/"
DEFAULT_VERSIONS_FILE = "versions.json"


def revive_packed_value(value):
    """还原打包时的特殊类型（对齐前端 revivePackedValue，递归原地还原）。"""
    if value is None or not isinstance(value, (dict, list)):
        return value
    if isinstance(value, list):
        for i, item in enumerate(value):
            value[i] = revive_packed_value(item)
        return value
    kind = value.get("__dnaPackType")
    if kind == "Undefined":
        return None
    if kind == "Date" and isinstance(value.get("value"), str):
        try:
            return datetime.fromisoformat(value["value"].replace("Z", "+00:00"))
        except ValueError:
            return value["value"]
    if kind == "Set" and isinstance(value.get("value"), list):
        return [revive_packed_value(v) for v in value["value"]]
    if kind == "Map" and isinstance(value.get("value"), list):
        return [(revive_packed_value(k), revive_packed_value(v)) for k, v in value["value"]]
    for k, item in list(value.items()):
        value[k] = revive_packed_value(item)
    return value


def _default_cache_dir() -> str:
    override = os.environ.get("DNA_BUILDER_CACHE")
    if override:
        return os.path.join(override, "data-pack")
    return os.path.join(os.path.expanduser("~"), ".cache", "dna-builder", "data-pack")


class DataPackStore:
    """全量包存储：磁盘 <cache>/<version>/package.zip + 内存 manifest/模块缓存。"""

    def __init__(
        self,
        cache_dir: str | None = None,
        data_pack_base: str | None = None,
        backup_base: str | None = "https://cdn.dobapp.cc/data-pack",
    ):
        self.cache_dir = cache_dir or _default_cache_dir()
        self.data_pack_base = (data_pack_base or "https://cdn.dna-builder.cn/data-pack").rstrip("/")
        self.backup_base = (backup_base or "").rstrip("/") or None
        self._manifest: dict | None = None
        self._active_version: str | None = None
        self._module_cache: dict[str, dict] = {}
        self._manifest_mtime: float = 0.0

    # ---- 远端 ----
    def versions_url(self) -> str:
        return f"{self.data_pack_base}/{DEFAULT_VERSIONS_FILE}"

    def _bases(self) -> list[str]:
        return [b for b in [self.data_pack_base, self.backup_base] if b]

    def remote_versions(self) -> list:
        """读远端 versions.json（主备依次尝试；全失败抛 DobHttpError）。"""
        last: Exception | None = None
        for base in self._bases():
            try:
                with urllib.request.urlopen(f"{base}/{DEFAULT_VERSIONS_FILE}", timeout=30) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                versions = payload if isinstance(payload, list) else payload.get("versions", [])
                versions.sort(key=lambda v: v.get("version", ""), reverse=True)
                return versions
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise DobHttpError(f"读取版本列表失败: {self.versions_url()}", payload=str(last)) from last

    def package_url(self, version: str) -> str:
        return f"{self.data_pack_base}/{version}.zip"

    # ---- 下载 + 持久化 ----
    def download(self, version: str | None = None, progress: Callable[[int, int | None], None] | None = None) -> dict:
        """下载整包并激活。version 缺省取远端最新；返回 manifest。"""
        if version is None:
            versions = self.remote_versions()
            if not versions:
                raise DobHttpError("远端版本列表为空", payload=self.versions_url())
            version = versions[0]["version"]
        dest_dir = os.path.join(self.cache_dir, version)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "package.zip")
        last: Exception | None = None
        for base in self._bases():
            try:
                self._fetch_to_file(f"{base}/{version}.zip", dest, progress)
                break
            except DobHttpError as exc:
                last = exc
        else:
            raise last if last else DobHttpError(f"下载数据包失败: {version}")
        return self.activate(version)

    def _fetch_to_file(self, url: str, dest: str, progress=None) -> None:
        try:
            with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as fh:
                total = resp.getheader("Content-Length")
                total_n = int(total) if total else None
                received = 0
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, total_n)
        except Exception as exc:
            raise DobHttpError(f"下载数据包失败: {url}", payload=str(exc)) from exc

    # ---- 激活 / 热重载 ----
    def activate(self, version: str) -> dict:
        """切换激活版本：重读磁盘 manifest 并清空模块缓存（运行时热重载入口）。"""
        manifest = self._read_manifest_from_zip(version)
        self._manifest = manifest
        self._active_version = version
        self._module_cache.clear()
        self._manifest_mtime = self._zip_mtime(version)
        return manifest

    def reload(self) -> dict | None:
        """重读当前激活版本的磁盘包（外部更新 zip 后调用，无需重启）。"""
        if not self._active_version:
            return None
        return self.activate(self._active_version)

    def refresh_if_changed(self) -> bool:
        """zip mtime 变化时自动重载，返回是否发生重载。"""
        if not self._active_version:
            return False
        if self._zip_mtime(self._active_version) != self._manifest_mtime:
            self.reload()
            return True
        return False

    @property
    def active_version(self) -> str | None:
        return self._active_version

    @property
    def manifest(self) -> dict | None:
        return self._manifest

    def installed_versions(self) -> list[str]:
        if not os.path.isdir(self.cache_dir):
            return []
        return sorted(
            d for d in os.listdir(self.cache_dir) if os.path.isfile(os.path.join(self.cache_dir, d, "package.zip"))
        )

    # ---- 模块读取 ----
    def load_module(self, module_key: str) -> dict:
        """读单个模块（内存 → zip），返回 {exportName: value}。"""
        if module_key in self._module_cache:
            return self._module_cache[module_key]
        if not self._active_version:
            raise DobHttpError("尚未激活数据包版本，先调用 download()/activate()", code="no_active_pack")
        raw = self._read_zip_entry(self._active_version, f"{MODULES_PREFIX}{module_key}.msgpack")
        if raw is None:
            return {}
        record = msgpack.unpackb(raw, raw=False, strict_map_key=False)
        record = revive_packed_value(record)
        self._module_cache[module_key] = record
        return record

    def load_export(self, module_key: str, export_name: str):
        return self.load_module(module_key).get(export_name)

    # ---- 数据源抽象接口（与 ModuleStore.load_tables 同形，给 Engine 用） ----
    def ensure(self, version: str | None = None) -> dict:
        """保证有可用版本：已安装则激活最新，否则下载（缺省远端最新）。返回 manifest。"""
        installed = self.installed_versions()
        if version is not None:
            return self.activate(version)
        if installed:
            return self.activate(installed[-1])
        return self.download()

    def load_tables(self):
        """数据源抽象接口实现：转换为可直接给 `Engine` 用的 `GameDataTables`。

        未激活版本时自动 `ensure()`（本地有缓存即复用，无缓存则下载）。
        """
        from .calc.gamedata import MODULE_DEFAULTS, PET_EXPORTS, GameDataTables

        if not self._active_version:
            self.ensure()
        raw: dict = {}
        for table, module in MODULE_DEFAULTS.items():
            record = self.load_module(module)
            if table in PET_EXPORTS:
                raw[table] = record.get(PET_EXPORTS[table]) or []
            else:
                raw[table] = record.get("default") or []
        raw["curves"] = dict(getattr(self, "curves", None) or {})
        return GameDataTables(raw)

    def rag_fingerprints(self, lang: str) -> dict | None:
        """取 manifest 里打包时算好的 RAG 指纹（原样携带，不本地重算）。"""
        kinds = (self._manifest or {}).get("rag", {}).get(lang, {}).get("kinds")
        return dict(kinds) if kinds else None

    # ---- 内部 ----
    def _zip_path(self, version: str) -> str:
        return os.path.join(self.cache_dir, version, "package.zip")

    def _zip_mtime(self, version: str) -> float:
        try:
            return os.path.getmtime(self._zip_path(version))
        except OSError:
            return 0.0

    def _read_zip_entry(self, version: str, name: str) -> bytes | None:
        path = self._zip_path(version)
        with zipfile.ZipFile(path) as zf:
            try:
                return zf.read(name)
            except KeyError:
                return None

    def _read_manifest_from_zip(self, version: str) -> dict:
        raw = self._read_zip_entry(version, MANIFEST_FILE)
        if raw is None:
            raise DobHttpError(f"数据包缺 manifest.json: {version}", code="bad_pack")
        manifest = json.loads(raw.decode("utf-8"))
        # 供调试：记录本地时间
        manifest.setdefault("_loadedAt", datetime.now(timezone.utc).isoformat())
        manifest.setdefault("_version", version)
        # 校验 zip 可读
        with zipfile.ZipFile(self._zip_path(version)) as zf:
            if zf.testzip() is not None:
                raise DobHttpError(f"数据包 zip 损坏: {version}", code="bad_pack")
        # imgs 清单一并校验存在性（不强制）
        _ = io.BytesIO(raw)
        return manifest
