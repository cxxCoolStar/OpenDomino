# 知识库文档同步（docs → L3 检索）

## Sub-features

- 周期扫描 `docs/`（`kbsync_interval_seconds` 默认 300s）→ 规范校验 → 切块入库（FTS5 全文 + 向量双路）
- 镜像删除：源文档删了，库里的块也删
- ConsoleAgent 的 answer 可引用 L3 检索结果（【正式文档】来源标记）

## How to get to it (user POV)

用户往仓库 `docs/` 写一篇符合规范的 Markdown（ADR/设计文档），最多 5 分钟后机器人答"XX 流程是什么"时能引用它。文档违规时入库校验给警告。

## Driving it with L1 脚本

```bash
cd backend && python scripts/e2e_kbsync.py     # 同步链路
python -m app.docs.import_docs ../docs         # 文档规范校验（「规范违规」警告需处理）
```

产物：`scripts/e2e_kbsync_report.json`。

## Gotchas

- 文档规范（AGENTS.md「知识库文档规范」节）：一个 `##` 一个主题、三级标题不跳级、小节首句自包含、`###` 100~800 字——违反会触发切块警告，检索质量直接受损
- 切块以 `##` 小节为单位，heading_path 依赖标题层级
- `docs_root` 相对 backend 运行目录（默认 `../docs`），换目录跑脚本时注意 cwd
- ChromaDB 持久化目录必须隔离到临时路径（conftest.py 教训）
