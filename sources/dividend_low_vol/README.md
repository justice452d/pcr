# 红利低波估值数据链路

指数采用中证红利低波动指数 `H30269`（50只样本），不是 `930955` 红利低波100。

自动数据：

- 股息率：中证指数 `H30269indicator.xls`，使用总股本口径 D/P1；官方文件通常只保留最近约20个交易日。
- 指数价格：中证指数 `index-perf` 接口中的 H30269 收盘点位；这是价格指数，不含现金分红再投资。
- 中国10年国债收益率：复用现有 ChinaBond 中债国债收益率曲线缓存。
- PB：中证 H30269 月度事实表。官方公开日频估值文件没有 PB，因此按事实表日期记录，日间只沿用不超过45天的最近快照，并在页面显示 PB 日期。

完整历史缓存在 `H:\PCR\cache\valuation\dividend_low_vol_history.json`。每条记录同时保存原始指标、来源、更新时间、百分位、分项温度和默认权重下的最终温度。

手动备用文件为 `H:\PCR\cache\valuation\valuation_manual.json` 或同目录 `valuation_manual.csv`。模板见同目录 `valuation_manual.example.*`。字段：

`date, dividend_yield, pb, cn10y, index_price`

合并优先级遵循：可靠自动数据 > 已核实本地缓存 > 手动备用。手动数据用于自动源和缓存均缺失的日期或字段，不会静默覆盖可靠自动值。

模型位于 `valuation_models/dividend_low_vol.py`。默认权重为 45/35/20；历史百分位算法位于 `valuation_models/common.py`，固定为“小于等于当前值的样本数 / 样本总数”。


