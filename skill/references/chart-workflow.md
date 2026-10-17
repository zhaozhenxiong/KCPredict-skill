# 默认三个月可交互综合图

用户说“分析某只股票”时，默认交付最近3个日历月的单文件 HTML 交互综合图：结构、量价、ICT、未来5个交易日预测及质量标识都在图里。HTML 内嵌实际图表数据与交互代码，可离线打开，不依赖 CDN、网页服务或旁边的 JSON 文件。回答内用绝对路径嵌入 PNG 预览，另给 HTML 入口和 PNG/SVG 下载链接；附简短共振、冲突、关键价位和缺失说明即可。用户明确的范围、参数和不出图要求优先。

## 一次调用

```sh
python3 SKILL_DIR/scripts/kcpredict_report.py --symbol 002436.SZ
# 固定历史截止；输出目录必须新建或为空：
python3 SKILL_DIR/scripts/kcpredict_report.py --symbol 002436.SZ --end 2026-10-16 \
  --output-dir ./my-stock-review
```

默认 `months=3,lookback=128,horizon=5,samples=64,seed=42,device=auto,temperature=.8,top_p=.9`。months 只控制展示窗口，不裁掉模型或结构预热；未来日期来自同一独立交易日历。原 CLI 的低层默认8条不变，包装入口显式传64。用户显式指定其他采样数时原样执行。

既有32条采样的研究文件保留原有样本、输入身份和质量状态；重新美化仍按实际32条呈现。用户要求改采样数重新预测时，保存一份新的预测与图表文件，不能把旧32条数据重新标成64条。

入口按 `status → analyze → predict → ICT → chart → HTML` 执行，默认在当前目录 `outputs/` 下创建带股票、上海时间及随机后缀的新目录。只有一个指定代码时直接做，无需再确认展示形式。HTML 阶段仅读取已经验证的 `chart-data.json`，不再调用模型。

- 分发器仍遵守显式 `KCPREDICT_PYTHON` / `KCPREDICT_EXECUTABLE`，然后 PATH / 当前已安装模块。不会从其他项目猜数据、权重或运行时。
- 无 `--end` 时将预测锁到本次分析实际 `data_end`，并说明是否滞后。显式截止原样保留，不用旧数据冒充指定日。
- 结构成功而预测缺资产／依赖／行情资格时，保留真实分析并写预测不可用记录，继续出图；不自动下载权重、不替换参数或重试。
- 合法预测 JSON 的退出码2表示质量未通过，继续画情景并保留失败状态；中断130立即停止。包装入口退出0仅表示综合图产出成功，不表示预测通过。
- `--no-predict` 供用户明确仅分析时使用，图中标“未执行”。`--home` 透传独立状态目录；`--chart-python` 显式指定绘图 Python，不悄悄换环境。

输出包括 `structure.json`、`prediction.json`（或 `prediction-unavailable.json`）、`ict.json`、`composite.html`、`composite.png`、`composite.svg`、`chart-data.json` 和 `manifest.json`。最后两项保存输入身份、可见证据、门槛原因、调用参数、退出码及文件 SHA，供复核。`manifest.charts.html` 列出交互文件，HTML 步骤记录退出码、文件大小和 SHA；命令成功但文件缺失、为空或不是 HTML 时不报告综合交付完成，已经生成的预览和 JSON 继续保留。已有文件拒绝覆盖。

## 已有结果只重新绘图

```sh
python3 SKILL_DIR/scripts/kcpredict_chart.py --input ./research.json \
  --prediction ./prediction.json --ict ./ict.json --months 3 \
  --output-dir ./restyled-chart
python3 SKILL_DIR/scripts/kcpredict_html.py --data ./restyled-chart/chart-data.json \
  --output ./restyled-chart/composite.html
```

`--input` 接收含 analysis 的研究 JSON 或原生结构 JSON。已有组合结果自带预测时可省 `--prediction`。省略 `--ict` 时只扫描真实闭合日线。单独预测文件没有真实历史，不能作为 `--input`。

分开保存的预测／ICT必须与结构标的、截止和数据身份相容，错配则报错；不得把别的股票、更新日期或未来样本接入历史图。已有结果美化不重跑模型，HTML/PNG/SVG 共享同一份已评估路径与质量记录。图使用实际样本数与 horizon，不能把旧图的32条／5天写成固定数字。

## 共享主题与重新导出

HTML、PNG、SVG 以 HTML 为视觉基准，共用 `assets/report-theme.json` 的配色、字体和版式令牌，统一标题、质量状态、连续主图、成交量与证据摘要的视觉层级。`scripts/kcpredict_theme.py` 提供 `load_theme()` 与 `theme_identity()`；主题身份包含 `schema`、`id` 和规范 JSON 的 `sha256`，供静态图与交互图核对。不要在单个 renderer 中另存一套默认色值或依赖上一张图的样式。

默认视图展示中位路径和 P10–P90，隐藏单条有效样本线；HTML 的图层开关可显示全部已有有效路径。隐藏只改变显示，`chart-data.json` 的 `prediction.samples`、原请求样本数、有效率和质量门槛均须保持原值。主题、交互和导出都不触发新推理，不修复无效价格，也不把来源未通过改成已通过。

重新生成图片与 HTML 时使用同一技能版本、同一份 `chart-data.json` 和共享主题。HTML 内嵌完整主题与 `theme_identity`；静态图在 `chart-data.json` 的 `render.theme` 记录同一主题身份，`render.layer_defaults.samples=false` 记录默认隐藏样本线。比较不同交付时同时核对历史指纹、预测参数、质量状态与主题 SHA，不能仅凭配色相同认定输入相同。`manifest.json` 继续记录证据 JSON 与成品文件的 SHA；分享既有 HTML 时无需额外主题文件。

## 图内信息

- **主图**：近3个月真实日线、已确认笔和形成中笔、最近回溯中枢、支撑／上沿、实际确认日。真实历史与未来日期共用连续横轴，预测从最后一个实际收盘衔接，截止线和情景区域清楚区分两者。锚点连线不表示当时已知；未来确认的笔／中枢不能显示成过去已确认。
- **量价**：真实成交量与此前20根闭合日线均量，当前相对量、连续供需方向与新事件。旧 LPS/Test 状态延续不等于新确认；少于20根预热或缺量时保留缺失。
- **ICT**：严格 FVG 必须给三根闭合 K 线及第三根确认日，扫荡候选、MSS参考与当前机械状态分开。缺失的位移、MSS、回撤、净 R:R不编造；机械结果不升级最终确认。
- **预测**：沿历史时间轴向右连续展示有效路径、中位数、P10–P90与实际未来日期，共用价格尺度；这些仍是独立的模型情景，不能补成历史确认。没有可用输出时保留原因面板。被质量规则拒绝的路径不修复、不混入有效样本分位数。
- **质量**：醒目标注通过／未通过／不可用／未执行，展示有效条数、比例和阈值。原始 `prediction_usable=false` 不能因为过滤后画出了漂亮路径而变成通过。门槛通过只表示可用性检查通过，不表示机构参与、胜率或收益认证。

标题、日期、价位、方向、标签和样本阈值一律从实际 JSON 提炼。自动布局保留少量关键标注、清晰字号和低干扰底色，不把所有事件 ID 堆进图中。量价与几何资格继续沿用原方法；没有正式来源资格的几何只能作为研究参考。

## 人工补充标注

扫描器只检查其指定 swing 模式；人工逐根复核发现较早流动性参考时，可用 `--notes ./notes.json` 补充。格式：

```json
{
  "schema": "kcpredict_chart_notes_v1",
  "symbol": "002436.SZ",
  "as_of": "2026-10-16",
  "items": [{
    "type": "ssl_candidate", "date": "2026-10-13",
    "reference_date": "2026-09-03", "reference_price": 32.80,
    "extreme": 32.69, "close": 36.19,
    "mss_reference_date": "2026-10-08", "mss_reference_price": 44.00
  }]
}
```

此例仅说明这次已核查的标注格式，不是其他股票默认值。`ssl_candidate` / `bsl_candidate` 会逐项与真实闭合 OHLC及当时 swing 可用性核对；价格／日期错配即拒绝。`level` 用 `date,field,price,label` 指定原 K 线字段参考。文字标签始终属于人工解读，不能把 `data_verified` 视为完整设定通过。净 R:R、事件和交易可行性仍需独立核验。

## 绘图依赖和迁移

工作流分发器、数据提炼和单文件 HTML 生成器只用标准库；PNG/SVG 预览另外需要 Matplotlib。完整工作流会生成两种交付，因此仍需准备绘图依赖。已有 `chart-data.json` 可直接生成 HTML，无需安装 Matplotlib，也不会重跑推理。源码安装器默认安装 `[charts]`（完整安装为 `[predict,charts]`），也可在选定绘图环境显式安装：

```sh
python -m pip install "/path/to/KCPredict[charts]"
# 或只补绘图依赖：
python -m pip install 'matplotlib>=3.9,<4'
```

离线安装先准备与目标系统／架构匹配的 Matplotlib及传递依赖 wheels。脚本发现缺依赖只给准确原因和安装命令，不自行联网。使用可用的中文字体，或 `--font` 指定本地字体；没有中文字体时以英语标签出图并记录原因。SVG内嵌字形路径，便于跨机器查看。

将整个技能目录复制到目标机的 `~/.codex/skills/kcpredict/`，保留 `agents/`、`references/`、`scripts/` 和 `assets/`。脚本包括 `kcpredict_report.py`、`kcpredict_chart_data.py`、`kcpredict_chart.py`、`kcpredict_html.py` 和共享主题 helper `kcpredict_theme.py`，以及同目录运行时分发器与 ICT 脚本。生成器按技能目录定位 `assets/composite.html` 与 `assets/report-theme.json`；生成、重新导出或引用主题时，这两个资源与 helper 必须整体复制。缺少资源应报告错误，不能悄悄退回另一套主题。

模板和主题文件只在生成时需要；最终 HTML 的数据、主题、样式和脚本全内嵌，可作为单个文件离线分享，PNG/SVG 也可独立查看。独立 skill ZIP、源码包与 standalone ZIP 应同时包含主题 JSON 和 helper；运行时 wheel 本身不代替技能目录。不把私人行情、模型或这次示例输出装进技能；不依赖本机 Downloads 路径。
