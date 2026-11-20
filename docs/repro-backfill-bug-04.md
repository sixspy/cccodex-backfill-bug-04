# 复现纸 · backfill-bug-04 同片连点叠双份

## 现象

同片连点两次重放，目标表 `replay_hits` 叠出双份行，覆盖条数翻倍；幂等键只写在注释里
（`idem_gate=comment_only`、`slice_key` 唯一性从未落库），页面四处（防重队列 / 片键详情 /
放行卡片 / 覆盖附页）都顺着注释口径核对，因此看不出问题。主链同一片二次重放也返回 200。

## 根因

1. `replay_hits(slice_key)` 无唯一约束，`replay_once` 仅在 `idem_gate=unique_key` 时才判重，
   而播种值是 `comment_only` —— 防重键只改了**注释文案**，没改**写库**。
2. `replay_slice` 允许 `已落库` 切片再次重放，目标表按片叠行。
3. 重放没拿写锁，两名值班并发时缺锁会双写片键行。

## 复现（修复前，旧快照 `99bd1b0`）

```bash
git stash            # 或 git checkout 99bd1b0 -- app static tests
python -m pytest tests/test_main_chain.py -q      # FAILED: assert 200 == 409
# 页面：防重队列连点“纠偏重放同片”三次 → 覆盖附页条数 3，目标表 3 行
```

## 修复（本分支）

- 建库即补 `CREATE UNIQUE INDEX ux_replay_hits_slice_key ON replay_hits(slice_key)`；
  旧库若已有双份行，先折叠进只读历史 `replay_rejects` 再建索引。
- 收口只选一边：**先到留目标表，后到进只读历史并拒再点**；目标表恒为一行。
- 片键唯一与放行门槛同源：唯一索引存在 且 `slice_key_live == slice_key_gate`；
  否则双键即拦，返回 409 且不落库。
- 重放包在 `BEGIN IMMEDIATE` 内，并发连点只成功一次；失败吐短句“片键已存在”。
- 主链 `replay_slice` 对已落库切片直接 409。

## 验收步骤

```bash
python -m pytest -q        # 9 passed
```

| 步骤 | 期望 |
|------|------|
| 防重队列点“纠偏重放同片” ×1 | 放行，放行号 `P0001`，覆盖条数 1 |
| 再点 ×2（共三次） | 409 · 防重芯片亮，覆盖条数仍 1，后到 2 行进只读历史 |
| 纠偏后第四次点 | 同上；防重键与覆盖附页同屏核对（顶部条带），`条数翻倍 否` |
| 片键详情 | `唯一片键索引 ux_replay_hits_slice_key · 同源 是` |
| 放行卡片 | `放行` 芯片 + `放行号 P0001` |
| 主链同一片二次重放 | 409 “该片已落库，拒再点”，覆盖对账 `已落库 1 / 缺口 2` |
| 两名值班并发点重放（`test_concurrent_replay_writes_one_row`） | 目标表 1 行 |
| 造双键不同源（`slice_key_gate=S9`） | 409 · `双键拦截`，目标表 0 行 |

## 别只改文案

只把 `idem_note` / `idem_gate` 改成“唯一键”而不在建库时落唯一索引、不改写入路径，复验仍失败：
`test_legacy_duplicates_collapse_on_init` 与 `test_guard_is_same_source_as_unique_index` 会直接打回。
