# -*- coding: utf-8 -*-
"""收盘破MA5信号：次日阳/阴线后续收益对比。

信号定义（与 strategy_normal.py L261 完全一致）：
  Day0: (MA20>MA10 或 MA10>MA5) 且 close[-1]<=MA5[-1] 且 close[0]>MA5[0]
  → 以 Day0 收盘价买入

分组：
  阳线组: Day1 close > open
  阴线组: Day1 close <= open

测量: Day0收盘价 → Day1/3/5/10/20日后的收益
"""
import os, sys, glob, json, multiprocessing as mp
import numpy as np, pandas as pd

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_SRC_DIR)
sys.path.insert(0, _SRC_DIR)
sys.argv = [sys.argv[0]]
os.chdir(PROJECT_ROOT)

import scan_signals as ss

BT_START = pd.Timestamp('2021-08-01')
BT_END   = pd.Timestamp('2026-09-17')
FWD_DAYS = [1, 3, 5, 10, 20]


def analyze_one(args_tuple):
    f, = args_tuple
    try:
        result = ss.load_local_csv(f)
        if result[0] is None:
            return None
        df, name = result
        basename = os.path.basename(f).replace('.csv', '')
        if '#' in basename:
            parts = basename.split('#')
            ts_code = f'{parts[1]}.{parts[0]}'
        else:
            ts_code = basename

        close = df['close'].values.astype(float)
        opn   = df['open'].values.astype(float)
        ma5   = df['close'].rolling(5).mean().values
        ma10  = df['close'].rolling(10).mean().values
        ma20  = df['close'].rolling(20).mean().values
        dates = df.index
        n = len(close)

        # 收盘破MA5 信号日（与 strategy_normal.py L261 完全一致）
        # 条件: (MA20>MA10 或 MA10>MA5) 且 close[-1]<=MA5[-1] 且 close[0]>MA5[0]
        signal_indices = []
        for i in range(1, n):
            if np.isnan(ma20[i]) or np.isnan(ma10[i]) or np.isnan(ma5[i]):
                continue
            if np.isnan(ma5[i-1]):
                continue
            trend_ok = (ma20[i] > ma10[i]) or (ma10[i] > ma5[i])
            breakout  = (close[i-1] <= ma5[i-1]) and (close[i] > ma5[i])
            if trend_ok and breakout:
                signal_indices.append(i)

        if not signal_indices:
            return None

        # 分阳线/阴线组，记录前瞻收益
        bull_rets = []  # Day1 阳线
        bear_rets = []  # Day1 阴线

        for idx in signal_indices:
            d1 = idx + 1
            if d1 >= n or d1 + 20 >= n:
                continue
            is_bull = close[d1] > opn[d1]  # Day1 阳线

            rets = {}
            for d in FWD_DAYS:
                end = min(idx + d, n - 1)
                if close[end] > 0 and close[idx] > 0:
                    rets[d] = (close[end] / close[idx] - 1) * 100
                else:
                    rets[d] = None
            rets['date'] = dates[idx]
            rets['code'] = ts_code
            rets['d1_ret'] = (close[d1] / close[idx] - 1) * 100 if close[idx] > 0 else None

            if is_bull:
                bull_rets.append(rets)
            else:
                bear_rets.append(rets)

        return {
            'code': ts_code, 'name': name,
            'n_signals': len(signal_indices),
            'bull': bull_rets,
            'bear': bear_rets,
        }
    except Exception:
        return None


def print_stats(label, items):
    n = len(items)
    if n == 0:
        print(f'{label:<16} n=0')
        return
    print(f'{label:<16} n={n:>7}  ', end='')
    for d in FWD_DAYS:
        vals = [x[d] for x in items if x.get(d) is not None]
        if vals:
            print(f'{d}d:{np.mean(vals):+6.2f}% ', end='')
        else:
            print(f'{d}d:   -   ', end='')
    print()
    # 胜率
    print(f'{"":16} 胜率:        ', end='')
    for d in FWD_DAYS:
        vals = [x[d] for x in items if x.get(d) is not None]
        if vals:
            wr = sum(1 for v in vals if v > 0) / len(vals) * 100
            print(f'{d}d:{wr:5.1f}%  ', end='')
        else:
            print(f'{d}d:   -   ', end='')
    print()
    # 中位数
    print(f'{"":16} 中位:        ', end='')
    for d in FWD_DAYS:
        vals = [x[d] for x in items if x.get(d) is not None]
        if vals:
            print(f'{d}d:{np.median(vals):+6.2f}% ', end='')
        else:
            print(f'{d}d:   -   ', end='')
    print()


def main():
    files = sorted(glob.glob(os.path.join(PROJECT_ROOT, 'stockdb_csv', '*.csv')))
    print(f'收盘破MA5信号：次日阳/阴线收益对比')
    print(f'股票数: {len(files)}  区间: {BT_START.date()} ~ {BT_END.date()}')
    print()

    n_procs = min(mp.cpu_count(), 8)
    with mp.Pool(n_procs) as pool:
        results = pool.map(analyze_one, [(f,) for f in files], chunksize=20)

    results = [r for r in results if r is not None]
    print(f'有信号的股票: {len(results)}/{len(files)}')

    all_bull = []
    all_bear = []
    for r in results:
        all_bull.extend(r['bull'])
        all_bear.extend(r['bear'])

    total = len(all_bull) + len(all_bear)
    print(f'总信号数: {total}  阳线: {len(all_bull)} ({len(all_bull)/total*100:.1f}%)  阴线: {len(all_bear)} ({len(all_bear)/total*100:.1f}%)')
    print()

    print('=' * 90)
    print_stats('阳线组(Day1收>开)', all_bull)
    print('-' * 90)
    print_stats('阴线组(Day1收≤开)', all_bear)
    print('=' * 90)

    # 差值
    print('\n差值(阳-阴):')
    for d in FWD_DAYS:
        bv = [x[d] for x in all_bull if x.get(d) is not None]
        rv = [x[d] for x in all_bear if x.get(d) is not None]
        if bv and rv:
            diff = np.mean(bv) - np.mean(rv)
            print(f'  {d:2d}d: 阳线{np.mean(bv):+6.2f}% vs 阴线{np.mean(rv):+6.2f}%  差{diff:+6.2f}pct')

    # Day1 自身收益
    print('\nDay1自身收益:')
    bv = [x['d1_ret'] for x in all_bull if x.get('d1_ret') is not None]
    rv = [x['d1_ret'] for x in all_bear if x.get('d1_ret') is not None]
    if bv: print(f'  阳线组: {np.mean(bv):+6.2f}% (中位{np.median(bv):+6.2f}%)')
    if rv: print(f'  阴线组: {np.mean(rv):+6.2f}% (中位{np.median(rv):+6.2f}%)')

    # 阳线按 Day1 涨幅分桶
    print('\n=== 阳线组按 Day1 涨幅分桶 ===')
    buckets = [('< 1%', 0, 1), ('1~3%', 1, 3), ('3~5%', 3, 5), ('> 5%', 5, 999)]
    for blabel, lo, hi in buckets:
        items = [x for x in all_bull if x.get('d1_ret') is not None and lo <= x['d1_ret'] < hi]
        if not items:
            continue
        print(f'{blabel:>8} n={len(items):>6}  ', end='')
        for d in FWD_DAYS:
            vals = [x[d] for x in items if x.get(d) is not None]
            if vals:
                print(f'{d}d:{np.mean(vals):+6.2f}% ', end='')
            else:
                print(f'{d}d:   -   ', end='')
        print()
        # 胜率
        print(f'{"":>8} 胜率:     ', end='')
        for d in FWD_DAYS:
            vals = [x[d] for x in items if x.get(d) is not None]
            if vals:
                wr = sum(1 for v in vals if v > 0) / len(vals) * 100
                print(f'{d}d:{wr:5.1f}%  ', end='')
            else:
                print(f'{d}d:   -   ', end='')
        print()

    # 逐年
    print('\n=== 逐年 5日前瞻收益 ===')
    for label, items in [('阳线', all_bull), ('阴线', all_bear)]:
        df_t = pd.DataFrame(items)
        if df_t.empty:
            continue
        df_t['year'] = pd.to_datetime(df_t['date']).dt.year
        print(f'\n{label}组:')
        for yr in sorted(df_t['year'].unique()):
            sub = df_t[df_t['year'] == yr]
            vals = sub[5].dropna()
            if len(vals) > 0:
                print(f'  {yr}: n={len(vals):>5}  均值{vals.mean():+6.2f}%  胜率{(vals>0).sum()/len(vals)*100:.1f}%')

    # CSV
    out_path = os.path.join(PROJECT_ROOT, 'output', 'ma5_confirm_analysis.csv')
    rows = []
    for x in all_bull:
        row = {'code': x['code'], 'date': x['date'], 'group': '阳线', 'd1_ret': x.get('d1_ret')}
        row.update({f'fwd_{d}d': x.get(d) for d in FWD_DAYS})
        rows.append(row)
    for x in all_bear:
        row = {'code': x['code'], 'date': x['date'], 'group': '阴线', 'd1_ret': x.get('d1_ret')}
        row.update({f'fwd_{d}d': x.get(d) for d in FWD_DAYS})
        rows.append(row)
    pd.DataFrame(rows).to_csv(out_path, index=False, encoding='utf-8-sig')
    print(f'\n明细: {out_path} ({len(rows)} 行)')


if __name__ == '__main__':
    main()
