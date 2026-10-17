# KCPredict × ICT 调用与数据隔离

## 三个入口

股票综合分析默认走 [综合图工作流](chart-workflow.md) 的 `scripts/kcpredict_report.py`，包含以下独立入口及未来5日预测；不再要求用户另说“出图”或“预测”。

| 入口 | 输入与用途 | 依赖 |
|---|---|---|
| `scripts/kcpredict.py` | 调用独立 `status / analyze / predict / run`，保持现有参数与退出码 | 独立发行运行时 |
| `scripts/kcpredict_ict.py` | 从研究 JSON 或结构 JSON 读取已闭合 OHLCV，生成附加 ICT 机械证据 | 标准库及同目录 `ict_scan.py` |
| `scripts/ict_scan.py` | 扫描用户提供的通用 OHLCV JSON，手工核查高周期与市场上下文 | Python 标准库 |

只做概念／截图解读时用 [ICT 规则](ict-model.md)，不调用不存在的数据服务。所有入口本地同步运行；不自动抓取行情、启动网页或下载模型。

日期转换需要运行环境可提供 IANA 时区数据；macOS 通常使用系统库，缺少时区数据的 Windows Python 环境可能需要安装 `tzdata`。扫描算法本身没有额外第三方计算依赖。

## 从已有研究结果整合

把 `SKILL_DIR` 替换为本技能实际目录；文件输出均使用新路径。示例日期不是数据新鲜度证明。

```sh
python3 SKILL_DIR/scripts/kcpredict.py status
python3 SKILL_DIR/scripts/kcpredict.py --output ./research.json analyze --symbol 002436.SZ --end 2026-10-16
python3 SKILL_DIR/scripts/kcpredict_ict.py --input ./research.json --frequency 日线 --output ./ict-daily.json
python3 SKILL_DIR/scripts/kcpredict_ict.py --input ./research.json --frequency 周线 --output ./ict-weekly.json
```

综合分析默认包含预测；用户明确仅结构／量价／ICT时省略。可用 `run`，或用综合图工作流分别运行 `analyze` 与 `predict`，这样预测资产缺失时仍能保留已完成的结构结果。适配器仍只用 `analysis.frequencies`，绝不读取 `prediction`。只有 `predict` 的文件缺乏真实结构 OHLCV，不能拿 forecast 充当扫描输入。模型／预测资格失败不改变已经存在的真实结构数据资格。

适配器参数：`--input`、`--frequency 日线|周线|月线`（默认日线）、`--direction auto|long|short`（默认 auto）、`--swing-width`（默认 2）、`--minimum-rr`（默认 2）、可选 `--output`。完整参数以脚本 `--help` 为准。

接收 `kcpredict_research_v1`（读取 `analysis`）及 `kcpredict_structure_v1`（直接读取 `frequencies`）。适配器：

- 只接收 `is_closed=true` 的 bars；排除形成中周／月桶。
- 对所有有效截止取最早限制，bar 的时间和显式 `observed_at` 均不能超过该限制；日期型 K 线用上海时间 15:00 标注。
- 保留符号、请求／实际截止、来源资格、价格口径、日历身份及输入文件 SHA256，便于核对两份 JSON 是否来自同一次研究。
- 不从未来样本、CZSC 笔、来源 B/S 或量价分数反推 ICT 确认。扫描直接使用 OHLCV，原方法证据仍在原始文件。
- 有效闭合 bars 少于 10 时输出 `status=unavailable` 和原因；不填充、不降宽度重跑、不换用形成桶。

输出 schema 为 `kcpredict_ict_v1`。`status=completed` 仅表示扫描完成；`final_status=not_assessed` 表示最终 ICT 裁决仍待上下文核查，机械证据在 `scan` 内。这与最终报告中的 `confirmed/developing/invalidated/no-trade` 不同。扫描的 `latest_candidate` 是最近扫荡候选，`candidates` 保留最近最多 20 个候选；不能因为最新候选失败就隐藏另一方向的冲突。

适配器退出码：`0` 为扫描完成，`2` 为闭合历史不足（仍有 `unavailable` JSON），`1` 为输入／输出错误，`130` 为中断。参数解析失败也可能为 `2`，须检查是否存在合法 JSON；不要只按退出码判断结果。通用扫描器的输入或文件错误使用 `2`；各脚本退出码不得与预测质量门槛混淆。

周／月频率的最后一桶按 KCPredict 原契约延后确认；不要假定过去的桶在当时已知，或把锚点日期当成实际可用日期。适配器用于该 JSON 的当前研究截止，历史复盘应先重新生成对应历史 `analyze --end`，不能用较新导出的周／月闭合布尔值重建更早时点。

沪深聚合模式将 session 标为 `not_applicable_daily_or_higher`，保留上海交易时钟；不解释纽约时间。费用／净 R:R、事件／公司行为和做空可行性不会因扫描完成取得认证。日历未核验、来源不合格时，原有 unavailable/partial 必须继续报告。

## 通用 OHLCV JSON

```json
{
  "meta": {"instrument": "USER-SUPPLIED", "timeframe": "5m", "venue": "user_supplied"},
  "candles": [
    {"timestamp": "2026-10-16T09:35:00+08:00", "open": 100, "high": 102,
     "low": 99, "close": 101, "volume": 1234, "is_closed": true}
  ]
}
```

以上仅格式示例。实际至少需要 10 根有效闭合 K 线；也接受直接 candle 数组或 `[timestamp,open,high,low,close,volume,...]`。数字时间为 Unix 秒／毫秒，字符串建议提供 ISO 时区；无时区字符串沿用上游按 UTC 解析的约定，因此不要向通用扫描器直接传上海日期标签。倒序输入会排序，重复时间和不合法数值会拒绝。未标闭合的历史输入属于用户声明，需核实；明确未闭合的 K 线不得用于确认。

```sh
python3 SKILL_DIR/scripts/ict_scan.py --input ./candles.json --direction auto --swing-width 2 --minimum-rr 2 --output ./ict-external.json
```

通用扫描只用该输入的一种周期，不能自行完成多周期偏向、新闻过滤、等高／等低集群、OB／Breaker／IFVG 全部序列验证。日线缺口需核查停牌／公司行为；候选目标可能已被此前行情消耗，需逐根复核流动性仍有效。K 线内成交顺序、费用与实际可成交价缺失时不能给最终确认。

10 根只是最低输入长度，不能视为足够预热；不足 `atr_period` 时扫描器使用当时可得的较短 ATR 窗口。报告数据长度与阈值，不把短窗口结果称为长期稳定结论。

## 汇合输出

按 [统一输出约定](output-contract.md) 用一份证据表比较 CZSC、量价、ICT，预测单列为情景。保持方法之间的定义差异，不新增综合分数、胜率或收益认证；共振是证据关系，不是独立统计样本。
