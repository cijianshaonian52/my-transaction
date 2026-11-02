# -*- coding: utf-8 -*-
"""首板次日溢价率分题材统计 (配合同花顺涨停复盘图使用)

口径:
  首板 = 当日涨停(主板9.8/双创19.8/ST4.8, 0.2pct容差) 且 前一日未涨停, 剔ST, 上市>5日
  开盘溢价% = 次日开盘价 / 首板收盘价 - 1
  收盘溢价% = 次日收盘价 / 首板收盘价 - 1
  最高溢价% = 次日最高价 / 首板收盘价 - 1  (盘中摸高, 反映打板客理论卖出上限)
  封板率   = 次日再次涨停比例
  行业 = cache_data/_stock_industry.csv (CSRC), 对应复盘图的"题材"分组
用法: python src/first_board_premium.py [--start 2026-09-22] [--end 2026-10-17]
输出: 控制台分组表 + output/first_board_premium_YYYYMMDD.csv 明细
"""
import sys, io, os, argparse, datetime
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKDB_DIR = os.path.join(ROOT, 'stockdb_csv')
IND_FILE = os.path.join(ROOT, 'cache_data', '_stock_industry.csv')


def limit_threshold(code6, name):
    if code6.startswith(('300', '301', '688', '689')):
        return 19.8
    if code6.startswith(('43', '83', '87', '920')):
        return 29.8
    if name and 'ST' in str(name).upper():
        return 4.8
    return 9.8


def tail_rows(fp, n=50):
    with open(fp, 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - n * 60))
        raw = f.read().decode('gbk', errors='ignore').splitlines()
    rows = []
    for ln in raw:
        p = ln.split(',')
        if len(p) in (7, 8) and p[0][:4].isdigit() and '-' in p[0]:
            rows.append(p)
    return rows[-n:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2026-09-22')
    ap.add_argument('--end', default='2026-10-17', help='首板日上限(需有次日数据)')
    args = ap.parse_args()
    s, e = args.start, args.end

    ind = pd.read_csv(IND_FILE, dtype=str)
    ind['code6'] = ind['ts_code'].str.split('.').str[0]
    name_map = ind.set_index('code6')['name'].to_dict()
    ind_map = ind.set_index('code6')['industry'].to_dict()

    recs = []
    for fn in sorted(os.listdir(STOCKDB_DIR)):
        if not fn.endswith('.csv'):
            continue
        code6 = fn.split('#')[1].split('.')[0]
        rows = tail_rows(os.path.join(STOCKDB_DIR, fn))
        if len(rows) < 5:
            continue
        d = [r[0] for r in rows]
        o = [float(r[1]) for r in rows]
        h = [float(r[2]) for r in rows]
        lo = [float(r[3]) for r in rows]
        c = [float(r[4]) for r in rows]
        a = [float(r[6]) for r in rows]
        name = name_map.get(code6, '')
        thr = limit_threshold(code6, name)
        for i in range(1, len(d) - 1):
            if not (s <= d[i] <= e):
                continue
            pct = (c[i] / c[i - 1] - 1) * 100
            if pct < thr:
                continue
            prev_pct = (c[i - 1] / c[i - 2] - 1) * 100 if i >= 2 else 0
            if prev_pct >= thr:                      # 前一日也涨停 → 非首板
                continue
            recs.append({
                '日期': d[i], '代码': code6, '名称': name,
                '行业': ind_map.get(code6, '未知') or '未知',
                '收盘价': c[i], '涨幅%': round(pct, 2),
                '成交额(亿)': round(a[i] / 1e8, 2),
                '一封到底': '是' if (lo[i] / c[i - 1] - 1) * 100 >= thr else '否',
                '开盘溢价%': round((o[i + 1] / c[i] - 1) * 100, 2),
                '收盘溢价%': round((c[i + 1] / c[i] - 1) * 100, 2),
                '最高溢价%': round((h[i + 1] / c[i] - 1) * 100, 2),
                '次日涨停': '是' if (c[i + 1] / c[i] - 1) * 100 >= thr else '否',
            })

    df = pd.DataFrame(recs)
    if df.empty:
        print('窗口内无首板样本'); return
    df = df.sort_values(['日期', '行业']).reset_index(drop=True)

    print(f'首板窗口 {s} ~ {e}, 样本 {len(df)} 只, 交易日 {df["日期"].nunique()} 天')
    print('\n══ 整体 ══')
    print(f'开盘溢价: 均值 {df["开盘溢价%"].mean():.2f}%  中位 {df["开盘溢价%"].median():.2f}%  '
          f'高开率 {(df["开盘溢价%"] > 0).mean() * 100:.1f}%')
    print(f'收盘溢价: 均值 {df["收盘溢价%"].mean():.2f}%  中位 {df["收盘溢价%"].median():.2f}%  '
          f'胜率 {(df["收盘溢价%"] > 0).mean() * 100:.1f}%')
    print(f'最高溢价: 均值 {df["最高溢价%"].mean():.2f}%  中位 {df["最高溢价%"].median():.2f}%')
    print(f'次日再涨停率: {(df["次日涨停"] == "是").mean() * 100:.1f}%')

    print('\n══ 分行业(题材, n≥3, 按开盘溢价均值排序) ══')
    g = df.groupby('行业').agg(
        n=('代码', 'count'),
        开盘均=('开盘溢价%', 'mean'), 开盘中=('开盘溢价%', 'median'),
        收盘均=('收盘溢价%', 'mean'), 收盘中=('收盘溢价%', 'median'),
        最高均=('最高溢价%', 'mean'),
        连板率=('次日涨停', lambda x: (x == '是').mean() * 100),
        高开率=('开盘溢价%', lambda x: (x > 0).mean() * 100),
    )
    g = g[g['n'] >= 3].sort_values('开盘均', ascending=False)
    print(f'{"行业":<28}{"n":>4}{"开盘均":>8}{"开盘中":>8}{"高开率":>8}'
          f'{"收盘均":>8}{"收盘中":>8}{"最高均":>8}{"连板率":>8}')
    for ix, r in g.iterrows():
        print(f'{ix:<28}{r["n"]:>4}{r["开盘均"]:>7.2f}%{r["开盘中"]:>7.2f}%{r["高开率"]:>7.0f}%'
              f'{r["收盘均"]:>7.2f}%{r["收盘中"]:>7.2f}%{r["最高均"]:>7.2f}%{r["连板率"]:>7.0f}%')

    out = os.path.join(ROOT, 'output',
                       f'first_board_premium_{datetime.date.today().strftime("%Y%m%d")}.csv')
    df.to_csv(out, index=False, encoding='utf-8-sig')
    print(f'\n明细已保存: {out}')


if __name__ == '__main__':
    main()
