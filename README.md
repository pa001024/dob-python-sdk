# dna-builder-sdk

dna-builder 后端服务 + 数据包 + 纯表达式计算的 Python SDK。

- **后端**：只覆盖无需登录的公开读接口（评论 / 构筑 / 攻略 / 时间轴 / 榜单 / 脚本 / MOD / DPS / `gameData*`）。写接口、登录态、`admin`、`shop` 私有接口一律不在本包。
- **数据双加载（同一抽象接口）**：全量 datapack 下载（`versions.json` + `<ver>.zip`），或按模块按需经 `gameData` GraphQL 分页拉取；两者都实现 `TableSource.load_tables()` → `GameDataTables`，上层（`Engine` / `snapshot_from_online` / 示例）只依赖该接口。磁盘持久化并支持运行时热重载。
- **计算**：纯 BD JSON（CharSettings + charId）进，目标值出。attrs/面板/技能表/伤害乘区
  全由引擎自算（实体等级化→加成汇总→条件/code 不动点→武器面板→技能结算链），
  真实 BD 42 个表达式与 bun 真机逐位一致。不含编辑语义（无 AutoBuild、无配装寻优）。

```python
from dna_builder_sdk import BackendClient, GameDataClient, DataPackStore, ModuleStore

backend = BackendClient()  # 默认 https://api.dna-builder.cn，本地联调传 http://localhost:8887
gamedata = GameDataClient(backend)
print(gamedata.modules())

# 数据源抽象接口：两者都实现 load_tables() → GameDataTables（直接给 Engine 用）
pack = DataPackStore()
pack.ensure()  # 本地有缓存即激活最新，无缓存则下载远端最新
print(pack.load_module("char").keys())

# 按需通道：模块列表在声明时确定（缺省 8 张全量表），load_tables() 只拉这些表
store = ModuleStore(gamedata, modules=["chars", "mods", "weapons", "buffs", "effects"])
print(store.tables, store.datasets)
store.preload()  # 可选：一次性预拉取声明过的模块
store.reload("mod")

# 计算：纯 BD JSON 进，目标值出（表经抽象接口来，测试用最小 fixture）
from dna_builder_sdk.calc import Engine, GameDataTables, build_state

tables = pack.load_tables()    # 或 store.load_tables()，或 GameDataTables.from_dict({...})
engine = Engine(build_state(char_id, settings, tables), tables)
engine.calculate()  # 目标函数值；engine.calculate("[若华纯蓄]DPS") 指定表达式

# 面板与技能：指定等级/MOD/技能等级/溯源
engine.char_panel()                    # 角色面板（全量角色属性）
engine.weapon_panel("melee")           # 武器面板（melee/ranged/skill 或技能名）
engine.skill_levels_final()            # 最终技能等级（含 trio + 溯源加成，钳制 1-12）
engine.skill_fields("以坚忍之名")      # 技能字段面板数值
# 技能等级 trio（E/Q/P）与溯源等级（单数；7 即含 1-7 节点效果）
engine2 = Engine(build_state(cid, settings, tables, skill_levels=[10, 10, 10], traces=7), tables)

# 在线快照：bdid → 线上 JSON → 完整快照（表达式/属性/面板/技能表）
# 快照与 Engine 走同一条数据通路：传抽象数据源，不再另分支 data_source
from dna_builder_sdk import snapshot_from_online, snapshot_from_settings

snapshot = snapshot_from_online("Svmqw3LGoY", source=pack)   # 全量通道
snapshot = snapshot_from_online("Svmqw3LGoY", source=store)  # 或按需通道
snapshot.calculate()        # 目标函数值
snapshot.evaluate("角色::攻击!")  # 任意表达式
snapshot.char_panel()       # 角色面板
snapshot.summary()          # 一页总览
# 注：线上实时数据随版本更新，结果跟踪数据版本；回归测试用钉住版本的 fixture。

# 示例：sdk/python/examples/snapshot.py、sdk/python/examples/panels.py
```

更多接口见各模块 docstring。缓存根目录可用 `DNA_BUILDER_CACHE` 覆盖。

## 发版（GitHub Actions → PyPI，无 token）

本仓用 Trusted Publishing：打 tag 即发版。

```bash
git tag v0.1.0 && git push origin v0.1.0
```

- 工作流 `.github/workflows/publish.yml`：先跑全量单测，再校验 tag 与
  `pyproject.toml` 版本一致，最后 `build` + 上传。
- 只需在 PyPI 上配一次：在账号设置 → Publishing 里加 **pending publisher**
 （owner `pa001024` / repo `dob-python-sdk` / workflow `publish.yml`），
  首次上传会自动创建 `dna-builder-sdk` 项目。之后如需轮换，可改用限定项目的作用域。

## 测试报告

运行（仓库根目录，纯本地，无网络）：

```bash
PYTHONPATH=sdk/python/src python -m unittest discover -s sdk/python/tests
```

52 tests 全过（含 4 个性能基线）。测试清单：

| 文件 | 覆盖 |
|---|---|
| `test_ast_evaluate.py` | 表达式 AST 解析与求值 |
| `test_data_pack.py` | 数据包 revive/编解码往返 |
| `test_gamedata_shape.py` | gameData ↔ datapack 形状归一化 |
| `test_jscode_corpus.py` | 14 段真实 BUFF code 可解析可执行 |
| `test_shardbuild_parity.py` | 纯 BD JSON：42 表达式 + 全量 attrs/panels 与 bun 真机逐位一致 |
| `test_online_targets.py` | 6 个线上真实 BD 的变量/target/attrs parity（DOT 分流跳过） |
| `test_skill_panels.py` | 技能等级 trio + 溯源 + 字段面板 |
| `test_snapshots.py` | 在线快照装配（fake 后端 + fixture 表） |
| `test_table_source.py` | 抽象数据源：模块声明、`load_tables()`、快照经 Engine 通路 |
| `test_datapack_perf.py` | 真实包加载性能基线（`mock/data-pack/1.6.208.6.zip` 钉住版本） |
| `test_calc_perf.py` | fixture BD 批量计算性能基线（online 六连批 + shard 42 式） |

### 表名映射核验（`calc/gamedata.py` 三处常量，均已对真实包 + 前端 + 服务端验过）

- `MODULE_DEFAULTS`：7 个 datapack 模块（`char/mod/buff/effect/weapon/pet/monster.data`）
  在真实包内全部存在且皆有 `default` 导出，条数与基线锚点一致。
- `PET_EXPORTS`：`pets → default`（152 条，对齐前端 `import petData from "./pet.data"`；
  曾误写 `petData` 导致魔灵表为空，已修正，Go SDK 同步改）、`pet_entries → petEntrys`（73 条）。
- `DATASET_DEFAULTS`：8 张表 → gameData 数据集 id（`char/mod/buff/effect/weapon/pet/monster` +
  具名 `pet:petEntrys`），与服务端 `gameDataRegistry` 的模块/数据集 id 逐项一致。

### 性能基线（best 实测，供后人对比；预算只防病态劣化，约实测百倍）

| 基线 | best | 预算 | 锚点 |
|---|---|---|---|
| `activate(1.6.208.6)`（manifest + 全包 zip 校验，23MB） | ~166ms（best-of-3） | 60s | manifest.version 吻合 |
| `load_tables()`（8 表 msgpack 解码 + 索引装配） | ~27ms（best-of-3） | 60s | 8 表条数 `33/586/187/65/71/152/73/393` |
| online 六 BD 一批（装配 + 目标值 + 全量属性） | ~20ms（best-of-5） | 5s | 目标值 + 全量 attrs 对 `online_expected.json` |
| shard 一批（42 表达式 + 全量属性） | ~56ms（best-of-3，~750 expr/s） | 10s | 逐式对 `shardBuild1_expr.json` 真机值 |

以上数为开发机实测（Windows / Python 3.14），换机器只对比量级；基线方法（checksum
锚点先行、best 取 min、每轮等价断言）见技能 `.agents/skills/perf-hotspot-profiling/SKILL.md`。
