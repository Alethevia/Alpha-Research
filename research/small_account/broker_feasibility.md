# 平台路径与 40% 回撤约束

核对日期：2026-10-12（America/New_York）。本金是整个研究账户的 2000 美元，
不是每个券商各投入 2000 美元。持有账户不等于已经验证其 API、融资、期货或期权权限。

用户已扩大到任何金融产品。IBKR 的 E-nano 期货与期权路线现已进入产品筛选；
本表的初始平台分工不限制研究范围。最新上市状态、保证金参考和数据要求见
[跨产品研究说明](multi_asset_scope_20261013.md)。

| 平台 | 当前研究定位 | 已核实及待核实事项 |
| --- | --- | --- |
| Alpaca | 优先研究美股/ETF API 路线；公开加密货币数据用于先行成本筛查 | 官方提供交易和模拟接口。股票历史数据需要 API 凭据；公开 crypto 历史数据可无凭据读取。账户权限、购买力和行情套餐尚未核实。 |
| IBKR | 多资产扩展候选 | 官方提供 TWS、Web API。账户计划、交易权限、行情订阅及具体品种保证金需逐项核实。 |
| moomoo | API 备选路线 | 官方提供 OpenAPI/OpenD。服务实体与账户市场决定可交易产品，不能把全球产品列表当成美国账户权限。 |
| Fidelity | 暂不作为本轮自动下单接口 | 本次找到的官方零售资料主要介绍 Trader+ 等工具；尚未核实可供此账户使用的官方零售程序化下单 API。这不是断言 Fidelity 所有业务都没有 API。 |

## 会影响小账户设计的新规则

Alpaca 官方 2026-06-04 更新说明，其 Trading API 已实施新的日内保证金框架，
取消 PDT 次数与 25000 美元相关限制。符合条件、启用杠杆的账户最低 2000 美元
可获得相应日内购买力；订单仍受动态购买力、保证金与账户状态限制。
不应把“本金恰好 2000 美元”当成始终可以使用四倍杠杆的保证。

SEC Investor.gov 说明，新规生效日为 2026-06-04，券商有到 2027-10-20 的过渡期。
所以不能把 Alpaca 的实施状态直接推广到另外三家券商。

## 执行约束

- 40% 从账户历史最高净值计算：2000 美元对应 1200 美元参考底线；
  若最高净值达到 3000 美元，参考底线提高至 1800 美元。
- 研究默认单笔计划风险 1%；20% 回撤时新仓计划风险减半；30% 回撤时锁定暂停、
  取消新仓并请求退出。40% 是失败判据，不能通过裁剪收益让结果恰好停在那里。
- 20%/30%/1% 是工程研究默认值，不是用户额外指定值。未来需用执行证据评估。
- 暂停后不会因为价格反弹自动重启。跳空、断网、限价未成交仍可能使损失越过阈值。
- 原型只有研究计算和决策输出，没有券商下单或资金操作。

## 为什么首轮公开数据采用 BTC/ETH

这两个标的可立即取得 Alpaca 自身的公开历史价格数据，先检验固定 H1 基线
能否承受实际量级的成本。选择它们不是认定该路线最有希望达到目标。

官方最低交易量档 crypto maker/taker 费率分别为单边 0.15%/0.25%。首轮采用
主动成交单边 0.25%，再假设单边 0.10% 价差/滑点和每月 10 美元运行成本。
这些当前费用被投射到历史价格，不代表已经复原当年的费率。
crypto 不支持融资买入或做空，因此本轮只有现货多头、无杠杆。

Alpaca 明确说明 crypto K 线包含报价中间价，零成交时仍可有 OHLC。
因此真实历史价格筛查与真实可执行订单验证是两个阶段。后续仍需报价、数量、
接收时间及账户资产精度等数据；不把触价或中间价当作成交证据。

## 官方来源

- [Alpaca 新日内保证金规则，更新于 2026-06-04](https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/)
- [SEC Investor.gov：券商过渡期](https://www.investor.gov/introduction-investing/investing-basics/glossary/pattern-day-trader)
- [Alpaca Python SDK：行情认证](https://alpaca.markets/sdks/python/market_data.html)
- [Alpaca crypto 费率](https://docs.alpaca.markets/us/docs/crypto-fees)
- [Alpaca crypto 历史数据及中间价说明](https://docs.alpaca.markets/us/docs/historical-crypto-data-1)
- [Alpaca crypto 订单、融资及做空限制](https://docs.alpaca.markets/us/docs/crypto-trading)
- [IBKR API 官方文档](https://www.interactivebrokers.com/docs/)
- [moomoo API 官方文档](https://openapi.moomoo.com/moomoo-api-doc/en/intro/intro.html)
- [Fidelity 官方交易工具介绍](https://www.fidelity.com/trading/advanced-trading-tools/overview)
