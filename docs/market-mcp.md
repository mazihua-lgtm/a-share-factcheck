# A 股研究 MCP

独立实现行情研究能力，不调用恢恢量化、不会下单、不会登录券商。Claude Code、Codex、Cursor 只需接一次本项目的 `finchecker-mcp`；客户端把自然语言转成工具参数，不由服务器自由生成或执行策略代码。

| 工具 | 能力与边界 |
|---|---|
| `discover_data` / `query_dataset` | 列出支持的字段、预设和已发现的数据文件；按日期、代码、列筛选，可用 `next_offset` 翻页。文件存在不代表数据已验证。 |
| `screen_stocks` | 在已加载的某一交易日截面上执行 AND 条件，支持跨字段比较、预设、排序、命中数和缺失统计。覆盖以导入文件为准。 |
| `diagnose_stock` | 输出个股行情和技术指标证据。没有本地数据时尝试腾讯沪深个股历史日线，为避免误用未定版行情，不取当天。当前不推断资金流或题材。 |
| `search_documents` / `get_document` | 指定沪深股票代码可查巨潮公告索引，最多 90 个自然日；标题匹配只在上游首 30 条中执行，可能截断。公告详情返回原文链接，不声称已解读 PDF。研报、新闻、调研、董秘问答查询授权本地索引。 |
| `get_realtime` | 读取外部定时写入的同一时间戳报价帧，支持 `market`（市场宽度和涨停、炸板、跌停池）、`quotes`、`screen`。没有内置实时采集器，也不从日线冒充盘中行情。 |
| `predict_lianban_promotion` | 同名入口只返回已知结果的历史晋级频率，少于 30 条不输出百分比；不是次日个股预测，也不依赖模型。 |
| `check_financial_claims` | 沿用独立财报核查；不会把这里的报价或文档索引当作公告证据。 |

## 快速试用

在项目目录安装 `pip install -e '.[dev]'`，用两个真实公开日线样本体验。输出目录不能已有 `daily.csv`：

```bash
finchecker-market-import --output "$HOME/finchecker-sample" public-sample 600519 600900
FINCHECKER_MARKET_DIR="$HOME/finchecker-sample" finchecker-mcp
```

在 MCP 客户端将同一个 `FINCHECKER_MARKET_DIR` 配为服务器环境变量；`command` 可以填虚拟环境中 `finchecker-mcp` 的绝对路径。未配置目录时，可用数据目录、腾讯个股诊断、指定代码的巨潮公告检索与既有财报核查。

## 盘后市场快照

用有权限的 `TUSHARE_TOKEN` 批量导入沪、深、北日线；Token 只供导入进程使用，无需放进 MCP 配置。按官方文档，[`trade_cal`](https://tushare.pro/document/2?doc_id=26) 与 [`stock_basic`](https://tushare.pro/document/2?doc_id=25) 需 2,000 积分，并需有 [`daily`](https://tushare.pro/document/2?doc_id=27) 权限。未授权、达到行数上限或无法核验覆盖时，导入失败，不回退到样本。仅在北京时间 16:00 之后允许纳入当天，仍要求通过覆盖检查；不要把截面当作实时行情。

```bash
finchecker-market-import --output "$HOME/finchecker-history" tushare \
  --start 2025-11-01 --end 2026-11-19
```

- 对照上交所日历和 Tushare 股票名录的上市／退市区间。任一交易日整批缺失、代码越界，或已上市股票中任一市场有日线的比例低于 95%，均拒绝发布；名录分母可能含停牌，缺数不自动解释成停牌。它衡量的是相对供数方名录的覆盖，不能证明全市场逐股完整。
- 一次最多 401 个自然日，默认限速（约 46 次／分钟）。行情与 `daily-manifest.json` 一起发布到新的目录，不替换旧快照。验收清单逐日列沪、深、北分母和有行情数，以 SHA-256 绑定 CSV；`screen_stocks`、`diagnose_stock` 和 `query_dataset` 返回覆盖与时效。
- `current` 表示对齐已到发布时间的最近交易日；`stale` 表示落后。`historical_selection` 为主动选历史日，`unverified_calendar` 为没有有效覆盖当前时点的日历。Tushare 快照未对齐或清单中的日历已过期时，默认选股拒绝，显式指定 `trade_date` 可回看。
- Tushare 的 `pct_change` 使用上游 `pct_chg`（除权昨收口径）；没有该列的旧数据才用相邻成交收盘价计算。**日线价格与 MA／MACD 等仍是未复权的**，除权可能影响信号。月 MACD 的 DIF+DEA 至少要 34 个有效月周期；一年快照远远不够，仍需合法的长历史数据才能补齐。

### 长历史、估值与每日更新

macOS／Linux 上可选用 `tushare-sync`（首次需新目录），使用者自行申请 Token 和 [`daily_basic`](https://tushare.pro/document/2?doc_id=32) 权限，并通过本机环境提供 `TUSHARE_TOKEN`；不要把密钥写进命令历史、MCP 配置或仓库。首次同步建议从四年前开始，可选 `--end` 指定历史截止日：

```bash
finchecker-market-import --output "$HOME/finchecker-market" tushare-sync --start 2023-01-01
FINCHECKER_MARKET_DIR="$HOME/finchecker-market/current" finchecker-mcp
```

之后可手动运行下列命令，或由使用者接入自己的计划任务（建议北京时间 17:15 后）。项目不注册后台任务，不保存 Token。计划任务须能读取使用者自行配置的本机密钥，并以失败退出码告警：

```bash
finchecker-market-import --output "$HOME/finchecker-market" tushare-sync
```

- 首次至多导入 2,196 个自然日，逐日请求行情和估值，四年数据可能耗时数十分钟且受积分与频次约束；使用 `--end` 可分段同步。历史代码迁移、缺日或旧名录与上游行情不一致时会拒绝发布，不自动拼接代码。历史行情仍不复权，新股也不保证有足量的月线指标。
- 再运行只拉未导入的交易日，不改写旧行情；无新增交易日保留现状。17:00 前不纳入当天估值。导入失败时 `current` 保留旧版本；完整通过校验后才原子切换。每个快照包含完整历史、占用独立磁盘；旧版本不会自动删除，且盘后更新不处理供数方后来修订的历史行情。
- 估值按当日 `daily_basic` 的 `pe_ttm` 和 `total_mv`，总市值从万元换算为亿元；**不等同于动态 PE**，`pe_dynamic` 和 `industry` 不填。沪、深、北分别要求有总市值的股票至少占当日日线股票 95%，PE 缺失保留空值，另报覆盖；可用 `multi_period_value_ttm` 预设。`current` 目录仅管理盘后数据；自备研报或盘中快照时请另建数据目录。

也可直接准备下面的文件。一致的复权方式、标的范围、数据更新时间和许可由数据提供者保证；没有清单的 CSV 不宣称覆盖已审核。

| 文件 | 列 / 结构 |
|---|---|
| `daily.csv` | `trade_date,symbol,open,high,low,close,volume`，可选 `pct_change`（百分点）。日期 `YYYY-MM-DD`；成交量单位全库一致，不混用股和手。只对目标日有成交且有记录的股票筛选；历史零成交日不进入指标。 |
| `attributes.csv` | `trade_date,symbol,pe_dynamic,market_cap,industry`，可选 `pe_ttm`；市值用亿元。盘后最多用目标日数据，盘中严格使用早于当天的一帧，返回 `attributes_date`；缺失不取未来，也不逐股混用日期。 |
| `quotes.csv` | `as_of,symbol,price,pct_change,volume_ratio,limit_status,status`；可选 `board_height,limit_down_status,open,high,low,volume`。帧时间为带时区 ISO 字符串，`pct_change` 用百分点；`volume_ratio` 是供数方盘中量比，不用盘后量比替代。提供同口径盘中 OHLCV 和历史日线后才重算未完成周期指标。 |
| `limit_events.csv` | `trade_date,symbol,board_height,outcome_date,promoted`。以唯一的日期、代码记录板数及下一交易日结果，`promoted` 为 0 或 1；调用者应保证下一交易日口径，程序只接收严格晚于起始日且截止前已公布的结果。 |
| `documents.jsonl` | 每行一个对象：`{"ref":"local:demo-1","kind":"report","date":"2026-11-19","symbol":"600519","title":"示例","url":"https://example.com/report","summary":"允许使用的摘要"}`。只导入有权使用的文档，不在公开仓库打包付费资料。 |

盘中每帧应由采集方同一次生成所有市场的股票行，原子替换文件。`status` 为 `ok`、`suspended` 或 `no_trade`；涨停状态为 `untouched`、`touched`、`opened` 或 `sealed`，可选跌停状态为 `untouched`、`touched` 或 `sealed`。供应方需要正确提供涨停价/ST/上市规则与状态；不从涨幅推定涨停。仅取最新帧、仅统计 `ok`，不拿上一帧填缺股。北京时间常规交易时段内同日且不超过 180 秒才标为 `recent_snapshot`；上游负责节假日和交易状态，过期只供回看，不能盘中筛选。

## 筛选示例

给客户端的提问：「按盘后数据筛日线均线多头、周 MACD 多头、成交量超过前 5 个有效交易日均量 2 倍，按涨幅排序；把执行条件和样本数一起列出来。」等效调用：

```json
{
  "preset": "ma_macd_trend",
  "conditions": [{"field": "volume_ratio", "op": ">", "value": 2}],
  "sort_by": "pct_change",
  "limit": 20
}
```

- `{"field":"close","op":">","ref":"day_ma20"}` 是「当前收盘价在 20 日均线之上」；要筛「从下方或均线上向上穿越」，用 `day_cross_above_ma20 = 1`。对应含放量条件的预设分别是 `above_ma20_with_volume` 和 `cross_ma20_with_volume`，均要求 `volume_ratio >= 2`。
- `day_ma_bull = 1` 为 MA5 > MA10 > MA20 > MA60，`week_macd_bull = 1` 为周 DIF > DEA；`week_kdj_bull = 1` 为周 K > D。周／月按截止交易日聚合，会包含尚未走完的周或月，不用后续行情。
- MACD 采用 12/26/9 EMA、柱值 `2*(DIF-DEA)`；KDJ 用 9 周期 RSV、K/D 以首个有效 RSV 起算后按 1/3 平滑；RSI 用 1/N 指数平滑；布林带为 20 周期均线 ± 2 倍总体标准差。直接比较计算值，不按前台展示位数先四舍五入，也不承诺和其他产品同源、同值。
- 新股或数据不足返回空，不当成 0；条件无输入值不入选。响应提供执行条件、样本数、缺失数、命中数和是否截断；涨幅相同按代码补充排序。
