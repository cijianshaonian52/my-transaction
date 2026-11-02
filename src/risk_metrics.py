# -*- coding: utf-8 -*-
r"""风险指标工具：年化波动率 / 夏普比率 / Sortino / 最大回撤 / 卡玛比率

用法:
    # 单票（详细指标 + 分年拆解）
    .venv\Scripts\python.exe risk_metrics.py 600206.SH
    .venv\Scripts\python.exe risk_metrics.py 600206.SH --timing

    # 多票/全市场表格（不带代码默认扫 stockdb_csv 前复权全市场，末尾给等权组合指标）
    .venv\Scripts\python.exe risk_metrics.py 000017.SZ 002122.SZ
    .venv\Scripts\python.exe risk_metrics.py --strategy strategy_normal.py
    .venv\Scripts\python.exe risk_metrics.py --cache   # 仅不复权对账口径用

    # 自定义无风险利率（默认1.5%年化，全脚本唯一口径来源）、窗口、排序
    .venv\Scripts\python.exe risk_metrics.py 600206.SH --rf 0.02
    .venv\Scripts\python.exe risk_metrics.py --start 2025-01-01 --end 2026-09-10 --sort sharpe

口径声明（收益与波动严格同源，避免口径漂移）:
    日收益 r_t = NAV_t / NAV_{t-1} - 1，NAV=每个交易日收盘账户总市值（持仓按收盘价盯市）
    年化交易日 252；rf_daily = 年化无风险利率 / 252（单利）
    年化收益 = (NAV末/NAV初)^(252/有效日收益数) - 1
    年化波动 = std(r, ddof=1) × √252
    夏普 Sharpe  = (mean(r) - rf_daily) / std(r, ddof=1) × √252
    Sortino = (mean(r) - rf_daily) / 下行偏差 × √252
              下行偏差 = sqrt(mean(min(0, r-rf_daily)^2)) × √252，全体交易日做分母（目标半方差）
    最大回撤 = min(NAV/cummax - 1)；卡玛 Calmar = 年化收益 / |最大回撤|
    注: 全程空仓（日收益恒为0）时 std=0，夏普/Sortino 记为 N/A，不输出浮点灰尘伪值
"""
import os
import sys
import glob
import argparse
import datetime

import numpy as np
import pandas as pd
import backtrader as bt

TRADING_DAYS = 252
DEFAULT_RF = 0.015
_EPS = 1e-12  # 年化/日波动低于此值视为无波动，拦截常数序列的浮点灰尘


# ── 每日净值记录器：在每根K线收盘记录账户总市值（持仓按当日收盘价盯市）──
class NAVRecorder(bt.Analyzer):
    def start(self):
        self.dates = []
        self.nav = []

    def next(self):
        self.dates.append(bt.num2date(self.strategy.data.datetime[0]).date())
        self.nav.append(self.strategy.broker.getvalue())


def calc_metrics(nav, rf_annual=DEFAULT_RF, py=TRADING_DAYS):
    """统一口径风险指标。nav: pd.Series(index=date, values=账户总市值) 或单位净值"""
    nav = pd.Series(nav).dropna().astype(float)
    rets = nav.pct_change().dropna()
    n = len(rets)
    out = {'n_days': n + 1, 'total_return': np.nan, 'cagr': np.nan,
           'ann_vol': np.nan, 'downside_vol': np.nan, 'sharpe': np.nan,
           'sortino': np.nan, 'max_dd': np.nan, 'calmar': np.nan,
           'exposure': np.nan, 'active_day_win': np.nan}
    if n < 2:
        return out
    rf_d = rf_annual / py
    # 注意：必须直接用 rets 的均值/标准差，不要先构造 excess 常数序列，
    # 否则空仓段（r恒为0）减样本均值会残留 1e-20 级浮点灰尘，夏普被放大成天文数字
    std_r = rets.std(ddof=1)
    mean_excess = rets.mean() - rf_d
    # 下行偏差用日频量纲，年化在输出/算比率时统一乘 √252；
    # 切勿先年化分母再配 √252 分子（会多除一次√252，Sortino 被压缩约16倍）
    downside_d = np.sqrt((np.minimum(rets - rf_d, 0.0) ** 2).mean())
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (py / n) - 1.0
    dd = nav / nav.cummax() - 1.0
    mdd = dd.min()
    active = rets[rets.abs() > 1e-9]  # 持仓日（账户市值实际变化）
    out.update(
        total_return=nav.iloc[-1] / nav.iloc[0] - 1.0,
        cagr=cagr,
        ann_vol=std_r * np.sqrt(py),
        downside_vol=downside_d * np.sqrt(py),
        # 门控统一用收益自身波动：全程空仓（r恒0）时相对rf的下行偏差=rf_d，
        # 会得到无意义的 -1，故 std=0 时夏普/Sortino 一律 N/A
        sharpe=(mean_excess / std_r * np.sqrt(py)) if std_r > _EPS else np.nan,
        sortino=(mean_excess / downside_d * np.sqrt(py)) if std_r > _EPS and downside_d > _EPS else np.nan,
        max_dd=mdd,
        calmar=cagr / abs(mdd) if mdd < -_EPS else np.nan,
        exposure=len(active) / n,
        active_day_win=(active > 0).mean() if len(active) else np.nan,
    )
    return out


def fmt(v, pct=True, nd=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 'N/A'
    return f'{v*100:+.{nd}f}%' if pct else f'{v:+.{nd}f}'


def print_metrics_block(m, indent=''):
    print(f'{indent}有效交易日: {m["n_days"]} 天（持仓日占比 {fmt(m["exposure"])}，'
          f'持仓日胜率 {fmt(m["active_day_win"])}）')
    print(f'{indent}总收益率: {fmt(m["total_return"])}   '
          f'年化收益(CAGR): {fmt(m["cagr"])}')
    print(f'{indent}年化波动率: {fmt(m["ann_vol"])}   '
          f'下行波动率: {fmt(m["downside_vol"])}')
    print(f'{indent}夏普比率: {fmt(m["sharpe"], pct=False, nd=3)}   '
          f'Sortino: {fmt(m["sortino"], pct=False, nd=3)}')
    print(f'{indent}最大回撤: {fmt(m["max_dd"])}   '
          f'卡玛比率: {fmt(m["calmar"], pct=False, nd=3)}')


def main():
    parser = argparse.ArgumentParser(description='回测风险指标（夏普/Sortino/波动率/回撤）')
    parser.add_argument('codes', nargs='*', help='股票代码（空则测 stockdb_csv 前复权全市场）')
    parser.add_argument('--strategy', default='main.py', help='策略文件（默认 main.py）')
    parser.add_argument('--timing', action='store_true', help='开启大盘择时（默认关闭）')
    parser.add_argument('--no-timing', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--cache', action='store_true',
                        help='扫描 cache_data 不复权目录（默认扫 stockdb_csv 前复权，仅对账用）')
    parser.add_argument('--start', default=None,
                        help='回测起始日 YYYY-MM-DD（默认用策略文件值；指定时预取60自然日预热）')
    parser.add_argument('--end', default=None, help='回测结束日 YYYY-MM-DD（排他上界）')
    parser.add_argument('--cash', type=float, default=None, help='初始资金（默认用策略文件值）')
    parser.add_argument('--rf', type=float, default=DEFAULT_RF,
                        help=f'年化无风险利率（默认 {DEFAULT_RF} 即1.5%%），唯一利率来源')
    parser.add_argument('--sort', default='sharpe', choices=['return', 'sharpe', 'sortino'],
                        help='多票表格排序方式（默认 sharpe）')
    args = parser.parse_args()

    if args.no_timing:
        os.environ['MARKET_TIMING'] = 'off'
    if args.timing:
        os.environ['MARKET_TIMING'] = '000300.SH:20:exit'

    # ── 加载策略文件中的策略和工具类（同 batch_test 惯例；在引擎创建前截断，两个策略文件均适用）──
    _src_dir = os.path.dirname(os.path.abspath(__file__))
    strat_file = args.strategy
    if not os.path.exists(strat_file):    # 只给文件名时（默认 main.py）到 src/ 同目录找
        _cand = os.path.join(_src_dir, strat_file)
        if os.path.exists(_cand):
            strat_file = _cand
    sys.argv = [strat_file, '000017.SZ']
    cns = {'__file__': os.path.abspath(strat_file), 'sys': sys}
    src = open(strat_file, encoding='utf-8').read()
    if 'cerebro = bt.Cerebro' in src:
        src = src.split('cerebro = bt.Cerebro')[0]
    exec(src, cns)

    init_cash = args.cash if args.cash else cns['INIT_CASH']
    start_date = pd.Timestamp(args.start).to_pydatetime() if args.start else cns['START_DATE']
    end_date_excl = pd.Timestamp(args.end).to_pydatetime() if args.end else cns['END_DATE_EXCL']
    feed_start = (start_date - datetime.timedelta(days=60)) if args.start else start_date
    strategy_class = cns.get('PullbackStrategy', cns.get('MyStrategy', None))
    if strategy_class is None:
        for v in cns.values():
            if isinstance(v, type) and issubclass(v, bt.Strategy):
                strategy_class = v
                break
    if strategy_class is None:
        print('ERROR: 未找到策略类（bt.Strategy 子类）')
        sys.exit(1)

    # scan_signals 顶层会解析命令行参数，导入时屏蔽本脚本参数
    saved_argv = sys.argv
    sys.argv = ['x']
    import scan_signals as ss
    import market_timing as mt
    sys.argv = saved_argv

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（src 上一级）
    cache_dir = os.path.join(base_dir, 'cache_data')
    stockdb_dir = os.path.join(base_dir, 'stockdb_csv')

    def run_one(code, csv_path, source):
        """跑一只票，返回 (净值Series, 完整交易笔数) 或 None（失败）"""
        try:
            if source == 'local':
                df, _ = ss.load_local_csv(csv_path)
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
            cerebro.broker.setcash(init_cash)
            cerebro.broker.set_coc(True)
            cerebro.broker.set_coo(True)
            cerebro.broker.set_checksubmit(False)
            cerebro.broker.addcommissioninfo(cns['AStockCommission']())
            cerebro.addstrategy(strategy_class)
            cerebro.addsizer(cns['FullLotSizer'])
            cerebro.addanalyzer(NAVRecorder, _name='nav')
            data = bt.feeds.PandasData(dataname=df, fromdate=feed_start, todate=end_date_excl)
            cerebro.adddata(data)
            _strat = cerebro.run()[0]
            rec = _strat.analyzers.nav
            nav = pd.Series(rec.nav, index=pd.to_datetime(rec.dates), name=code)
            n_trades = len(getattr(_strat, 'fills', [])) // 2
            return nav, n_trades
        except Exception as e:
            print(f'  {code} 回测失败: {str(e)[:60]}')
            return None

    # ── 确定股票列表（同 batch_test 逻辑，跳过 _ 开头的辅助文件）──
    csv_files = []
    if args.codes:
        missing = []
        for c in args.codes:
            cache_path = os.path.join(cache_dir, f'{c}.csv')
            if os.path.exists(cache_path):
                csv_files.append((c, cache_path, 'cache'))
                continue
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
        csv_files = [(os.path.basename(f)[:-4], f, 'cache')
                     for f in sorted(glob.glob(os.path.join(cache_dir, '*.csv')))
                     if not os.path.basename(f).startswith('_')]
    else:
        # 默认 stockdb_csv 前复权全市场
        for f in sorted(glob.glob(os.path.join(stockdb_dir, '*.csv'))):
            stem = os.path.basename(f)[:-4]
            if '#' in stem:
                exch, num = stem.split('#')
                csv_files.append((f'{num}.{exch}', f, 'local'))

    print(f'策略: {strat_file}（{strategy_class.__name__}）  股票数: {len(csv_files)}  '
          f'初始资金: {init_cash:,.0f}')
    print(f'窗口: {start_date.date()} ~ {end_date_excl.date()}'
          + (f'（feed 预取自 {feed_start.date()} 预热）' if args.start else ''))
    print(f'大盘择时: {mt.describe()}'
          + ('（--timing 已开启）' if args.timing
             else '（环境变量 MARKET_TIMING 已开启）' if mt.is_enabled()
             else '（默认关闭）'))
    print(f'无风险利率: {args.rf*100:.2f}%/年（日化 {args.rf/TRADING_DAYS*100:.4f}%）  '
          f'年化基准: {TRADING_DAYS} 交易日  标准差: 样本(ddof=1)，Sortino下行偏差 ddof=0')
    print()

    # ── 逐只回测 ──
    nav_map, rows = {}, []
    for code, csv_path, source in csv_files:
        res = run_one(code, csv_path, source)
        if res is None:
            continue
        nav, n_trades = res
        nav_map[code] = nav
        rows.append((code, n_trades, calc_metrics(nav, rf_annual=args.rf)))

    if not rows:
        print('没有可计算的股票。')
        sys.exit(1)

    # ── 单票：详细指标 + 分年拆解 ──
    if len(rows) == 1:
        code, n_trades, m = rows[0]
        print(f'===== {code} 风险指标（{n_trades} 笔完整交易）=====')
        print_metrics_block(m)

        rets = nav_map[code].pct_change().dropna()
        print(f'\n--- 分年拆解（夏普/Sortino 用同年 {args.rf*100:.1f}% 利率口径）---')
        print(f'{"年份":>6s}  {"交易日":>5s} {"持仓占比":>8s} {"收益":>9s} {"年化波动":>9s} '
              f'{"夏普":>7s} {"Sortino":>8s} {"最大回撤":>9s}')
        for yr, grp in rets.groupby(rets.index.year):
            if len(grp) < 2:
                continue
            yr_nav = (1 + grp).cumprod()  # 年内单位净值（年初=1），pct_change 后即 grp
            mm = calc_metrics(yr_nav, rf_annual=args.rf)
            print(f'{yr:>6d}  {len(grp):>5d} {fmt(mm["exposure"]):>8s} '
                  f'{fmt(mm["total_return"]):>9s} {fmt(mm["ann_vol"]):>9s} '
                  f'{fmt(mm["sharpe"], pct=False, nd=2):>7s} '
                  f'{fmt(mm["sortino"], pct=False, nd=2):>8s} {fmt(mm["max_dd"]):>9s}')
        print('\n注: 首个自然年若不足完整年度，年化收益按当年实际交易日折算，与全年不可直接比较；'
              '无持仓年份夏普/Sortino 为 N/A。')
        return

    # ── 多票：排序表 + 个股分布 + 等权组合 ──
    def _key(r):
        name = {'return': 'total_return', 'sharpe': 'sharpe', 'sortino': 'sortino'}[args.sort]
        v = r[2][name]
        return v if not np.isnan(v) else -99.0

    rows.sort(key=_key, reverse=True)
    print(f'{"股票":>12s}  {"笔数":>4s} {"持仓%":>6s} {"总收益":>9s} {"年化":>8s} '
          f'{"年波动":>8s} {"夏普":>7s} {"Sortino":>8s} {"最大回撤":>9s} {"卡玛":>6s}')
    print('=' * 96)
    for code, n_trades, m in rows:
        print(f'{code:>12s}  {n_trades:>4d} {fmt(m["exposure"], nd=0):>6s} '
              f'{fmt(m["total_return"]):>9s} {fmt(m["cagr"]):>8s} {fmt(m["ann_vol"]):>8s} '
              f'{fmt(m["sharpe"], pct=False, nd=2):>7s} '
              f'{fmt(m["sortino"], pct=False, nd=2):>8s} '
              f'{fmt(m["max_dd"]):>9s} {fmt(m["calmar"], pct=False, nd=2):>6s}')

    sharpes = [r[2]['sharpe'] for r in rows if not np.isnan(r[2]['sharpe'])]
    sortinos = [r[2]['sortino'] for r in rows if not np.isnan(r[2]['sortino'])]
    vols = [r[2]['ann_vol'] for r in rows if not np.isnan(r[2]['ann_vol'])]
    mdds = [r[2]['max_dd'] for r in rows if not np.isnan(r[2]['max_dd'])]
    print(f'\n--- 个股分布（{len(rows)} 只，非组合口径）---')
    print(f'年化波动: 均值 {np.mean(vols)*100:.1f}%  中位 {np.median(vols)*100:.1f}%')
    print(f'夏普: 均值 {np.mean(sharpes):+.3f}  中位 {np.median(sharpes):+.3f}  '
          f'正比例 {(np.array(sharpes) > 0).mean()*100:.0f}%')
    print(f'Sortino: 均值 {np.mean(sortinos):+.3f}  中位 {np.median(sortinos):+.3f}')
    print(f'最大回撤: 均值 {np.mean(mdds)*100:.1f}%  中位 {np.median(mdds)*100:.1f}%')

    # 等权组合：个股日收益按交易日对齐（停牌日净值前向填充），横截面等权平均
    all_dates = sorted(set().union(*[set(s.index) for s in nav_map.values()]))
    nav_df = pd.DataFrame({c: s for c, s in nav_map.items()}).reindex(all_dates).ffill()
    rets_df = nav_df.pct_change().fillna(0.0)
    port_rets = rets_df.mean(axis=1)  # 每日等权（当日无数据票不参与）
    port_nav = (1 + port_rets).cumprod() * init_cash
    pm = calc_metrics(port_nav, rf_annual=args.rf)
    print(f'\n--- 等权组合（{len(nav_map)} 只，日收益等权再累计）---')
    print_metrics_block(pm, indent='  ')
    print('  说明: 组合波动/夏普远优于个股均值属正常——分散化消除个股特异波动'
          '（参见 risk-rule-backtest 基准：个股均回撤约-28%，全分散组合约-9%）。')


if __name__ == '__main__':
    main()
