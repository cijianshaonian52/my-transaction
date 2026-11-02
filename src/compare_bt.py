# -*- coding: utf-8 -*-
"""对比 backtrader 与 pandas 回测结果一致性

用法:
    .venv\\Scripts\\python.exe compare_bt.py
"""
import os, sys, glob
import pandas as pd
import backtrader as bt

# 加载策略文件
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_SRC_DIR)   # stockdb_csv 在 src 上一级（项目根）
strat_file = os.path.join(_SRC_DIR, 'strategy_normal.py')
_orig_argv = sys.argv
sys.argv = [strat_file, '600206.SH']
ns = {'__file__': os.path.abspath(strat_file), 'sys': sys}
src = open(strat_file, encoding='utf-8').read()
for marker in ['# 创建 cerebro', '# ====.*引擎', '# 引擎']:
    if marker in src:
        src = src.split(marker)[0]
        break
exec(src, ns)
sys.argv = _orig_argv

INIT_CASH = ns['INIT_CASH']
COMMISSION = ns['COMMISSION']
MIN_COMM = ns['MIN_COMM']
STAMP = ns['STAMP']
AStockCommission = ns['AStockCommission']
FullLotSizer = ns['FullLotSizer']
StrategyClass = ns['MyStrategy']

# 复用 scan_signals 的 pandas 回测
import scan_signals as ss

YEAR_START = pd.Timestamp('2026-01-01')
YEAR_END = pd.Timestamp('2026-12-31')
PRE_START = YEAR_START - pd.Timedelta(days=60)


def run_bt(df):
    """backtrader 回测（今年）"""
    seg = df[(df.index >= PRE_START) & (df.index <= YEAR_END)].copy()
    if len(seg) < 25:
        return 0.0, 0.0, 0

    cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
    cerebro.broker.setcash(INIT_CASH)
    cerebro.broker.set_coc(True)
    cerebro.broker.set_coo(True)
    cerebro.broker.set_checksubmit(False)
    cerebro.broker.addcommissioninfo(AStockCommission())
    cerebro.addstrategy(StrategyClass)
    cerebro.addsizer(FullLotSizer)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade')
    data = bt.feeds.PandasData(dataname=seg, fromdate=PRE_START, todate=YEAR_END)
    cerebro.adddata(data)
    results = cerebro.run()
    strat = results[0]

    final = cerebro.broker.getvalue()
    ret = (final / INIT_CASH - 1) * 100

    ta = strat.analyzers.trade.get_analysis()
    try:
        n_trades = ta.total.closed
    except (AttributeError, KeyError):
        n_trades = 0
    try:
        n_won = ta.won.total
    except (AttributeError, KeyError):
        n_won = 0
    win_rate = (n_won / n_trades * 100) if n_trades > 0 else 0
    return ret, win_rate, n_trades


def main():
    # 选取不同特征的股票测试 (文件名前缀, 显示代码, 名称)
    test_codes = [
        ('SH#600206', '600206.SH', '有研新材'),
        ('SH#603186', '603186.SH', '华正新材'),
        ('SH#600487', '600487.SH', '亨通光电'),
        ('SZ#003036', '003036.SZ', '泰坦股份'),
        ('SH#600176', '600176.SH', '中国巨石'),
        ('SZ#002667', '002667.SZ', '*ST威领'),
        ('SH#600004', '600004.SH', '白云机场'),
        ('SZ#000001', '000001.SZ', '平安银行'),
        ('SH#600519', '600519.SH', '贵州茅台'),
        ('SZ#002428', '002428.SZ', '云南锗业'),
    ]

    print(f'{"代码":<12}{"名称":<10} | {"BT收益%":>10}{"PD收益%":>10}{"差异":>9} | {"BT胜率":>8}{"PD胜率":>8} | {"BT笔数":>6}{"PD笔数":>6}  一致')
    print('-' * 110)

    all_match = True
    for file_prefix, ts_code, name in test_codes:
        filepath = os.path.join(PROJECT_ROOT, 'stockdb_csv', f'{file_prefix}.csv')
        if not os.path.exists(filepath):
            print(f'{ts_code:<12}{name:<10} | 文件不存在')
            continue

        df, _ = ss.load_local_csv(filepath)
        if df is None or len(df) < 25:
            continue

        bt_ret, bt_wr, bt_n = run_bt(df)
        pd_ret, pd_wr, pd_n, _pd_sharpe = ss.run_backtest(df)  # 第4项为回测夏普（本对账不用）

        diff = pd_ret - bt_ret
        match = '✓' if abs(diff) < 1.0 and bt_n == pd_n else '✗'
        if match == '✗':
            all_match = False

        print(f'{ts_code:<12}{name:<10} | '
              f'{bt_ret:>+9.2f}%{pd_ret:>+9.2f}%{diff:>+8.2f}% | '
              f'{bt_wr:>7.1f}%{pd_wr:>7.1f}% | {bt_n:>6}{pd_n:>6}  {match}')

    print('-' * 105)
    if all_match:
        print('结论：两种回测结果基本一致（收益差异<1%且交易数相同）')
    else:
        print('结论：存在差异，主要原因可能是：')
        print('  1. backtrader 在 next_open 中以开盘价买入（开盘上MA5信号）')
        print('     pandas 简化版统一以收盘价买入')
        print('  2. backtrader 当天买入当天不检查卖出，pandas 也一致但撮合时点有细微差别')
        print('  3. FullLotSizer 资金计算时点不同（开盘价 vs 收盘价影响可买股数）')


if __name__ == '__main__':
    main()
