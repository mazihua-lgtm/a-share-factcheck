# A-Share Financial Research & News Fact-Checker with Anti-Hallucination MCP Server

面向 A 股财报和研报的事实核查工具。用规则抽取股票代码、报告期、指标、金额和同比，再与明确选择的数据源比对。默认使用有公告依据的有限数据快照；取不到依据就报告无法核实，不回退到模拟数据。核查结果只供人工研究，不提供交易功能。

## 复用的上游开源组件

本项目不重复造轮子, 以下组件直接使用:

| 组件 | 用途 | 来源 |
|------|------|------|
| AKShare | A股真实数据 (股票列表, 财务摘要) | https://github.com/akfamily/akshare |
| FastMCP | MCP 服务器框架 | https://github.com/jlowin/fastmcp |
| Pydantic | 数据模型与校验 | https://github.com/pydantic/pydantic |
| Rich | 终端表格输出 | https://github.com/Textualize/rich |
| pytest | 单元测试 | https://github.com/pytest-dev/pytest |

调研时发现的相关项目 (可作为后续扩展参考, 本次未复制代码):
- akshare-one-mcp (https://github.com/zwldarren/akshare-one-mcp): 基于 akshare 的A股 MCP 服务
- aktools (https://github.com/akfamily/aktools): akshare 的 HTTP API 与 MCP 服务
- openbb_akshare (https://github.com/finanalyzer/openbb_akshare): OpenBB 的 akshare 扩展

调研中未找到专门针对A股财报断言核查的现成项目, 因此核查规则部分为本项目自研.

## 架构与流水线

文本 -> extractor (正则规则抽取断言) -> verifier (数学与容差校验, 对比真值) -> DiscrepancyReport

- finchecker/models.py: FinancialClaim, VerificationResult, DiscrepancyReport
- finchecker/extractor.py: 股票代码, 报告期 (2025Q3, 2025FY), 指标, 金额 (支持 亿/万 单位), 同比/环比增速
- finchecker/verifier.py: 金额容差 0.5%, 增速容差 0.5 个百分点; 重算增速; 数量级错误检测; 符号颠倒检测; 比率公式校验
- finchecker/fetcher.py: DisclosureSource（公告摘录）、AkshareSource（第三方摘要）、MockSource（仅演示和测试）
- finchecker/cli.py: rich 表格命令行
- finchecker/mcp_server.py: FastMCP 服务, 暴露 check_financial_claims

## 错误类型 (issue_type)

- wrong_code: 股票代码未出现在所选数据源的股票列表中（有限公告快照未覆盖的股票不按错码处理）
- magnitude: 数量级错误 (如 万 与 亿 混淆)
- value_mismatch: 金额不一致
- inverted_growth: 同比或环比涨跌方向颠倒
- growth_mismatch: 增速数值不一致
- no_ground_truth / no_base_period: 无法核实

## 安装

```bash
pip install -e '.[dev]'
```

## 命令行

```bash
finchecker demo                         # 独立的虚构数据演示
finchecker --text '600519 2025年报：营业收入1688.38亿元，同比下降1.21%。' --json
finchecker --file examples/2025-disclosures.txt
finchecker --file report.txt --source akshare  # 显式选用第三方摘要
```

退出码：`0` 已提取断言全部一致，`1` 至少一项不一致，`2` 命令参数错误，`3` 未提取到断言、全部无法核实或仅部分可核实。`demo` 的退出码只表示演示运行成功。JSON 和 MCP 返回 `source`、`simulated`、`status`；`status` 为 `verified`、`mismatch`、`no_claims`、`unverifiable`、`incomplete` 或 `simulation`。`verified` 只表示**已提取**的断言与所选来源在容差内一致，不代表核过全文或所有用语。

## MCP 配置

```json
{
  "mcpServers": {
    "a-share-factcheck": {
      "command": "finchecker-mcp"
    }
  }
}
```

- `check_financial_claims(text, source='disclosure')`：源可显式改为 `akshare`，不能选 `mock`。
- `demo_financial_claims()`：仅运行内置虚构例子，返回 `simulated: true` 和 `status: simulation`。

## 公告依据

默认的 `disclosure` 是人工摘录的公开公告快照，不自动下载、更新或覆盖其他公司和年份。仅覆盖下表两家公司 `2025FY` 的营业收入、归母净利润及同比。结果逐条附公告标题、链接、PDF 页码和报告期；同比从同一张表的 2025 年与 2024 年可比金额重算。

| 公司 | 公开依据 | PDF 页码 |
|---|---|---|
| 贵州茅台 `600519` | [2025 年年度报告摘要 §3.1](https://static.cninfo.com.cn/finalpage/2026-04-17/1225114731.PDF) | 4 |
| 长江电力 `600900` | [2025 年年度报告摘要 §3.1](https://static.cninfo.com.cn/finalpage/2026-04-30/1225262042.PDF)，上年按调整后列 | 3 |

## 测试

```bash
pytest -q
```

## 注意事项

- MockSource 中的数据为测试用的虚构固定值, 不代表真实财务数据.
- AKShare 默认不调用；显式使用时，结果只能作为第三方摘要比对，未绑定原始公告。字段或唯一记录缺失时返回无法核实，且不把「营业总收入」当成「营业收入」。
- 验收时实测，AKShare 财务摘要中的贵州茅台 2025 年营业总收入约为 1,720.54 亿元，而公告的营业收入约为 1,688.38 亿元。这两个会计项目不能互换；同次查询还返回了重复指标行，适配器不会擅自选第一行。
- 真实数据模式目前只做年报金额与同比，不以累计期末值计算环比。规则抽取覆盖有限；建议一句只写一个股票代码、一个报告期、一个指标和一个金额。未抽取的语句不在结果之内。
