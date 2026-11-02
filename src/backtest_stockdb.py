# -*- coding: utf-8 -*-
"""批量回测 stockdb_csv 目录中的股票数据（默认 2026 年，可用 --start/--end 改窗口）

用法:
    .venv\\Scripts\\python.exe backtest_stockdb.py
    .venv\\Scripts\\python.exe backtest_stockdb.py --sort win_rate
    .venv\\Scripts\\python.exe backtest_stockdb.py --profit-only
    .venv\\Scripts\\python.exe backtest_stockdb.py --timing
    .venv\\Scripts\\python.exe backtest_stockdb.py --start 2024-01-01 --end 2024-12-31
    .venv\\Scripts\\python.exe backtest_stockdb.py --strategy main.py        # 测影线策略

大盘择时默认关闭，--timing 开启（HS300 MA20 exit + 涨停顺延）。
"""
import os, sys, glob, argparse, pandas as pd, backtrader as bt

# ── 参数解析 ──
parser = argparse.ArgumentParser(description='批量回测 stockdb_csv 目录')
parser.add_argument('--strategy', default='strategy_normal.py',
                    help='策略文件（默认 strategy_normal.py；影线策略用 main.py）')
parser.add_argument('--sort', default='return', choices=['return', 'win_rate', 'trades'], help='排序方式')
parser.add_argument('--profit-only', action='store_true', help='只显示盈利股票')
parser.add_argument('--timing', action='store_true', help='开启大盘择时（HS300 MA20 exit + 涨停顺延；默认关闭）')
parser.add_argument('--no-timing', action='store_true', help=argparse.SUPPRESS)  # 旧参数兼容（显式关闭）
parser.add_argument('--start', default=None, help='回测起始日 YYYY-MM-DD（默认 2026-01-01，预取60天预热）')
parser.add_argument('--end', default=None, help='回测结束日 YYYY-MM-DD（默认 2026-12-31）')
args = parser.parse_args()

# 必须在 exec 策略文件（其 import market_timing）之前设置；显式参数优先于环境变量
if args.no_timing:
    os.environ['MARKET_TIMING'] = 'off'
if args.timing:
    os.environ['MARKET_TIMING'] = '000300.SH:20:exit'

# ── 加载策略文件（只取策略类定义部分）──
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
strat_file = args.strategy
if not os.path.exists(strat_file):
    # 只给文件名时（默认 strategy_normal.py）到 src/ 同目录找
    _cand = os.path.join(_SRC_DIR, strat_file)
    if os.path.exists(_cand):
        strat_file = _cand
    else:
        sys.exit(f'[错误] 找不到策略文件：{args.strategy}（可选 strategy_normal.py / main.py）')
sys.argv = [strat_file, '600206.SH']
ns = {'__file__': os.path.abspath(strat_file), 'sys': sys}
src = open(strat_file, encoding='utf-8').read()
for marker in ['# 创建 cerebro', '# ====.*引擎', '# 引擎']:
    if marker in src:
        src = src.split(marker)[0]
        break
exec(src, ns)

INIT_CASH = ns['INIT_CASH']
AStockCommission = ns['AStockCommission']
FullLotSizer = ns['FullLotSizer']
# 自动检测策略类（strategy_normal.py=MyStrategy，main.py=PullbackStrategy）
StrategyClass = ns.get('MyStrategy') or ns.get('PullbackStrategy')
if StrategyClass is None:
    for v in ns.values():
        if isinstance(v, type) and issubclass(v, bt.Strategy):
            StrategyClass = v
            break
if StrategyClass is None:
    sys.exit('[错误] 策略文件中未找到 bt.Strategy 子类')

# 回测区间（默认今年；预取60自然日预热，预热段同样参与交易，与 scan_signals 口径一致）
START_DATE = pd.Timestamp(args.start) if args.start else pd.Timestamp('2026-01-01')
END_DATE = pd.Timestamp(args.end) if args.end else pd.Timestamp('2026-12-31')
PRE_START = START_DATE - pd.Timedelta(days=60)

STOCKDB_DIR = os.path.join(os.path.dirname(_SRC_DIR), 'stockdb_csv')   # 项目根（src 上一级）


def load_local_csv(filepath):
    """加载 stockdb_csv 逐股 CSV（GBK编码，tab+空格混合分隔，首行股票名、第2行表头）
    返回 (df, 股票名)；失败返回 (None, '')"""
    try:
        raw = open(filepath, 'r', encoding='gbk').read()
        lines = raw.strip().split('\n')
        # 第1行形如 "600487 亨通光电 日线 前复权"，取第2段作股票名（ST 涨停5%识别用）
        stock_name = ''
        first_tokens = [t for t in lines[0].replace('\t', ' ').split(' ') if t.strip()]
        if len(first_tokens) >= 2:
            stock_name = first_tokens[1].strip()
        # 第2行是表头
        if len(lines) < 3:
            return None, stock_name
        # 解析表头：用tab和空格分割
        headers = [h.strip() for h in lines[1].replace('\t', ' ').split(' ') if h.strip()]
        # 解析数据行
        data = []
        for line in lines[2:]:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.replace('\t', ' ').split(' ') if p.strip()]
            if len(parts) < 6:
                # 尝试逗号分割
                parts = [p.strip() for p in line.split(',') if p.strip()]
            if len(parts) < 6:
                continue
            data.append(parts[:7] if len(parts) >= 7 else parts + [None] * (7 - len(parts)))

        df = pd.DataFrame(data, columns=['trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
        df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')
        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=['trade_date', 'close']).sort_values('trade_date').set_index('trade_date')
        return df, stock_name
    except Exception:
        return None, ''


def run_backtest(df, ts_code, stock_name=''):
    """对单只股票执行回测"""
    seg = df[(df.index >= PRE_START) & (df.index <= END_DATE)].copy()
    if len(seg) < 25:
        return None

    # 策略类方法的 __globals__ 即 ns：逐票注入真实代码/名称，
    # 保证涨停顺延按板块识别（主板10/创业科创20/北交所30/ST5），否则全用占位 600206.SH
    ns['STOCK_CODE'] = ts_code
    if 'STOCK_NAME' in ns:
        ns['STOCK_NAME'] = stock_name

    cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
    cerebro.broker.setcash(INIT_CASH)
    cerebro.broker.set_coc(True)
    cerebro.broker.set_coo(True)
    cerebro.broker.set_checksubmit(False)
    cerebro.broker.addcommissioninfo(AStockCommission())
    cerebro.addstrategy(StrategyClass)
    cerebro.addsizer(FullLotSizer)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade')
    data = bt.feeds.PandasData(dataname=seg, fromdate=PRE_START, todate=END_DATE)
    cerebro.adddata(data)
    run_results = cerebro.run()
    strat = run_results[0]

    final = cerebro.broker.getvalue()
    ret = (final / INIT_CASH - 1) * 100

    ta = strat.analyzers.trade.get_analysis()
    # 零交易时 backtrader 的 autodict 取不存在的键会抛 KeyError（hasattr 挡不住），需 try
    try:
        n_trades = ta.total.closed
    except (KeyError, AttributeError):
        n_trades = 0
    try:
        n_won = ta.won.total
    except (KeyError, AttributeError):
        n_won = 0
    win_rate = (n_won / n_trades * 100) if n_trades > 0 else 0

    return {'code': ts_code, 'return': ret, 'final': final,
            'trades': n_trades, 'won': n_won, 'win_rate': win_rate}


def main():
    files = sorted(glob.glob(os.path.join(STOCKDB_DIR, '*.csv')))
    timing_desc = ns['mt'].describe() if 'mt' in ns else '未知'
    print(f'策略: {strat_file}（{StrategyClass.__name__}）  初始资金: {INIT_CASH:,.0f}元')
    print(f'回测区间: {START_DATE.date()} ~ {END_DATE.date()}'
          f'（feed 预取自 {PRE_START.date()} 预热）')
    _mt = ns['mt']
    print(f'大盘择时: {timing_desc}'
          + ('（--timing 已开启）' if args.timing
             else '（环境变量 MARKET_TIMING 已开启）' if _mt.is_enabled()
             else '（--no-timing 已关闭）' if args.no_timing
             else '（默认关闭，--timing 可开启）'))
    print(f'共 {len(files)} 个文件\n')

    results = []
    for i, f in enumerate(files):
        basename = os.path.basename(f)
        parts = basename.replace('.csv', '').split('#')
        if len(parts) != 2:
            continue
        market, code = parts[0], parts[1]
        ts_code = f'{code}.{market}'

        df, stock_name = load_local_csv(f)
        if df is None or len(df) == 0:
            continue

        try:
            res = run_backtest(df, ts_code, stock_name)
            if res:
                results.append(res)
                sign = '+' if res['return'] >= 0 else ''
                print(f'[{i+1}/{len(files)}] {ts_code}: {sign}{res["return"]:.2f}%  ({res["trades"]}笔)')
        except Exception as e:
            print(f'[{i+1}/{len(files)}] {ts_code}: ERROR {e}')

    print(f'\n===== 汇总（{len(results)} 只） =====')
    won = [r for r in results if r['return'] > 0]
    lost = [r for r in results if r['return'] <= 0]
    avg_ret = sum(r['return'] for r in results) / len(results) if results else 0
    total_trades = sum(r['trades'] for r in results)
    avg_win = (sum(r['win_rate'] for r in results if r['trades'] > 0) /
               len([r for r in results if r['trades'] > 0])) if results else 0
    print(f'盈利: {len(won)} 只 | 亏损: {len(lost)} 只')
    print(f'平均收益: {avg_ret:.2f}%')
    print(f'总交易笔数: {total_trades}')
    print(f'平均胜率: {avg_win:.1f}%')

    # 排序
    if args.sort == 'win_rate':
        results.sort(key=lambda x: x['win_rate'], reverse=True)
    elif args.sort == 'trades':
        results.sort(key=lambda x: x['trades'], reverse=True)
    else:
        results.sort(key=lambda x: x['return'], reverse=True)

    display = [r for r in results if r['return'] > 0] if args.profit_only else results

    print(f'\n--- {"盈利Top" if args.profit_only else "收益排序Top"}50 ---')
    for r in display[:50]:
        print(f'  {r["code"]}: {r["return"]:+.2f}%  {r["trades"]}笔  胜率{r["win_rate"]:.0f}%')

    if not args.profit_only:
        print(f'\n--- 亏损Top20 ---')
        for r in results[-20:]:
            print(f'  {r["code"]}: {r["return"]:+.2f}%  {r["trades"]}笔  胜率{r["win_rate"]:.0f}%')


if __name__ == '__main__':
    main()
