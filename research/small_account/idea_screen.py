#!/usr/bin/env python3
"""Validate a hypothesis catalogue and render desk screening, not performance.

The P/B/C labels are authored judgments, not discovered by a fitted score. This
program checks their structure; it cannot establish semantic independence,
source accuracy, account eligibility or positive expectancy.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

try:
    from .product_screen import screen
except ImportError:
    from product_screen import screen

HERE = Path(__file__).resolve().parent


def validate_catalogue(catalogue, queue):
    ideas = catalogue['ideas']
    expected = [f'I{i:03}' for i in range(1, 101)]
    if [x['id'] for x in ideas] != expected:
        raise ValueError('Catalogue must contain I001 through I100 in order')
    for field in ('title', 'mechanism'):
        values = [x[field].strip() for x in ideas]
        if len(set(values)) != 100 or not all(values):
            raise ValueError(f'Duplicate or empty {field}; text uniqueness is not semantic independence')
    families = {x['id'] for x in catalogue['families']}
    if families != {f'F{i:02}' for i in range(1, 21)}:
        raise ValueError('Expected 20 mechanism families')
    if Counter(x['family'] for x in ideas) != Counter({f: 5 for f in families}):
        raise ValueError('Every family must contain five questions')
    source_ids = [x['id'] for x in catalogue['sources']]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError('Duplicate source identifier')
    for row in ideas:
        if row['screen'] not in {'P', 'B', 'C'}:
            raise ValueError('Unknown screening label')
        for field in ('expression', 'required_data', 'falsifier', 'blocker', 'target_role'):
            if not isinstance(row[field], str) or not row[field].strip():
                raise ValueError(f'Missing {field}')
        if not set(row['source_ids']).issubset(source_ids):
            raise ValueError('Unresolved source reference')
        if row['profitability'] != 'UNTESTED_IN_THIS_CATALOGUE' or row['live_execution'] != 'UNVERIFIED':
            raise ValueError('Desk screen cannot certify performance or execution')
    if catalogue['constraints']['initial_capital_usd'] != 2000 or catalogue['constraints']['maximum_drawdown_from_peak'] != .4:
        raise ValueError('User constraints changed')
    if catalogue['constraints']['live_execution_authorized'] is not False:
        raise ValueError('No live execution authorization')
    cards = queue['cards']
    if len({c['id'] for c in cards}) != len(cards):
        raise ValueError('Duplicate validation card')
    if {c['id'] for c in cards} != {x['id'] for x in ideas if x['screen'] == 'P'}:
        raise ValueError('Priority labels and validation cards disagree')
    if queue['new_backtests_run'] != 0 or queue['validated_profitable'] != 0 or queue['execution_authorized'] is not False:
        raise ValueError('This catalogue has no new backtest or validated strategy')
    for c in cards:
        if c['performance_ready'] is not False or c['execution_ready'] is not False:
            raise ValueError('Unresolved intake cannot be marked ready')
        if c['wave'] not in (1, 2, 3):
            raise ValueError('Unknown research wave')
        for field in ('question', 'current_evidence', 'minimum_check', 'control', 'evaluation_plan', 'stop_condition', 'why_priority'):
            if not c[field].strip():
                raise ValueError(f'Incomplete card: {field}')
    return dict(sorted(Counter(x['screen'] for x in ideas).items()))


def geometric_hurdle(opportunities, multiple=11.0, risk_fraction=.01):
    if isinstance(opportunities, bool) or not isinstance(opportunities, int) or opportunities <= 0:
        raise ValueError('Opportunities must be a positive integer')
    if not math.isfinite(multiple) or multiple <= 1 or not math.isfinite(risk_fraction) or not 0 < risk_fraction < 1:
        raise ValueError('Invalid target multiple or risk fraction')
    growth = math.expm1(math.log(multiple) / opportunities)
    return {'opportunities': opportunities, 'equal_net_equity_growth_per_opportunity': growth,
            'equal_net_R_per_opportunity_at_risk_fraction': growth / risk_fraction}


def illustrative_friction(round_trips, notional=1900.0, slip_bps=5.0, fee_per_order=.02, annual_fixed=120.0):
    if isinstance(round_trips, bool) or not isinstance(round_trips, int) or round_trips <= 0:
        raise ValueError('Round trips must be a positive integer')
    if any(not math.isfinite(x) or x < 0 for x in (notional, slip_bps, fee_per_order, annual_fixed)):
        raise ValueError('Costs must be finite and nonnegative')
    variable = 2 * notional * slip_bps / 10000 + 2 * fee_per_order
    return {'round_trips': round_trips, 'variable_usd_per_round_trip': variable,
            'fixed_allocation_usd_per_round_trip': annual_fixed / round_trips,
            'total_usd_per_round_trip': variable + annual_fixed / round_trips,
            'fraction_of_initial_20_usd_risk': (variable + annual_fixed / round_trips) / 20}


def unit_examples():
    # Synthetic inputs only. Unknown margin remains unknown, never default zero.
    common = {'currency': 'USD', 'quantity_step': 1, 'minimum_quantity': 1, 'verified': {},
              'risk_basis': 'Illustrative planned amount, not a guaranteed stop or verified account funding'}
    cases = [
        {**common, 'id': 'SYNTHETIC_ETF_100_PRICE_1_5_STOP', 'kind': 'etf',
         'loss_before_cost_per_unit_usd': 1.5, 'capital_per_unit_usd': 100,
         'variable_round_trip_cost_per_unit_usd': .1, 'fixed_round_trip_cost_usd': .04},
        {**common, 'id': 'SYNTHETIC_OPTION_018_PREMIUM_X100', 'kind': 'long_option',
         'loss_before_cost_per_unit_usd': 18, 'capital_per_unit_usd': 18,
         'variable_round_trip_cost_per_unit_usd': 2, 'fixed_round_trip_cost_usd': 0},
        {**common, 'id': 'SYNTHETIC_OPTION_025_PREMIUM_X100', 'kind': 'long_option',
         'loss_before_cost_per_unit_usd': 25, 'capital_per_unit_usd': 25,
         'variable_round_trip_cost_per_unit_usd': 2, 'fixed_round_trip_cost_usd': 0},
        {**common, 'id': 'SYNTHETIC_NES_20_POINT_STOP_UNKNOWN_MARGIN', 'kind': 'future',
         'loss_before_cost_per_unit_usd': 10, 'capital_per_unit_usd': None,
         'variable_round_trip_cost_per_unit_usd': 2.5, 'fixed_round_trip_cost_usd': 0},
    ]
    return [{'input': p, 'result': screen(p, 20, 2000, 100)} for p in cases]


def make_result(catalogue, queue, catalogue_bytes, queue_bytes):
    counts = validate_catalogue(catalogue, queue)
    return {
        'evidence_type': 'desk_screen_and_analytic_or_synthetic_arithmetic_not_backtest',
        'catalogue_sha256': hashlib.sha256(catalogue_bytes).hexdigest(),
        'validation_queue_sha256': hashlib.sha256(queue_bytes).hexdigest(),
        'counts': counts, 'ideas': 100, 'families': 20, 'source_anchors': len(catalogue['sources']),
        'new_performance_backtests': 0, 'validated_profitable': 0, 'execution_authorized': False,
        'priority_order': [c['id'] for c in sorted(queue['cards'], key=lambda c: c['wave'])],
        'equal_growth_hurdles': [geometric_hurdle(n) for n in (8, 12, 50, 100, 250, 500, 1000)],
        'friction_examples': [illustrative_friction(n) for n in (8, 50, 250, 1000)],
        'synthetic_product_examples': unit_examples(),
        'multiple_search_illustration': {
            'independent_null_tests': 100, 'nominal_false_positive_rate_each': .05,
            'probability_at_least_one_false_positive_under_independence': 1-.95**100,
            'bonferroni_alpha_for_exactly_100_prespecified_valid_tests': .05/100,
            'not_project_estimates': True,
            'limits': 'Actual ideas are dependent and legacy variants, directions, metrics and parameter searches extend the testing family; no actual p-values or correction have been calculated.'},
        'checks': 'IDs, text uniqueness, family counts, nonempty falsifiers/blockers, source references, priority-card consistency and no invented performance. Semantic independence and source truth require substantive review.'}


def render_report(c, q, r):
    lines = ['# 100个量化研究方向：先提出命题，再寻找反例', '',
        '日期：2026-10-13（America/New_York）。目标：2000美元本金、完整年度净收益+1000%、峰值回撤容忍40%；不追加本金。', '',
        '**本轮完成100项桌面初筛，不是100项回测。12项优先做最小证伪，61项补证据后再评估，27项在当前资金/权限/设施下暂不推进。新增收益回测0项，已验证盈利0项。**', '',
        '## 怎么理解“100个不同方向”', '',
        '这是一份20个机制家族、100个区分性研究命题的清单，不是100个统计独立的alpha，也不声称都是学术上从未有人研究的发现。不同编号必须改变关键可检验的因果链、现金流权利或交易约束；换RSI参数、周期、币种或杠杆不另算一个方向。仍有关联的命题按家族和共同风险标注，不把命名差异当分散化。', '',
        '“P优先”表示用较低成本获得证伪信息的研究次序，不表示预计收益最高或已可实盘。“B待证”不等于不可行。“C暂停”是当前实施约束判断，不是证明机构或其他条件下一定不能做。所有100项的盈利和账户执行资格都尚未验证。', '',
        '## 数学解题式研究流程', '',
        '1. **写命题与前提。** 谁因什么约束支付收益？我们何时能看到信息、用哪个真实工具捕获？',
        '2. **先检查必要条件。** 数据是否事前可得、单位风险和资金是否够、利润是否超过完整成本；任何一条未知都保留未知。',
        '3. **寻找最小反例。** 用延迟、买卖价、删去最大赢家、错时数据、随机匹配事件或简单基准攻击解释；不是每次失败都加过滤器。',
        '4. **冻结后求证。** 明确参数、数据哈希、样本窗口、真实表达、成本、对照、主指标和拒绝规则，再登记并计算。目录发布不等于绩效试验已预注册。',
        '5. **复核推导与实现。** 检查信号时间、现金流、整数单位、保证金、指派和不可成交情景；单元测试不证明市场优势。',
        '6. **尝试推翻推广结论。** 新时间/新资产、事件分组、成本压力与独立前瞻；选型用过的历史不再叫盲测。', '',
        '能严格证明的是给定前提下的现金流恒等式和程序性质；市场条件与收益分布会改变，只能积累可重复、能被推翻的经验支持，不能把回测称为数学证明。这里沿用公开研究的“提出—攻击—修订—复核”精神，不声称有OpenAI内部量化配方。', '',
        '## 目标算术：先看机会是否足够', '',
        '11倍终值要求累计对数增长达到ln(11)。若每个机会净值都按同一正比例g增长，且除此之外没有损失或费用，则 g=11^(1/N)-1。下表只描述这种等效增长，不是胜率、收益预测或实际机会数量。R取交易前净值的1%；真实有输有赢、现金占用与停机后不能用此表替代回测。', '',
        '| 一年机会数N（假设） | 每机会等效净值增长 | 相对1%计划风险的等效净赚R |',
        '| ---: | ---: | ---: |']
    for x in r['equal_growth_hurdles']:
        lines.append(f"| {x['opportunities']} | {x['equal_net_equity_growth_per_opportunity']:.3%} | {x['equal_net_R_per_opportunity_at_risk_fraction']:.3f} R |")
    lines += ['', '少量事件可成为组合组件，但不能只凭一两笔赚钱就外推十倍；增加交易次数也不会自动增加正期望。组合里相关的同日信号不是新增独立机会，所有工具共享同一2000美元。', '',
        '### 初始规模下的成本例子（假设，不是券商报价）', '',
        '固定每次1900美元名义金额、每侧5bp滑点、每单0.02美元、全年120美元固定费，往返可变摩擦为1.94美元。下表只把固定费用平均分摊；实际仓位、净值和费用会变化，不能当策略全年收益模拟。', '',
        '| 年往返次数 | 往返摩擦+平均固定费 | 占初始20美元计划风险 |', '| ---: | ---: | ---: |']
    for x in r['friction_examples']:
        lines.append(f"| {x['round_trips']} | ${x['total_usd_per_round_trip']:.2f} | {x['fraction_of_initial_20_usd_risk']:.2%} |")
    lines += ['', '四个额外合成单位检查：100美元ETF、1.5美元止损距离的假设在费用下最多容纳12股；0.18美元报价、100乘数的长权利金加2美元费用刚好20美元；0.25美元则要27美元而超预算；NES假设20点止损加2.50美元摩擦为12.50美元，但保证金未知，程序保持资金资格未知。期权还须处理行权/指派与退出，计划止损不限制跳空损失。所有例子均禁止据此下单。', '',
        '## 已做的一手资料核查：证明机制存在，不证明赚钱', '',
        '- SEC公开接口支持申报/财报资料；它不替我们提供完整历史一致预期、退市股票池、第一次新闻发布时间或真实成交。',
        '- 历史Wix要约文件确有少于100股、全部有效提交的优先条款，原截止2026-04-01。这只是历史机制证据，不表示今天有可参与或可获利的同样要约。',
        '- 分拆发行人文件明确讨论原持有人/指数跟踪账户可能出售分拆股；这仍不能证明会反弹。',
        '- 杠杆/反向ETF存在每日目标重置，但既不保证预测得到实际对冲订单，也不能把正向ETF回报简单乘负号充当反向工具历史。',
        '- COT持仓观测日与发布日不同，FIRMS卫星观测与到达时间不同；两个方向都必须按后者可见时间建模。',
        '- 期权指派、盘口订阅、期货整数单位/可变保证金、链上清算与流动性库存损失分别存在门槛；不因文档可读就认定用户已获权限。', '',
        '核查了20个一手资料锚点，不是完成100个方向的系统文献复核。无资料锚点的项仍须查清具体数据/规则。完整链接与证明范围在文末及JSON；来源不背书本项目的方向判断。', '',
        '## 优先12项：按信息价值安排，不按想象收益打分', '',
        '第一批只安排I037、I010、I019的数据/规则最小检查：价格残差假说、公开申报信息和小账户容量机制彼此差异较大。第二批为I001、I023、I076；其余六项第三批。它们目前均未具备完整绩效深验输入，不能称“12个可盈利策略”。没有启动后台任务、订阅付费数据或交易账户。', '',
        '| 次序批次 | 编号/方向 | 先做的最小证伪 | 关键对照 |', '| --- | --- | --- | --- |']
    by_id={x['id']:x for x in c['ideas']}
    for card in sorted(q['cards'], key=lambda x:x['wave']):
        lines.append(f"| {card['wave']} | {card['id']} {by_id[card['id']]['title']} | {card['minimum_check']} | {card['control']} |")
    lines += ['', '每项的当前证据、样本隔离要求和停止条件详见 [idea_validation_queue.json](idea_validation_queue.json)。获得必要数据后，再按 [experiment_registry.py](experiment_registry.py) 单独冻结试验。若数据收费、需要新权限或账户配合，先报告阻碍，不绕过限制。', '',
        '## 防止“100次搜索总能找到一个赢家”', '',
        f"仅作数学说明：若100项检验全部无真实效果且彼此独立，每项误报率5%，至少误报一次的概率为 {r['multiple_search_illustration']['probability_at_least_one_false_positive_under_independence']:.2%}。本目录不独立，且尚未做这100项统计检验，这不是本项目的误报率估计。",
        '必须保留全部尝试、方向、参数和指标；现有H1/M2/H3共84个账户情景、3个H3/期货诊断及旧仓库探索不能清零。对恰好100个事先固定的有效检验，Bonferroni单项阈值0.0005只是一个保守示例，不可机械用于实际更多搜索。正式分析须先定义检验族、主指标及依赖结构，再选多重检验方法；样本不足就保留不确定，不靠更高杠杆弥补。', '',
        '以整场宏观事件、整次公司事件、整次火灾或连续持仓块为重采样单元；不能把相关ETF、不同费用情景或同一事件多根K线当独立样本。前瞻纸面成交仍需订单时间/拒单/部分成交核对。40%是容忍线，20%降风险、30%锁定暂停是既有研究默认；这些不保证实际损失不会跳过阈值。', '',
        '## 全部100个方向', '',
        'P=优先最小证伪；B=先补证据；C=当前不推进。每个收益来源都是待检验假说，不是事实性盈利断言。']
    for family in c['families']:
        lines += ['', f"### {family['id']} · {family['name']}", '']
        for row in [i for i in c['ideas'] if i['family']==family['id']]:
            src=', '.join(row['source_ids']) or '具体数据/规则待核查'
            lines += [f"#### {row['id']} · {row['title']}〔{row['screen']}〕", '',
                f"假说：{row['mechanism']}", '',
                f"- 表达/持有期：{row['expression']}。",
                f"- 必需证据：{row['required_data']}。",
                f"- 初筛原因：{row['blocker']}",
                f"- 淘汰条件：{row['falsifier']}",
                f"- 目标角色：{row['target_role']}。数据/规则锚点：{src}。", '']
    lines += ['## 一手资料与支持范围', '', '| ID | 来源 | 本轮实际核实范围 |', '| --- | --- | --- |']
    for s in c['sources']:
        lines.append(f"| {s['id']} | [{s['title']}]({s['url']}) | {s['verified_scope']} |")
    lines += ['', '## 机器可读记录与复现', '',
        '[100项原始目录](idea_catalogue_100.json) · [12项深验前置方案](idea_validation_queue.json) · [初筛算术与结构核验](idea_screen_results_20261013.json)。', '',
        '```bash', 'python3 research/small_account/idea_screen.py --output artifacts/small-account/idea_screen_reproduction.json --report artifacts/small-account/idea_100_reproduction.md',
        "python3 -m unittest discover -s research/small_account -p 'test_*.py' -v", '```', '',
        '程序核对100个编号、20个家族、字段、资料引用、优先项一致性与算术；不能自动证明文字语义真正独立、资料真实或市场会赚钱。全部筛选属于本次研究判断，可被新证据改变；改变须保留原因和历史。', '']
    return '\n'.join(lines)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--catalogue', type=Path, default=HERE/'idea_catalogue_100.json')
    p.add_argument('--queue', type=Path, default=HERE/'idea_validation_queue.json')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--report', type=Path, required=True)
    a=p.parse_args()
    cb,qb=a.catalogue.read_bytes(),a.queue.read_bytes()
    c,q=json.loads(cb),json.loads(qb)
    result=make_result(c,q,cb,qb)
    for path in (a.output,a.report):
        if path.exists():
            raise ValueError(f'Preserve existing output; select a new path: {path}')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.report.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    a.report.write_text(render_report(c,q,result))
    print(json.dumps({k:result[k] for k in ('ideas','families','counts','source_anchors','new_performance_backtests','validated_profitable')}))


if __name__=='__main__':
    main()
