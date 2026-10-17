---
name: kcpredict
description: 使用 KCPredict 分析沪深股票，默认输出最近3个月的 CZSC 结构、威科夫量价、ICT/SMC 与未来5个交易日 Kronos 预测可交互HTML综合图，附PNG/SVG预览，历史与预测共用连续时间轴，明确预测质量四种状态。也支持图表或 OHLCV JSON 的 ICT 解读。适用于股票综合分析、笔与中枢、流动性扫荡、FVG、MSS/BOS、OB、Breaker、OTE。不用于行情下载、训练、模型晋升或交易执行。
---

# KCPredict

将 CZSC 结构、威科夫量价、ICT/SMC 验证和可选 Kronos 路径放在同一工作流中，各自保留证据和可用性。KCPredict 运行时拥有独立的数据、日历和模型目录，不依赖其他项目或已启动的网页服务。ICT 扫描器随本技能提供，只使用 Python 标准库；运行时、冻结引擎、行情和模型仍由独立发行包管理。

## 默认交付：三个月可交互综合图

用户仅说“分析某只股票”或“综合分析”时，默认检查状态、分析结构与量价、验证 ICT、预测未来 **5个交易日**，交付可离线打开的单文件 **HTML 交互综合图**。回答内嵌 PNG 预览，并提供 HTML 入口及 PNG/SVG 下载链接。历史图窗为实际行情截止往前 **3个日历月**，计算预热与预测历史仍单独保留。读取 [默认综合图与调用说明](references/chart-workflow.md)。

优先使用 `scripts/kcpredict_report.py --symbol 准确代码` 一次生成结构 JSON、预测 JSON、ICT JSON、综合 HTML/PNG/SVG 和审计记录。综合图默认 **lookback=128、horizon=5、samples=64、seed=42、device=auto、temperature=.8、top_p=.9**；这是技能工作流默认值，原 CLI 默认值保持原样。用户指定只分析、不预测、不出图、周期或参数时遵守其范围。

图中同时展示：真实日线与确认笔／回溯中枢、成交量及此前20日均量、ICT 扫荡候选与严格 FVG／缺失确认、未来路径及 P10–P90、三类方法证据摘要，以及醒目的预测质量状态。历史与未来沿同一条连续时间轴衔接，并以明确的实际行情截止线区分事实和采样情景；不把它们拆成互不相连的行情与预测面板。图形只按实际输入自动提炼；任何标的、日期、价位、方向或标签都不能从上次示例硬编码复用。

预测质量 **通过 / 未通过 / 不可用 / 未执行** 必须分开。以 `prediction.quality.prediction_usable` 及真实门槛原因判定；未通过仍出图并标“仅采样情景”，不可用时保留结构／量价／ICT图和缺失原因。HTML 与 PNG/SVG 共用已验证的 `chart-data.json`，重新布局、交互或导出均不重新采样，不改变已评估路径、参数或质量状态。质量通过也不等于策略收益认证或 ICT 最终确认。不得为了出绿色“通过”而改参数、修样本、放宽门槛或伪造价格。

HTML、PNG、SVG 以 HTML 为视觉基准，配色、字体和版式共用 `assets/report-theme.json`，由 `scripts/kcpredict_theme.py` 加载并记录主题身份与 SHA。默认都隐藏单条样本线，仍展示预测中位数与 P10–P90；完整有效路径保留在数据中，HTML 可切换显示。生成、重新导出或引用这套样式时，整体保留技能的 `assets/` 与 `scripts/`，不要只复制绘图入口。主题改变只影响呈现，不改历史、预测、确认时钟或资格；迁移与审计约定见综合图说明。

## 选择工作流

- **沪深综合分析**：默认走三个月综合图工作流，包含未来5日预测及质量标注。已有研究 JSON 可直接重新绘图，无需重复模型推理；ICT 数据隔离见 [ICT 整合说明](references/ict-integration.md)。
- **仅结构／量价／预测**：按用户范围调用原 CLI，不强制添加无关步骤。
- **ICT 概念、截图或外部 OHLCV**：读取 [ICT 规则](references/ict-model.md)；有 JSON 时用 `ict_scan.py`。这一路径不要求 KCPredict 运行时，但不代表它支持导入这些市场或周期。截图中读不清的价格、时间和缺失周期标为 `unverified`。
- **验证观点或条件计划**：另外读取 [统一输出约定](references/output-contract.md)。先检查证据，再给条件；不要迎合用户预设方向。

## 调用入口

调用独立运行时时先阅读 [运行与输入说明](references/runtime.md) 的相关部分。使用本技能的 `scripts/kcpredict.py`；它仅使用 Python 标准库，将参数原样传给独立 CLI。命令中的 `SKILL_DIR` 替换为本技能实际目录。

```sh
python3 SKILL_DIR/scripts/kcpredict.py status
python3 SKILL_DIR/scripts/kcpredict_report.py --symbol 002436.SZ --end 2026-10-16
python3 SKILL_DIR/scripts/kcpredict.py data list
python3 SKILL_DIR/scripts/kcpredict.py analyze --symbol 002436.SZ --end 2026-10-16
python3 SKILL_DIR/scripts/kcpredict.py predict --symbol 002436.SZ --end 2026-10-16
python3 SKILL_DIR/scripts/kcpredict.py run --symbol 002436.SZ --end 2026-10-16
```

- `analyze`：结构与量价；`predict`：Kronos 预测；`run`：组合二者。同步执行，直接检查完成结果。
- 全局 `--home`、`--output` 放在子命令前。`--output` 保存完整 JSON 到新文件，拒绝覆盖已有文件；不要把全部 K 线刷进对话。
- 分发器依次使用显式 `KCPREDICT_PYTHON`、显式 `KCPREDICT_EXECUTABLE`、PATH 中的 `kcpredict`、当前 Python 已安装的模块。显式配置无效就报告，不能改走另一环境。找不到时按错误说明安装独立发行包，不猜其他项目路径。

```sh
python3 SKILL_DIR/scripts/kcpredict.py --output ./structure.json analyze --symbol 002436.SZ --end 2026-10-16
python3 SKILL_DIR/scripts/kcpredict.py --output ./forecast.json run --symbol 002436.SZ --end 2026-10-16 --lookback 128 --horizon 5 --samples 8 --seed 42 --device auto
```

在现有分析或组合结果上运行 ICT，原始研究 JSON 保持独立：

```sh
python3 SKILL_DIR/scripts/kcpredict_ict.py --input ./structure.json --frequency 日线 --output ./ict.json
# 已有用户提供的通用 OHLCV JSON：
python3 SKILL_DIR/scripts/ict_scan.py --input ./candles.json --direction auto --minimum-rr 2
```

综合分析通常以已闭合周／月线提供偏向，以日线扫描候选。日线不含分钟级入场或纽约时段证据；不能从中重建 Silver Bullet。沪深日周月模式按 `Asia/Shanghai` 解释日期，不应用纽约 Kill Zone。外部分钟数据的海外时段按 `America/New_York` 动态转换，不能硬编码 UTC 偏移。

## KCPredict 运行时约定

1. 需调用独立 CLI 时先 `status` 检查实际引擎平台、已导入股票、日历、模型和预测依赖；已有研究 JSON 或外部 ICT 输入无需检查运行时。运行时只使用准确沪深代码，支持显式沪市 B 股；不接受北交所。仅有名称且无法可靠确定代码时，先澄清代码。
2. 缺行情或日历时，使用用户提供的 CSV 导入独立状态目录。日历需要显式 `--verified`；行情的 `--verified-unadjusted` 与单位参数记录用户声明，不能称为外部认证。原始 CSV 缺合法来源或真实交易价声明时，保留 `csv_unverified` 与不可用原因，不能修改来源字符串来取得资格。
3. 技能综合图预测默认历史 **128** 根、未来 **5** 个交易日、**64** 条采样、种子 **42**、设备 **auto**；原 CLI 单独调用的默认采样仍为8。遵守用户参数；质量失败或设备不可用时不偷偷改变参数重跑。明确的预测截止日必须有该日已收盘行情，不能拿较早收盘替代。
4. 模型使用独立固定版本 Kronos-base / tokenizer；按用户选择执行 `models import` 或显式联网的 `models download`。`status`、分析和预测本身不下载行情或权重。
5. CLI 退出码 **0** 表示命令执行成功；带预测 JSON 的 **2** 表示已生成但未通过可用性门槛，仍要读取 `prediction.quality` 和缺失原因（参数解析错误也可能返回 2，但无研究 JSON）；**1** 是输入、依赖或执行失败。HTTP 状态、进程存活、返回文件存在都不能替代质量判断。

## 结果表达

报告股票、请求截止、实际行情截止、方法、实际设备与输入来源声明。结构读取 `analysis.frequencies`、`analysis.source_evidence` 和 `analysis.price_volume`；预测读取 `prediction.forecast`、`summary`、`quality`、`resource` 与 `qualification`。

- 锚点与确认时间分开，形成中笔和周/月末桶明确可变化；`as_of` 后的行情不得进入历史结论。
- 来源 B/S、连续量价强度与模型路径分别解释，不生成目标仓位或成交结论。
- 筹码为历史 HLC/3 成交量加权分布，不是股东真实持仓成本。
- P10–P90 是采样分位区间；上涨采样比例不是校准概率、历史胜率或收益认证。
- 缺数据、日历、模型或输入资格时保留 unavailable / partial 与原因，不填零或提高资格。
- 独立版不提供旧评分模型、异步任务查询或历史预测图核验入口。以当前 CLI `--help` 为准。

## ICT 验证与综合结论

按顺序核查：高周期偏向与区间 → 对侧流动性目标 → premium/discount → 有意义的 sweep → displacement 与严格三 K 线 FVG → 收盘确认 MSS／BOS → 有效回撤 → 结构失效与扣成本盈亏比。OB、Breaker、OTE 是需分别验证的区域或共振，不能替代缺失的扫荡和结构确认。

- 原 CLI 不原生输出 ICT。`kcpredict_ict.py` 的 `kcpredict_ict_v1` 和扫描器的 `mechanical_status` 是独立派生证据，不能改写 CZSC/Wyckoff 输入资格或冒充最终状态。
- CZSC 笔／中枢不等同 ICT swing／OB，Wyckoff Spring 不自动等同 SSL sweep；分别列出支持、冲突和缺失。Kronos 未来样本不参与历史 ICT 扫描，也不能补齐未发生的确认。
- 对当前待验证设定给出一个最终状态：`confirmed`（所需证据、已闭合确认及净 R:R ≥ 2 均通过）、`developing`（已有有效前提、仍待后续确认）、`invalidated`（原设定已失效）、`no-trade`（冲突、过时、错过入场、关键数据缺失或净 R:R 不足）。概念解释不需强行生成设定状态。
- 机械扫描器只算毛 R:R；费用、滑点、实际止损缓冲与成交可行性缺失时净 R:R 是 `unverified`，不能因此给最终 `confirmed`。沪深看空候选表达结构风险，不默认为可做空指令。
- 每条关键结论带 K 线日期／确认时间、价位、方法与 `observed / derived / interpretive / missing` 分类。最终状态范围、缺失资料及下一步条件见统一输出约定。

ICT 源自 [carreypro/ict-trading-model](https://github.com/carreypro/ict-trading-model)，冻结版本及本地适配见 [来源记录](references/ict-provenance.md)。
