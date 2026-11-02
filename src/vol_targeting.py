# -*- coding: utf-8 -*-
"""Vol Targeting 网格验证：对全市场 stockdb_csv 跑多种仓位方案，输出对照表。

方案：
  - 基线满仓（vol_target=None）
  - 固定半仓（fixed_scale=0.5）
  - vol_target = 20 / 25 / 30 / 35%

输出：output/vol_targeting_grid.csv + 终端汇总表
"""
import os, sys, glob, argparse, multiprocessing as mp
import numpy as np, pandas as pd

# ── 路径 ──
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_SRC_DIR)
sys.path.insert(0, _SRC_DIR)
# scan_signals 在 import 时会执行 argparse，先保存原始 argv 再清空让它用默认值
_orig_argv = sys.argv[:]
sys.argv = [sys.argv[0]]
os.chdir(PROJECT_ROOT)

import scan_signals as ss

# ── 5 年回测区间 ──
BT_START = pd.Timestamp('2021-08-01')
BT_END   = pd.Timestamp('2026-09-17')

SCHEMES = [
    ('基线满仓',    dict()),
    ('固定半仓',    dict(fixed_scale=0.5)),
    ('VT_20%',     dict(vol_target=20)),
    ('VT_25%',     dict(vol_target=25)),
    ('VT_30%',     dict(vol_target=30)),
    ('VT_35%',     dict(vol_target=35)),
]


def run_one(args_tuple):
    """单票 × 全方案：返回 {scheme_name: (ret, max_dd, sharpe, win_rate, n_trades, avg_pos)}"""
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

        out = {'code': ts_code, 'name': name}
        for label, kw in SCHEMES:
            ret, wr, n, sharpe = ss.run_backtest(
                df, code=ts_code, stock_name=name,
                start_date=BT_START, end_date=BT_END,
                **kw)
            out[f'{label}_ret'] = ret
            out[f'{label}_wr'] = wr
            out[f'{label}_n'] = n
            out[f'{label}_sharpe'] = sharpe if sharpe is not None else 0
        return out
    except Exception:
        return None


def max_drawdown(nav_series):
    """从日净值序列算最大回撤%"""
    arr = np.asarray(nav_series, dtype=float)
    if len(arr) < 2:
        return 0.0
    peak = np.maximum.accumulate(arr)
    dd = (arr / peak - 1) * 100
    return round(float(dd.min()), 2)


def main():
    sys.argv = _orig_argv  # 恢复原始命令行参数
    parser = argparse.ArgumentParser(description='Vol Targeting 网格验证（全市场 5 年）')
    parser.add_argument('--limit', type=int, default=0, help='只跑前N只票（0=全部，调试用）')
    args = parser.parse_args()

    files = sorted(glob.glob(os.path.join(PROJECT_ROOT, 'stockdb_csv', '*.csv')))
    if args.limit > 0:
        files = files[:args.limit]
    print(f'Vol Targeting 网格验证：{len(files)} 只股票 × {len(SCHEMES)} 方案')
    print(f'回测区间: {BT_START.date()} ~ {BT_END.date()}')
    print()

    n_procs = min(mp.cpu_count(), 8)
    print(f'使用 {n_procs} 进程并行处理...')
    with mp.Pool(n_procs) as pool:
        results = pool.map(run_one, [(f,) for f in files], chunksize=20)

    results = [r for r in results if r is not None]
    print(f'\n有效结果: {len(results)}/{len(files)}')

    # ── 汇总表 ──
    print(f'\n{"方案":<12} {"平均收益%":>10} {"中位收益%":>10} {"正收益占比":>10} {"平均夏普":>10} {"平均胜率%":>10}')
    print('-' * 72)
    for label, _ in SCHEMES:
        rets = np.array([r[f'{label}_ret'] for r in results])
        sharpes = np.array([r[f'{label}_sharpe'] for r in results])
        wrs = np.array([r[f'{label}_wr'] for r in results])
        pos_pct = np.mean(rets > 0) * 100 if len(rets) > 0 else 0
        print(f'{label:<12} {rets.mean():>10.2f} {np.median(rets):>10.2f} '
              f'{pos_pct:>9.1f}% {sharpes.mean():>10.2f} {wrs.mean():>10.1f}')

    # ── 写 CSV ──
    out_dir = os.path.join(PROJECT_ROOT, 'output')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'vol_targeting_grid.csv')
    df_out = pd.DataFrame(results)
    df_out.to_csv(out_path, index=False, encoding='utf-8-sig')
    print(f'\n明细已写入: {out_path}')

    # ── 配对比较：VT_30% vs 基线 / vs 固定半仓 ──
    print(f'\n=== 配对比较（VT_30% vs 其他） ===')
    base = np.array([r['基线满仓_ret'] for r in results])
    half = np.array([r['固定半仓_ret'] for r in results])
    vt30 = np.array([r['VT_30%_ret'] for r in results])
    diff_base = vt30 - base
    diff_half = vt30 - half
    print(f'vs 基线满仓: 平均差 {diff_base.mean():+.2f}%  减仓票数 {np.sum(diff_base < -0.01)}  加仓票数 {np.sum(diff_base > 0.01)}')
    print(f'vs 固定半仓: 平均差 {diff_half.mean():+.2f}%  优于半仓 {np.sum(diff_half > 0.01)}  劣于半仓 {np.sum(diff_half < -0.01)}')


if __name__ == '__main__':
    main()
