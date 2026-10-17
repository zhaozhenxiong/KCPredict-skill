# ICT 来源与本地适配

- 上游：[carreypro/ict-trading-model](https://github.com/carreypro/ict-trading-model)。
- 冻结提交：`7162437e28b1f59a4b25ea88615ee99902c9cc75`。
- 取得日期：2026-10-17（Asia/Shanghai）。
- 版权：Copyright (c) 2026 carreypro；[完整 MIT 许可证](ict-LICENSE.txt)。
- 引入并适配：`scripts/ict_scan.py`；测试来源为上游 `tests/test_ict_scan.py`。
- 根据上游 `SKILL.md`、`references/ict-model.md`、`references/output-contract.md` 改编中文规则、证据门槛及输出约定。

本地保留 FVG、sweep、位移、收盘结构突破、区间／OTE、回撤、候选毛 R:R 与机械状态接口；补充输入有限数检查、闭合过滤、swing 确认时钟及后续失效／目标检查，避免把后知的极值或历史回撤直接称作当前确认。输出拒绝覆盖已有文件。

新增 `scripts/kcpredict_ict.py` 将 KCPredict 真实闭合日／周／月 OHLCV 转换为扫描输入，保留研究截止和来源，排除预测样本；沪深聚合数据的时段标为不适用。`kcpredict` 的名称与运行时分发契约不变，无需另装第二个 ICT skill。

此集成未赋予上游机械规则已验证收益或本地市场适用性；未加入行情抓取、自动交易、训练或新模型资格。
