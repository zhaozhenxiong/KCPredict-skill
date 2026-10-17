# 独立运行与输入契约

本文件描述独立 CLI 的原有能力。ICT 是技能目录中的附加扫描层；调用、输入隔离和日周月限制见 [ICT 整合说明](ict-integration.md)，不改变下面的数据／模型资格门槛。

股票分析的默认交付已设为 [三个月综合图](chart-workflow.md)：技能包装入口显式传 `samples=64` 并展示未来5日预测。下文原 CLI 的 `samples=8` 是低层默认值，二者不要混淆。

## 安装和发现运行时

技能文件可以单独复制，但实际计算需要独立 KCPredict 发行包。将发行包解压到新电脑的可写目录，安装 **64 位 Python 3.12**，然后在发行目录执行：

```sh
# macOS
sh install.sh

# 或显式 Python 3.12
python3.12 install.py

# 只做结构分析，暂不安装 PyTorch
python3.12 install.py --no-predict
```

Windows 使用 `install.bat` 或 `py -3.12 install.py`。安装器在发行目录创建 `.venv`，不修改系统 Python。结构引擎随包支持 macOS arm64 和 Windows x64；Linux / Intel Mac 需要额外构建相同冻结来源的匹配引擎，不能把其他 CZSC 版本当作替代。实际验证平台以本次发行验证记录为准。

让技能找到这次安装，选择一种配置：

```sh
# macOS / POSIX；用自己的独立发行目录替换路径
export KCPREDICT_PYTHON="/path/to/KCPredict/.venv/bin/python"

# 或指定该目录的 CLI
export KCPREDICT_EXECUTABLE="/path/to/KCPredict/.venv/bin/kcpredict"
```

Windows 命令提示符：

```bat
set "KCPREDICT_PYTHON=C:\path\to\KCPredict\.venv\Scripts\python.exe"
```

也可激活该 `.venv`，让 PATH 能找到 `kcpredict`。不设置以上变量且 PATH 没有命令时，分发器只尝试当前 Python 已安装的 `kcpredict` 模块。发现顺序为 `KCPREDICT_PYTHON` → `KCPREDICT_EXECUTABLE` → PATH → 当前 Python；显式配置错误不会转投别的安装。变量值是单个可执行路径，不要附带命令行参数。

独立状态默认位于 `~/.kcpredict`；`KCPREDICT_HOME` 或 CLI 全局 `--home` 可更改。迁移时复制自己的独立状态，重建新电脑的 `.venv`，再运行 `status`；不要直接跨平台搬运虚拟环境。skill 不自动定位或导入另一项目的数据库与模型。

离线安装需要提前在相同目标系统、CPU 和 Python 3.12 环境准备全部依赖 wheels，并使用发行包的 `install.py --offline --wheelhouse /path/to/wheelhouse`。普通本地分析和预测不联网；首次在线安装依赖和显式 `models download` 需要网络。完整安装参数见发行包 `README.md` 与 `install.py --help`。

## 准备行情和日历

独立版没有行情下载器。用户提供 UTF-8 CSV，至少包含 `date,open,high,low,close,volume,amount`；没有 `symbol` 列时提供 `--symbol`。日期须为递增且不重复的实际本地日线日期。价格为未复权 OHLC，成交量单位为股、成交额为人民币元，不能将手、万元或复权后价格冒用为这些单位。

以下命令示例直接使用独立 CLI；通过技能时在参数前加 `python3 SKILL_DIR/scripts/kcpredict.py` 并省略 `kcpredict`。

```sh
kcpredict data import-csv ./my_daily.csv --symbol 002436.SZ \
  --verified-unadjusted --volume-unit shares --amount-unit CNY
kcpredict data import-calendar ./my_calendar.csv --verified
kcpredict data list
kcpredict status
```

日历 CSV 需要一列 `date`，内容为已核实的开市日，递增且唯一，覆盖所需历史依赖和未来预测开市日。普通周一至周五日历不能替代交易所节假日核验。`--verified` 记录导入用户的已核验声明，`externally_verified` 仍为 false。

`--verified-unadjusted` 同样记录用户的价格与单位声明。只有原 CSV 同时含受支持的 `source` 和 `has_trade_price=1` 时，才保留对应原始来源契约；没有这些列或来源不受支持时，存储为 `csv_unverified`，不能只加确认标志就称为来源已核验。结构原生来源白名单包括 `tushare`、`tushare:daily`、`fuyao_unadjusted_v1`、`baostock`、`eastmoney_unadjusted_v1`、`sina_unadjusted_v1`、`tencent_star_unadjusted_v1`。预测当前契约只接收 `sina_unadjusted_v1` / `fuyao_unadjusted_v1`。不得为了通过门槛改写不真实的来源声明。

导入不会自动认证 CSV 内容，输出保留文件 SHA 和 `provenance=user_asserted`。同代码同日期完全一致可复用，冲突数据整批拒绝，不覆盖已有观察。

## 准备固定模型

预测额外需要预测依赖及固定权重，约 425 MB。按用户选择显式导入或下载：

```sh
kcpredict models status
kcpredict models import /path/to/kronos
kcpredict models download
```

导入目录结构：

```text
kronos/
  config.json
  model.safetensors
  tokenizer/
    config.json
    model.safetensors
```

模型为 `NeoQuasar/Kronos-base` revision `2b554741eca47781b64468546e77fef3e85130e6`，tokenizer 为 `NeoQuasar/Kronos-Tokenizer-base` revision `0e0117387f39004a9016484a186a908917e22426`。程序核验固定文件身份，不能用其他版本文件混装。状态和预测调用都不会自动下载权重。

## 分析、预测和组合

```sh
kcpredict analyze --symbol 002436.SZ --end 2026-10-16
kcpredict predict --symbol 002436.SZ --end 2026-10-16 \
  --lookback 128 --horizon 5 --samples 8 --seed 42 --device auto
kcpredict --output ./combined.json run --symbol 002436.SZ --end 2026-10-16
```

`--output` 与 `--home` 是全局参数，放在子命令前。输出目标必须为新文件。所有研究命令同步完成；预测运算中断返回 130，已经生成的文件不代表质量通过。独立网页可使用 `kcpredict serve --host 127.0.0.1 --port 8130` 打开，但技能计算直接走 CLI。

不指定 `--end` 时，CLI 按上海时区选择当前可知收盘截止，并明确输出实际 `data_end`；需要说明本地单股行情是否落后。显式预测 `--end` 必须有当日已收盘行情。`analyze --start` 仅截取图表展示起点，历史输入仍保留预热；`analyze --end` 可用带时区时间戳做盘中截止。`predict` / `run` 的预测截止使用实际日线交易日。

预测默认 `lookback=128,horizon=5,samples=8,seed=42,device=auto,temperature=.8,top_p=.9`。CLI 允许 lookback 128 或 256，样本和未来长度等范围由当前 CLI 校验。`auto` 选择实际可用的 CUDA / MPS / CPU，明确指定不可用设备会失败。报告 `prediction.resource.actual_device`，不能从用户请求推断执行设备，也不承诺不同设备随机样本逐位一致。

## 结果和退出状态

输出外层是 `kcpredict_research_v1`：

- `symbol,requested_end,data_end,data_provenance,calendar_identity`：请求、实际截止与用户来源声明。
- `analysis.frequencies`：日/周/月 bars、fractals、pens、zones、divergences 与 indicators。
- `analysis.source_evidence.latest`：当前结构和威科夫状态、可用性、支持事实及失效边界；`events` / `evaluations` 保留因果来源。
- `analysis.price_volume`：W32 数值、事件、输入资格及原因。缺显式已核验日历时，几何仍可查看，来源资格不可用。
- `prediction.forecast,summary`：有效样本的采样中位路径和 P10–P90。缺有效样本时保留空结果。
- `prediction.quality`：`prediction_usable`、有效样本和输入契约 / 日历可用性。预测完成不代表此字段为 true。
- `prediction.resource,qualification`：实际设备、耗时、冻结身份与研究资格；不授予交易策略收益认证。

退出码 0 为命令成功执行；包含预测结果且退出码为 2 时，表示 `prediction.quality.prediction_usable=false`，应读取 JSON 并解释门槛原因，不把质量失败当成可用预测。参数解析错误也可能返回 2，但不生成研究 JSON。退出码 1 为输入、资产、依赖或执行错误，130 为用户中断。结构命令返回 0 仍需检查来源 evidence 的 available / partial / unavailable，不能把缺资格事实补成中性分数。
