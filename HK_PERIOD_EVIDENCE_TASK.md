# RF-HK-PERIOD-EVIDENCE-001 · 業績公告标题判期回填（v1.0.4）

日期：2026-09-26。状态：**✅ 已实现**（方案 A，用户 2026-09-26 拍板；B/OCR 与 C/排序改动不采用）。实现要点：证据随 DiscoveryResult 传递，core 阶段 C 兜底（document > announcement_title 优先级）；375 单测 + 37 integration-kit 全绿；生产 1810 验证见 DEPLOY.md v1.0.4 记录。

## 背景与根因（生产排查留证）

- 现象：生产（v1.0.3）`01810`（小米）`last_n=10` 只返回季度业绩公告，最新为 Q1（期末 2026-03-31）；2026-09-23 发布的《2026年中期報告》与历年 ANNUAL/INTERIM 全部"判期预取失败"被挤出（任务 `job_1b1dd12d950b461ebfa8` 结果含 11 份候选的遗漏警告）。
- 根因：小米历年定期报告 PDF 正文字体缺 ToUnicode 映射，pdfminer 文本层恒为 `(cid:xxxx)`，`document_period` 提取恒 None；同 stem 英文版不存在（404）。文档判期路径对该公司结构性失效（探针：`2026092300297_c.pdf` 第 9 页可见 `2026(cid:11830)6(cid:14026)30(cid:13735)` 即"2026年6月30日"但不可还原）。
- 未利用的可靠证据：t1=10000+title=業績 检索中的 **[中期業績]/[末期業績] 公告标题自带明确期末日**（如 1810 的《截至2026 年6 月30 日止三個月及六個月之業績公告》，2026-08-18 发布）——可提取文本、与字体无关。

## 方案（A）

1. HK 适配器在 t1=10000 業績检索中额外收集判期证据：子类别含 `中期業績` → INTERIM 证据；含 `末期業績` → ANNUAL 证据；标题含"截至…止"完整明确日期即入证据（复用现有 explicit 正则）。
2. t1=40000 的 ANNUAL/INTERIM 候选：标题解析期末未知且为**单年标签**时，按 `(doc_type, 年份)` 匹配证据；**仅当该键证据期末唯一**时回填报告期。
3. 语义与可追溯：`period_source=announcement_title`（新增枚举值），`source_metadata.period_evidence={source_id,title,subcategory}` 记录证据来源公告。
4. 不做的事（明确边界）：
   - 跨年标签（2024/25）不回填（年份对不上，保持 unknown + 警告）；
   - 证据歧义（同期多条不同期末）不回填；
   - **業績公告本身不作为归档候选**（ANNUAL/INTERIM 仍只取 t1=40000 完整报告；业绩公告仅作判期证据）——公告窗口期（業績已发、完整报告未发）仍取不到中报，属已知边界；
   - 不引入 OCR、不改"未知期置后"选择契约。

## 附带修复（可观测性）

`_prepare_selection_periods` 判期提取返回 None 的候选此前无任何日志行（本次生产排查 0 条 WARNING 即因此）；补一条聚合 WARNING（数量 + 前若干 source_id）。

## 验收

1. 单测：证据解析（真实标题形态含空格变体/复合子类别）、回填成功（含 metadata/period_source）、歧义不回填、跨年不回填、`forms` 无 QTR-HK 时仍采集证据（QTR 候选不入）、混合排序中回填后的中报按已知期排到 QTR 之前。
2. 全量测试绿；integration-kit 通过。
3. 生产（v1.0.4）：`01810 last_n=10` 返回含《2026年中期報告》（report_period=2026-06-30，period_source=announcement_title）且排序第一；历年 ANNUAL/INTERIM 同样获得报告期；日志出现判期失败 WARNING（针对仍无法判期的候选，如 ESG 之外的其他形态）。
