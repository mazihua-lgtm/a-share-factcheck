# A-Share Financial Research & News Fact-Checker with Anti-Hallucination MCP Server

面向A股财报与研报的事实核查工具. 从文本中抽取股票代码、报告期、指标、金额与同比/环比增速, 再用确定性的规则和数学校验与真实数据源比对, 找出大模型常见的幻觉: 增速涨跌颠倒, 数量级错误, 股票代码张冠李戴等. 通过 MCP 暴露 check_financial_claims 工具, 可供 AI 助手在输出前调用.

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
- finchecker/fetcher.py: GroundTruthSource 抽象, MockSource (测试夹具), AkshareSource (真实数据)
- finchecker/cli.py: rich 表格命令行
- finchecker/mcp_server.py: FastMCP 服务, 暴露 check_financial_claims

## 错误类型 (issue_type)

- wrong_code: 股票代码不存在或与断言不符
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
finchecker                              # 对内置样例文本运行 (mock 数据源)
finchecker --text '600519 2025年三季报：营业收入130亿元，同比增长8.3%。'
finchecker --file report.txt --source akshare
finchecker --json
```

退出码: 全部通过为 0, 存在失败为 1.

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

工具: check_financial_claims(text, source='mock'|'akshare')

## 测试

```bash
pytest -q
```

## 注意事项

- MockSource 中的数据为测试用的虚构固定值, 不代表真实财务数据.
- AkshareSource 的字段名基于 akshare 当前接口, 金额单位假定为元, 上线前需用真实数据回归验证.
- 抽取为规则实现, 覆盖的句式有限, 复杂表述可能抽取不全.
