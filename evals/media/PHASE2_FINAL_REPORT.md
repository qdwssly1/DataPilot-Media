# DataPilot-Media Phase 2 Final Report

本轮完成了三个既定问题的真实验证。A、C 通过纯知识回答核验；B 在 Pro Planner + Pro Reviewer 配置下完成完整工作流，但人工事实核验未通过。因此不能将本次结果标记为“Phase 2 全部验收通过”。

完整任务、生成 SQL、SQL 行结果、Reviewer 决策、Analyst 输出、Final Answer 和失败运行均保存在 [phase2_verification_results.json](phase2_verification_results.json)。没有创建 commit，没有进入 Phase 3。

## 1. 中断恢复情况

保留并检查现有 dirty working tree；没有 reset、checkout、clean 或重建 Phase 2。恢复时已有 corpus、retrieval、Agent 集成、Golden Set 与测试。本轮根据明确授权执行真实 DeepSeek 验证，并补充 LIMITATION 输出要求、验证脚本的调用分类与模型配置、结果报告。

## 2. Architecture

当前实现先进行只读知识检索和 Wren capability context 加载，将知识与 schema 分别提供给 Planner；随后保留既有 Dispatcher → SQL Agent → Reviewer → Analyst → Final Answer → success-only Session Memory 流程。知识检索不是新 Task 类型。E2E runner 复用实际 Core 组件及调度函数。

CLI 从当前 Wren project 的 knowledge 目录发现语料；本次 E2E runner 显式使用同一 Media corpus。检索结果存入独立的 knowledge_evidence，SQLResult 保持其原数据结构。直接调用 Graph 的其他入口仍需调用方提供知识上下文，不应假设所有入口都会自动检索。

## 3. Added / Modified Files

Phase 2 新增：

- datapilot/retrieval/__init__.py、knowledge.py、integration.py。
- domains/media/knowledge/ 下四份 Markdown 语料和 retrieval_config.json。
- evals/media/ 下 Golden Set、检索评测器、真实 E2E runner、README、本报告和原始结果 JSON。
- tests/media/ 下检索与 Agent 集成测试。

Phase 2 修改 Planner、SQL Agent、Analyst、AgentState、CLI 的知识上下文接入。graph.py、wren_tools.py 及三个 tests/datapilot 文件还含已有 Phase 1.3 改动。Reviewer 与 Session Memory 源文件没有修改。

## 4. Knowledge Corpus

四份 synthetic 文档覆盖 QoE 指标、错误码、排障 SOP、编码/转码基础。含 Playback Success Rate、Rebuffer Ratio、Startup Time/P95、Failed Session Count；错误码包括 E302、E401、E503、T100。E302 与 SOP 明确说明时间相关性不等于因果关系。

## 5. Chunking Strategy

共 16 个 chunks，平均 617.3125 字符。按 H2 分节，长节再按 H3/段落拆分。元数据包括 document_id、title、category、source_path、chunk_id、domain、tags；返回 API 中来源字段为 source。

## 6. Retrieval Pipeline

Markdown loader → heading-aware chunks → BM25 与本地向量检索 → RRF → 可解释 rerank → retrieve_knowledge()。返回 text、title、category、source、chunk_id 及 lexical/semantic/fused/rerank scores、retrieved_reason。Media 别名和分类提示来自 domain config。

每个本轮 E2E run 仅检索一次，最多五个命中 chunks；各组件复用该批知识，Planner 提示最多四个，其余相关组件最多五个。检索不调用 LLM。

## 7. BM25 Results

Recall@1 = 0.785714，Recall@3 = 1.000000，MRR@3 = 0.928571。BM25 也使用领域别名扩展，因此是带别名的词法基线。

## 8. Semantic Results

Recall@1 = 0.571429，Recall@3 = 0.928571，MRR@3 = 0.761905。当前实现是 TF-IDF 特征哈希向量、领域概念别名与余弦相似度；没有 pretrained sentence embedding。这里的 Semantic 是现有接口名称，不能把结果描述为神经语义模型的效果。

## 9. Hybrid Results

纯 RRF：Recall@1 = 0.714286，Recall@3 = 1.000000，MRR@3 = 0.892857。RRF 使用 k=60，并在查询内归一化融合分数；分数不是置信概率。

## 10. Rerank Results

Hybrid + rerank：Recall@1 = 0.928571，Recall@3 = 1.000000，MRR@3 = 1.000000。规则考虑标题中的确切错误码、正文交叉引用、metric、category 和 heading overlap。E302 标题匹配优先于其他文档提及 E302 的交叉引用。

## 11. 为什么 Hybrid 是否优于 BM25

纯 Hybrid 没有超过 BM25：Recall@3 相同，Recall@1/MRR@3 更低。Corpus 小、指标和错误码词法特征强，有限的别名/哈希向量无法稳定提供额外信号。Rerank 改善了本开发集的首位排序，但不证明在独立测试集上泛化。没有修改 Golden answers、删除失败 case 或按 query 原文硬编码答案。

## 12. Agent Integration

Planner 获得业务术语建议，Wren schema 继续约束可查询字段。SQL Agent 可以参考指标口径，但 SQL 仍经过 safety、dry-plan、DuckDB 执行与 Reviewer。Analyst/Final Answer 分别接收 approved 数据与知识，知识引用使用 [knowledge:chunk_id]。

纯知识问题复用 response task：只在存在检索证据时允许零 SQL 依赖，source_task_ids 可为空，且要求有效知识引用。没有新增 knowledge task，query/analysis/response 类型保持不变。RAG 不可用时继续数据流程；缺乏数据与知识的 response 仍然失败。检索失败、纯知识路径、未知引用与混合提示边界已有测试。

## 13. Evidence Boundaries

DATA EVIDENCE 来自 approved SQL/分析，KNOWLEDGE EVIDENCE 来自检索，INFERENCE 标注推断，LIMITATION 标注缺口。三份实际 Final Answer 均具备这些区分。该结构和来源引用校验不能保证每一条自然语言事实正确：Case B 的告警状态合并问题就是本次反例。

## 14. Retrieval Eval Results

15 个固定开发案例，其中 14 个有相关 chunk，1 个为无关问题。Recall 按每个 query 的相关 chunk 集合计算召回比例并宏平均；它不是 Hit Rate。MRR 基于返回的 top 3，准确名称为 MRR@3。无关问题在四种模式下都返回空结果。

| Mode | Recall@1 | Recall@3 | MRR@3 | Mean latency (ms) |
|---|---:|---:|---:|---:|
| BM25 + aliases | 0.7857 | 1.0000 | 0.9286 | 0.573 |
| Local vector baseline | 0.5714 | 0.9286 | 0.7619 | 0.620 |
| Hybrid RRF | 0.7143 | 1.0000 | 0.8929 | 0.802 |
| Hybrid + rerank | 0.9286 | 1.0000 | 1.0000 | 2.312 |

两个 query 各有两个相关 chunk，所以即使每个 query 首位都相关，Recall@1 仍小于 1；这与 MRR@3=1 不冲突。检索 LLM Calls=0，Retry=0。

## 15. Real Agent E2E Results

Case A：`E302 是什么？`。Flash，1/1 response task 完成；命中 E302、E503、CDN degradation SOP 共三个 chunks。无 SQL，无 Reviewer；Final Answer 解释 CDN upstream timeout，明确没有查询数据库、不证明根因。回答为英文，未要求模型语言一致性，属于交互局限。

Case C：`首帧耗时升高应该检查什么？`。Flash，1/1 response task 完成；命中 Startup Time/P95 定义和 startup latency SOP 两个 chunks。无 SQL，无 Reviewer；Final Answer 说明 P95 不可平均、基线/当前窗口比较、可用维度与链路检查，并声明未查询实际指标或告警。

Case B：`华南地区播放成功率下降，并出现 E302 告警，应该怎么排查？`。共执行三次，全部保留：

1. 全 Flash：一条 SQL 成功，Reviewer 两次 reason_summary 超长，fail-closed，未产生 Final Answer。
2. Flash Planner + Pro Reviewer：三条 SQL 全部 approve；Planner 产生了不兼容的两个 query 比较输入，Analyst source contract 拒绝，未产生 Final Answer。
3. Pro Planner + Pro Reviewer，其余 Flash：5/5 tasks 完成，3/3 approve，Analyst 与 Final Answer 生成，Session Memory commit 成功。但最终事实核验未通过，详见下文。

最后一次 B 的 tasks：

1. query：华南四个窗口按 CDN 查询成功数、会话数与成功率。
2. query：独立查询华南 E302 告警。
3. query：按可用 device 维度查询成功率。
4. analysis：依赖上述三个查询，比较窗口并关联告警。
5. response：基于已完成分析输出排查结论。

B 命中五个 chunks：CDN degradation SOP、E302、Playback Success Rate、Failed Session Count、startup latency SOP。未规划 province/ISP/OS/App Version/content type 等不存在的查询字段。

经过独立只读查询核验的数据：

| 华南范围 | previous_window | current_window | 变化 |
|---|---:|---:|---:|
| 整体 | 56/60 = 93.33% | 43/60 = 71.67% | -21.67 pp |
| CDN-A | 19/20 = 95% | 17/20 = 85% | -10 pp |
| CDN-B | 19/20 = 95% | 10/20 = 50% | -45 pp |
| CDN-C | 18/20 = 90% | 16/20 = 80% | -10 pp |

CDN-B 对新增失败会话的贡献为 9/13（69.23%）。同期有 3 条 high E302，真实状态分别为 open、investigating、resolved 各一条。

最终答案的关键原文片段：

> 【DATA EVIDENCE】……华南播放成功率从上一窗口 0.9333（56/60）降至当前窗口 0.7167（43/60）……CDN-B 从 0.95 降至 0.50……共 3 条、severity=high、status=resolved……
>
> 【INFERENCE】证据与“当前窗口 CDN-B 上游超时”这一方向一致……但这只是相关性，未获因果证明。
>
> 【LIMITATION】……无请求链路追踪、源站遥测、路由数据或受控验证，因此“E302 导致成功率下降”不可判定为因果。

原文全部保存在结果 JSON，未静默修订。上面 `status=resolved` 的表达存在错误：SQL 使用 MAX(status)，得到的是字符串聚合值，无法说明三个告警全部 resolved。Reviewer approve 也没有消除这一证据损失。答案还将 CDN-A/C 称为“健康对照组”，但它们也下降了 10pp，应称为下降较小的比较组。

因此 B 的工作流完成=true，人工事实验收=false；四类证据分区与因果谨慎性通过，事实准确性未完全通过。安全的核验结论是：优先排查 CDN-B 上游超时方向，核对当前 open/investigating 事件，并以 A/C 作相对比较；因果关系仍未证明。这是本报告的人工核验摘要，不是重新运行生成的 Agent 答案。

## 16. Latency / LLM Calls / Retry

| Run | Completed tasks | Retrieval calls | Agent SQL | Reviewer | LLM calls | Workflow seconds |
|---|---:|---:|---:|---|---:|---:|
| A Flash | 1/1 | 1 | 0 | N/A | 4 | 12.15 |
| B Flash failure | 0/5 | 1 | 1 | invalid JSON-contract output twice | 4 | 14.23 |
| C Flash | 1/1 | 1 | 0 | N/A | 3 | 9.72 |
| B Pro Reviewer failure | 3/6 | 1 | 3 | 3 approve | 7 | 57.18 |
| B Pro Planner + Reviewer | 5/5 | 1 | 3 | 3 approve | 10 | 110.06 |

所有运行 SQL Technical Retry=0、SQL Semantic Retry=0；A 有 1 次 Final Answer JSON 输出重试，第一次 B 有 1 次 Reviewer JSON 输出重试。B 的两次整体复测是独立 run，不隐藏在 bounded retry 内。A/C/最后一次 B 的成功后 Session Memory 更新均成功。

最后一次 B 的 10 次调用：Planner 1、SQL Agent 3、Reviewer 3、Analyst 1、Final Answer 1、Session extraction 1。A 的 4 次：Planner 1、Final Answer 2、Session extraction 1。C 的 3 次：Planner 1、Final Answer 1、Session extraction 1。

总计：28 次真实 LLM 调用，5 次 Agent retrieval，7 次 Agent SQL，另有 2 次本地只读事实复核 SQL。A/B-final/C 检索耗时分别为 1.56/1.87/1.32ms。工作流耗时不含 Wren/retriever 初始化和检索；没有测量 token 使用量。初始脚本错误分类的原计数和根据实际调用路径校正的分类均保留在 JSON。

## 17. Regression / Environment

Media tests：16 passed。tests/datapilot：184 passed。合并执行 200 passed（0.85s）。Phase 1.3 Planner、Reviewer、Session workflow/Memory 等原回归均通过；这不替代真实回答质量核验。

Python 3.12.14，venv 正常；真实 Wren + DuckDB 可用；Wren SQL memory 关闭，未检索历史其他数据。WREN_PROJECT_PATH 使用 domains/media，WREN_PROFILE 使用 datapilot_media_duckdb。原 MDL 构建受 Windows 默认 GBK 编码影响，已通过 PYTHONUTF8=1 重建；validate 通过：2 models、2 views、0 relationships。没有安装新依赖。git diff --check 通过。

## 18. Known Limitations

- B 的告警状态聚合/描述与对照组表述有已复现事实缺陷，需后续受控修复后再验收，不能称全部通过。
- 全 Flash 配置本次未完成 B；成功工作流依赖 Pro Planner/Reviewer，且耗时约 110s。CLI 默认模型配置没有修改。
- 当前 Semantic 依赖人工别名和特征哈希；未完成对 pretrained embeddings 的对比。语料小、开发集与配置开发有交互，结果不能代表真实生产数据。
- 只有一个无关问题案例；空返回检查不能证明广泛的相关性校准。RRF/rerank 分数不能当作置信概率。
- 三个窗口比较、泛化分析由 LLM 计算解释，并非所有派生值都经过独立算术校验。
- Planner/SQL/Final 提示使用有长度上限的知识文本；长文档仍可能被截断。本次 16 个 chunks 均低于 Final 的单条上限。
- 引用校验只验证 chunk 标识存在，不能证明句子被来源支持；Reviewer 检查当前任务，可能放行有信息损失的聚合。
- 无外部追踪、源站遥测或受控干预，无法证明 CDN-B 或 E302 与失败的因果关系。

## 19. Git Status / Diff

工作区保留原 Phase 1/1.3 与 Phase 2 改动。10 个 tracked 文件修改，另有 datapilot/retrieval、domains、evals/media、tests/media 未跟踪目录。全部 diff 不是本轮独有；domains 下既有数据与模型仍未提交。Reviewer/session_memory 源文件无 diff。没有创建 commit、没有 reset/checkout/clean、没有进入 Phase 3。

本轮到验证报告为止停止；不把已发现缺陷隐藏为成功，也不在“仅验证”的边界外继续改写 Core。
