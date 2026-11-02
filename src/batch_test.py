# -*- coding: utf-8 -*-
r"""批量回测工具 - 默认测试 stockdb_csv 前复权全市场

用法:
    # 用 main.py 策略测试所有股票（默认 stockdb_csv 前复权）
    .venv\Scripts\python.exe batch_test.py

    # 用 strategy_normal.py 策略测试所有股票
    .venv\Scripts\python.exe batch_test.py --strategy strategy_normal.py

    # 测试指定股票
    .venv\Scripts\python.exe batch_test.py 000017.SZ 002122.SZ

    # 用指定策略测试指定股票并生成图表
    .venv\Scripts\python.exe batch_test.py --strategy strategy_normal.py --chart 600206.SH

    # 按收益排序（默认）或按胜率排序
    .venv\Scripts\python.exe batch_test.py --sort win_rate

    # 只看盈利的股票
    .venv\Scripts\python.exe batch_test.py --profit-only

    # 指定初始资金
    .venv\Scripts\python.exe batch_test.py --cash 500000

    # 扫描 cache_data 不复权目录（默认扫 stockdb_csv，仅对账用）
    .venv\Scripts\python.exe batch_test.py --cache

    # 开启大盘择时（默认关闭；HS300 MA20 exit + 涨停顺延）
    .venv\Scripts\python.exe batch_test.py --timing

    # 自定义回测窗口（按 scan 口径预取60天预热，预热期同样参与交易）
    .venv\Scripts\python.exe batch_test.py --strategy strategy_normal.py --timing --start 2026-01-01 --end 2026-12-31
"""
import os, sys, glob, argparse, datetime, pandas as pd, backtrader as bt

# ── 参数解析 ──
parser = argparse.ArgumentParser(description='批量回测工具')
parser.add_argument('codes', nargs='*', help='股票代码（空则测全部）')
parser.add_argument('--strategy', default='main.py', help='策略文件（默认 main.py）')
parser.add_argument('--chart', action='store_true', help='为每只股票生成图表')
parser.add_argument('--sort', default='return', choices=['return', 'win_rate', 'trades'], help='排序方式')
parser.add_argument('--profit-only', action='store_true', help='只显示盈利股票')
parser.add_argument('--cash', type=float, default=None, help='初始资金（默认用策略文件中的值）')
parser.add_argument('--timing', action='store_true', help='开启大盘择时（HS300 MA20 exit + 涨停顺延；默认关闭）')
parser.add_argument('--no-timing', action='store_true', help=argparse.SUPPRESS)  # 旧参数兼容（显式关闭）
parser.add_argument('--cache', action='store_true', help='扫描 cache_data 不复权目录（默认扫 stockdb_csv 前复权，仅对账用）')
parser.add_argument('--start', default=None, help='回测起始日 YYYY-MM-DD（默认用策略文件值；按scan口径预取60天）')
parser.add_argument('--end', default=None, help='回测结束日 YYYY-MM-DD（排他上界，默认用策略文件值）')
args = parser.parse_args()

# 必须在 exec 策略文件（其 import market_timing）之前设置；显式参数优先于环境变量
if args.no_timing:
    os.environ['MARKET_TIMING'] = 'off'
if args.timing:
    os.environ['MARKET_TIMING'] = '000300.SH:20:exit'

# ── 加载策略文件中的策略和工具类 ──
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SRC_DIR)   # 数据目录在 src 上一级（项目根）
strat_file = args.strategy
if not os.path.exists(strat_file):        # 只给文件名时（默认 main.py）到 src/ 同目录找
    _cand = os.path.join(SRC_DIR, strat_file)
    if os.path.exists(_cand):
        strat_file = _cand
sys.argv = [strat_file, '000017.SZ']
ns = {'__file__': os.path.abspath(strat_file), 'sys': sys}
src = open(strat_file, encoding='utf-8').read()
# 截取引擎创建之前的代码（策略定义部分）
for marker in ['# 创建 cerebro', '# ====.*引擎', '# 引擎']:
    if marker in src:
        src = src.split(marker)[0]
        break
exec(src, ns)

INIT_CASH = args.cash if args.cash else ns['INIT_CASH']
AStockCommission = ns['AStockCommission']
FullLotSizer = ns['FullLotSizer']
START_DATE = pd.Timestamp(args.start).to_pydatetime() if args.start else ns['START_DATE']
END_DATE_EXCL = pd.Timestamp(args.end).to_pydatetime() if args.end else ns['END_DATE_EXCL']
# 指定 --start 时按 scan_signals 口径预取60自然日预热（预热段同样参与交易，
# 与 numpy 引擎 YEAR_START-60d 一致）；默认行为不变（feed 从 START_DATE 起）
FEED_START = (START_DATE - datetime.timedelta(days=60)) if args.start else START_DATE
# 自动检测策略类名（main.py 用 PullbackStrategy，strategy_normal.py 用 MyStrategy）
StrategyClass = ns.get('PullbackStrategy', ns.get('MyStrategy', None))
if StrategyClass is None:
    # 尝试遍历找到 bt.Strategy 子类
    for v in ns.values():
        if isinstance(v, type) and issubclass(v, bt.Strategy):
            StrategyClass = v
            break
if StrategyClass is None:
    print('ERROR: 未找到策略类（bt.Strategy 子类）')
    sys.exit(1)

print(f'策略文件: {strat_file}  策略类: {StrategyClass.__name__}')
print(f'初始资金: {INIT_CASH:,.0f}元  股票数: (扫描中...)  生成图表: {"是" if args.chart else "否"}')
print(f'回测窗口: {START_DATE.date()} ~ {END_DATE_EXCL.date()}'
      + (f'（feed 预取自 {FEED_START.date()} 预热）' if args.start else ''))

# ── 确定要测试的股票列表 ──
# scan_signals 模块顶层会解析命令行参数，导入时需屏蔽本脚本的参数
_saved_argv = sys.argv
sys.argv = ['x']
import scan_signals as ss
import market_timing as mt
sys.argv = _saved_argv
print(f'大盘择时: {mt.describe()}'
      + ('（--timing 已开启）' if args.timing
         else '（环境变量 MARKET_TIMING 已开启）' if mt.is_enabled()
         else '（--no-timing 已关闭）' if args.no_timing
         else '（默认关闭，--timing 可开启）'))

base_dir = PROJECT_ROOT
cache_dir = os.path.join(base_dir, 'cache_data')
stockdb_dir = os.path.join(base_dir, 'stockdb_csv')

# (显示代码, 文件路径, 数据源)  数据源: 'cache'=tushare格式 / 'local'=GBK混合分隔
csv_files = []
if args.codes:
    missing = []
    for c in args.codes:
        cache_path = os.path.join(cache_dir, f'{c}.csv')
        if os.path.exists(cache_path):
            csv_files.append((c, cache_path, 'cache'))
            continue
        # 回退到 stockdb_csv 目录：600487.SH → SH#600487.csv，000001.SZ → SZ#000001.csv
        if '.' in c:
            num, exch = c.split('.')
            local_path = os.path.join(stockdb_dir, f'{exch}#{num}.csv')
            if os.path.exists(local_path):
                csv_files.append((c, local_path, 'local'))
                continue
        missing.append(c)
    if missing:
        print(f'WARNING: 以下股票在 cache_data / stockdb_csv 中均未找到: {", ".join(missing)}')
elif args.cache:
    # 不复权对账口径（辅助文件 _ 开头跳过）
    csv_files = [(os.path.basename(f)[:-4], f, 'cache')
                 for f in sorted(glob.glob(os.path.join(cache_dir, '*.csv')))
                 if not os.path.basename(f).startswith('_')]
else:
    # 默认全量扫描 stockdb_csv 前复权：SH#600487.csv → 600487.SH
    csv_files = []
    for f in sorted(glob.glob(os.path.join(stockdb_dir, '*.csv'))):
        stem = os.path.basename(f)[:-4]
        if '#' not in stem:
            continue
        exch, num = stem.split('#')
        csv_files.append((f'{num}.{exch}', f, 'local'))

print(f'初始资金: {INIT_CASH:,.0f}元  股票数: {len(csv_files)}  生成图表: {"是" if args.chart else "否"}')
print()

# ── 逐只回测 ──
results = []
for code, csv_path, source in csv_files:
    try:
        if source == 'local':
            df, _ = ss.load_local_csv(csv_path)  # GBK 混合分隔格式
            if df is None:
                raise ValueError('stockdb_csv 文件解析失败')
        else:
            df = pd.read_csv(csv_path)
            date_col = 'trade_date' if 'trade_date' in df.columns else 'date'
            df[date_col] = pd.to_datetime(df[date_col].astype(str))
            df = df.sort_values(date_col).set_index(date_col)
            if 'vol' in df.columns:
                df = df.rename(columns={'vol': 'volume'})

        cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
        cerebro.broker.setcash(INIT_CASH)
        cerebro.broker.set_coc(True)
        cerebro.broker.set_coo(True)
        cerebro.broker.set_checksubmit(False)
        cerebro.broker.addcommissioninfo(AStockCommission())
        cerebro.addstrategy(StrategyClass)
        cerebro.addsizer(FullLotSizer)
        data = bt.feeds.PandasData(dataname=df, fromdate=FEED_START, todate=END_DATE_EXCL)
        cerebro.adddata(data)
        run_results = cerebro.run()
        strat = run_results[0]

        final = cerebro.broker.getvalue()
        ret = (final / INIT_CASH - 1) * 100
        years = (END_DATE_EXCL - START_DATE).days / 365.25
        annual = ((final / INIT_CASH) ** (1 / years) - 1) * 100 if final > 0 else -100

        # 用 fills 统计盈亏
        fills = strat.fills if hasattr(strat, 'fills') else []
        n = len(fills) // 2
        w, l = 0, 0
        open_buy = None
        for side, fdate, price, size, signal in fills:
            if side == 'buy':
                open_buy = (price, abs(size))
            elif open_buy is not None:
                bprice, qty = open_buy
                pnl = (price - bprice) * qty
                if pnl >= 0:
                    w += 1
                else:
                    l += 1
                open_buy = None
        pos = cerebro.broker.getposition(data).size
        results.append((code, n, w, l, ret, annual, pos))

        # 生成图表
        if args.chart:
            print(f'  生成图表: {code} ...', end='\r')
            import subprocess
            subprocess.run([sys.executable, strat_file, code], capture_output=True,
                           cwd=PROJECT_ROOT)

    except Exception as e:
        results.append((code, -1, 0, 0, 0, 0, str(e)[:50]))

# ── 排序 ──
if args.sort == 'return':
    results.sort(key=lambda x: x[4], reverse=True)
elif args.sort == 'win_rate':
    results.sort(key=lambda x: (x[2] / x[1] if x[1] > 0 else 0), reverse=True)
elif args.sort == 'trades':
    results.sort(key=lambda x: x[1], reverse=True)

# ── 过滤 ──
if args.profit_only:
    results = [r for r in results if r[4] > 0]

# ── 输出 ──
print(f'{"=" * 90}')
print(f'{"股票":>12s}  {"笔数":>4s} {"盈":>3s} {"亏":>3s} {"胜率":>6s} {"总收益":>9s} {"年化":>8s}  {"持仓":>8s}')
print(f'{"=" * 90}')
for code, n, w, l, ret, annual, pos in results:
    if n < 0:
        print(f'{code:>12s}  ERROR: {pos}')
        continue
    wr = w / n * 100 if n > 0 else 0
    pos_str = str(pos) if pos != 0 else '0'
    print(f'{code:>12s}  {n:4d} {w:3d} {l:3d} {wr:5.1f}% {ret:+8.2f}% {annual:+7.2f}%  {pos_str:>8s}')

# ── 汇总 ──
valid = [r for r in results if r[1] >= 0]
profitable = [r for r in valid if r[4] > 0]
losing = [r for r in valid if r[4] < 0]
print(f'\n--- 汇总 ---')
print(f'策略: {strat_file}  总股票数: {len(results)}')
print(f'盈利: {len(profitable)}  亏损: {len(losing)}')
if profitable:
    print(f'盈利股票平均收益: {sum(r[4] for r in profitable)/len(profitable):+.2f}%')
if losing:
    print(f'亏损股票平均收益: {sum(r[4] for r in losing)/len(losing):+.2f}%')
if valid:
    avg = sum(r[4] for r in valid) / len(valid)
    print(f'全部股票平均收益: {avg:+.2f}%')
    total_w = sum(r[2] for r in valid)
    total_l = sum(r[3] for r in valid)
    print(f'总交易笔数: {sum(r[1] for r in valid)}  总盈: {total_w}  总亏: {total_l}  总胜率: {total_w/(total_w+total_l)*100:.1f}%')
