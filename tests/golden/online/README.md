# 线上靶点（`online/`）

真实分享构筑的 parity 靶点，覆盖本地 fixture 碰不到的分支：
`modVariants`（多变体激活）、`dotSettings`（归一化合并）、`effectConfig`
（特效等级）、旧格式数字 `charSkillLevel`、空武器槽（id 0）、`useGlobal` 缺省。

- `online/<buildId>.json`：BD 原文（`charId` + `settings`）+ provenance（meta）
- `../online_data.json`：union 原始表（引擎的唯一数据输入）
- `../online_expected.json`：真机 target + attrs + panels（期望值）

## 复现链（需联网 + bun）

```bash
# 1. 拉取（特性 scoring，最多 8 个，每特性至少 2 靶点）
PYTHONPATH=sdk/python/src python .tmp/pull-online-targets.py
# 2. 提 union 原始表
bun .tmp/extract-online-data.ts
# 3. 真机期望
bun .tmp/dump-online-expected.ts
# 4. 验证
PYTHONPATH=sdk/python/src python -m unittest discover -s sdk/python/tests -p "test_online_targets.py"
```

说明：

- `DOT伤害` 表达式靶点在拉取时过滤（DOT 引擎超出本 SDK 范围）；
  Svmqw3LGoY（含 DOT 链）按变量依赖分流断言，非 DOT 部分全覆盖。
- `Svmqw3LGoY`（琳恩裂伤特性，`技能=裂伤` + `code`）专测条件 BUFF 三条路径：
  不进全局汇总/全局 code、不进乘法聚合、经差分进字段级合并——由 `[引爆]单次` 精确命中证明。
- `DOT伤害` 表达式靶点在拉取时过滤（DOT 引擎超出本 SDK 范围）。
- `charSkillLevel` 归一化含一个易错点：`normalizeCharSettings` 的
  `charSkillLevel` 行读**原始** settings（绕过类型门合并），SDK 同口径。
- 空武器（id 0）为零属性占位，`teamWeaponCategories` 计 `长柄`。
