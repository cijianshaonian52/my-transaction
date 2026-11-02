# -*- coding: utf-8 -*-
"""首板→二板→三板 全链路分析 + 换手率维度 (数据源 stockdb_csv, 含换手率)

口径:
  首板 = 当日涨停 且 前一日未涨停, 剔ST
  换手率% = 当日成交量 / 流通股本 × 100 (stockdb_csv 第8列, 已是百分数)
分割点: 2026-07-01 (前期下跌 vs 后期反弹)
用法: python src/limit_up_chain_turnover.py
"""
import os, sys, io
import pandas as pd
import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, 'stockdb_csv')
IND_FILE = os.path.join(ROOT, 'cache_data', '_stock_industry.csv')
OUT_DIR = os.path.join(ROOT, 'output')

START = '2025-10-21'
END   = '2026-10-20'
SPLIT = '2026-07-01'

def limit_threshold(code6, name):
    if code6.startswith(('300', '301', '688', '689')):
        return 19.8
    if code6.startswith(('43', '83', '87', '920')):
        return 29.8
    if name and 'ST' in str(name).upper():
        return 4.8
    return 9.8

def load_stock(fp):
    """返回 d, o, h, lo, c, v, a, tor(换手率%)"""
    d, o, h, lo, c, v, a, tor = [], [], [], [], [], [], [], []
    with open(fp, 'r', encoding='gbk', errors='ignore') as f:
        lines = f.readlines()
    for ln in lines[2:]:
        p = ln.strip().split(',')
        if len(p) >= 8 and p[0][:4].isdigit() and '-' in p[0]:
            d.append(p[0]); o.append(float(p[1])); h.append(float(p[2]))
            lo.append(float(p[3])); c.append(float(p[4])); v.append(float(p[5]))
            a.append(float(p[6])); tor.append(float(p[7]))
    return d, o, h, lo, c, v, a, tor

def main():
    ind = pd.read_csv(IND_FILE, dtype=str)
    ind['code6'] = ind['ts_code'].str.split('.').str[0]
    name_map = ind.set_index('code6')['name'].to_dict()
    ind_map = ind.set_index('code6')['industry'].to_dict()

    files = sorted([f for f in os.listdir(DATA_DIR) if f.endswith('.csv')])
    print(f'数据源 stockdb_csv (含换手率), 扫描 {len(files)} 只')

    records = []
    for fn in files:
        code6 = fn.split('#')[1].split('.')[0]
        name = name_map.get(code6, '')
        industry = ind_map.get(code6, '未知') or '未知'
        if 'ST' in str(name).upper():
            continue
        thr = limit_threshold(code6, name)

        fp = os.path.join(DATA_DIR, fn)
        try:
            d, o, h, lo, c, v, a, tor = load_stock(fp)
        except Exception:
            continue
        if len(d) < 10:
            continue

        limit_up = [False] * len(d)
        for i in range(1, len(d)):
            limit_up[i] = (c[i] / c[i-1] - 1) * 100 >= thr

        for i in range(1, len(d) - 2):
            if not (START <= d[i] <= END):
                continue
            if not limit_up[i] or limit_up[i-1]:
                continue

            first_sealed = (lo[i] / c[i-1] - 1) * 100 >= thr
            first_amt = a[i] / 1e8
            first_tor = tor[i]
            first_vr = v[i] / (sum(v[i-20:i])/20) if i >= 20 and sum(v[i-20:i]) > 0 else np.nan

            has_t1 = i + 1 < len(d)
            is_2nd = has_t1 and limit_up[i+1]
            second_sealed = None
            second_tor = None
            second_amt = None
            second_vr = None
            p2_open = p2_close = p2_high = np.nan
            if has_t1:
                p2_open = (o[i+1] / c[i] - 1) * 100
                p2_close = (c[i+1] / c[i] - 1) * 100
                p2_high = (h[i+1] / c[i] - 1) * 100
                if is_2nd:
                    second_sealed = (lo[i+1] / c[i] - 1) * 100 >= thr
                    second_tor = tor[i+1]
                    second_amt = a[i+1] / 1e8
                    if i + 1 >= 20:
                        avg20b = sum(v[i+1-20:i+1]) / 20
                        second_vr = v[i+1] / avg20b if avg20b > 0 else np.nan

            has_t2 = is_2nd and (i + 2 < len(d))
            is_3rd = has_t2 and limit_up[i+2]
            p3_open = p3_close = p3_high = np.nan
            if has_t2:
                p3_open = (o[i+2] / c[i+1] - 1) * 100
                p3_close = (c[i+2] / c[i+1] - 1) * 100
                p3_high = (h[i+2] / c[i+1] - 1) * 100

            period = '前期' if d[i] < SPLIT else '后期'
            records.append({
                '代码': code6, '名称': name, '题材': industry,
                '首板日': d[i], '时期': period,
                '首板成交额亿': round(first_amt, 2),
                '首板量比': round(first_vr, 2) if not np.isnan(first_vr) else None,
                '首板换手率%': round(first_tor, 2),
                '首板一封到底': first_sealed,
                '二板': is_2nd,
                '二板开盘溢价%': round(p2_open, 2) if has_t1 else None,
                '二板收盘溢价%': round(p2_close, 2) if has_t1 else None,
                '二板最高溢价%': round(p2_high, 2) if has_t1 else None,
                '二板一封到底': second_sealed,
                '二板量比': round(second_vr, 2) if second_vr is not None else None,
                '二板成交额亿': round(second_amt, 2) if second_amt is not None else None,
                '二板换手率%': round(second_tor, 2) if second_tor is not None else None,
                '三板': is_3rd,
                '三板开盘溢价%': round(p3_open, 2) if has_t2 else None,
                '三板收盘溢价%': round(p3_close, 2) if has_t2 else None,
                '三板最高溢价%': round(p3_high, 2) if has_t2 else None,
            })

    df = pd.DataFrame(records)
    if df.empty:
        print('无数据'); return
    df = df.sort_values(['首板日', '代码']).reset_index(drop=True)
    df.to_csv(os.path.join(OUT_DIR, 'limit_up_chain_turnover_1y.csv'),
              index=False, encoding='utf-8-sig')

    print(f'总首板样本: {len(df)}  前期: {len(df[df["时期"]=="前期"])}  后期: {len(df[df["时期"]=="后期"])}')
    print(f'换手率覆盖: 首板 {df["首板换手率%"].notna().mean()*100:.1f}%, 二板 {df[df["二板"]]["二板换手率%"].notna().mean()*100:.1f}%')

    # ========== A. 首板换手率分组 → 二板表现 ==========
    print('\n' + '='*80)
    print('A. 首板换手率 → 二板表现')
    print('='*80)
    df['首板换手档'] = pd.cut(df['首板换手率%'],
                          bins=[0, 2, 5, 10, 15, 20, 30, 999],
                          labels=['<2%', '2-5%', '5-10%', '10-15%', '15-20%', '20-30%', '>30%'])
    gA = df.groupby('首板换手档', observed=True).agg(
        n=('代码', 'count'),
        二板率=('二板', lambda x: x.mean() * 100),
        二板开=('二板开盘溢价%', 'mean'),
        二板收=('二板收盘溢价%', 'mean'),
        二板高=('二板最高溢价%', 'mean'),
        三板率=('三板', lambda x: x.mean() * 100),
    ).round(2)
    print(gA.to_string())

    # 前后分段
    print('\n--- 首板换手率 × 时期 ---')
    for period in ['前期', '后期']:
        sub = df[df['时期'] == period]
        g = sub.groupby('首板换手档', observed=True).agg(
            n=('代码', 'count'),
            二板率=('二板', lambda x: x.mean() * 100),
            二板收=('二板收盘溢价%', 'mean'),
        ).round(2)
        print(f'\n[{period}]')
        print(g.to_string())

    # ========== B. 二板换手率 → 三板表现 ==========
    print('\n' + '='*80)
    print('B. 二板换手率 → 三板表现 (仅二板样本)')
    print('='*80)
    df2 = df[df['二板'] == True].copy()
    df2['二板换手档'] = pd.cut(df2['二板换手率%'],
                           bins=[0, 2, 5, 10, 15, 20, 30, 999],
                           labels=['<2%', '2-5%', '5-10%', '10-15%', '15-20%', '20-30%', '>30%'])
    gB = df2.groupby('二板换手档', observed=True).agg(
        n=('代码', 'count'),
        三板率=('三板', lambda x: x.mean() * 100),
        三板开=('三板开盘溢价%', 'mean'),
        三板收=('三板收盘溢价%', 'mean'),
        三板高=('三板最高溢价%', 'mean'),
    ).round(2)
    print(gB.to_string())

    print('\n--- 二板换手率 × 时期 ---')
    for period in ['前期', '后期']:
        sub = df2[df2['时期'] == period]
        g = sub.groupby('二板换手档', observed=True).agg(
            n=('代码', 'count'),
            三板率=('三板', lambda x: x.mean() * 100),
            三板收=('三板收盘溢价%', 'mean'),
        ).round(2)
        print(f'\n[{period}]')
        print(g.to_string())

    # ========== C. 首板换手率 × 量比 联合 ==========
    print('\n' + '='*80)
    print('C. 首板换手率 × 量比 联合 (二板率)')
    print('='*80)
    df['量比档'] = pd.cut(df['首板量比'], bins=[0, 0.5, 0.8, 1.2, 2.0, 5.0, 999],
                        labels=['<0.5', '0.5-0.8', '0.8-1.2', '1.2-2.0', '2.0-5.0', '>5'])
    gC = df.pivot_table(index='首板换手档', columns='量比档',
                       values='二板', aggfunc=lambda x: f'{x.mean()*100:.1f}%({len(x)})',
                       observed=True)
    print(gC.to_string())

    # ========== D. 首板换手率 × 一封到底 联合 ==========
    print('\n' + '='*80)
    print('D. 首板换手率 × 一封到底 → 二板率')
    print('='*80)
    for sealed in [True, False]:
        label = '一封到底' if sealed else '非一封'
        sub = df[df['首板一封到底'] == sealed]
        g = sub.groupby('首板换手档', observed=True).agg(
            n=('代码', 'count'),
            二板率=('二板', lambda x: x.mean() * 100),
            二板收=('二板收盘溢价%', 'mean'),
        ).round(2)
        print(f'\n[{label}]')
        print(g.to_string())

    # ========== E. 二板换手率 × 一封到底 → 三板 ==========
    print('\n' + '='*80)
    print('E. 二板换手率 × 二板一封到底 → 三板率')
    print('='*80)
    for sealed in [True, False]:
        label = '一封到底' if sealed else '非一封'
        sub = df2[df2['二板一封到底'] == sealed]
        g = sub.groupby('二板换手档', observed=True).agg(
            n=('代码', 'count'),
            三板率=('三板', lambda x: x.mean() * 100),
            三板收=('三板收盘溢价%', 'mean'),
        ).round(2)
        print(f'\n[{label}]')
        print(g.to_string())

    # ========== F. 高换手 + 大成交额是否被高估? ==========
    print('\n' + '='*80)
    print('F. 首板换手率 × 成交额 → 二板率')
    print('='*80)
    df['成交额档'] = pd.cut(df['首板成交额亿'], bins=[0, 0.5, 1, 3, 5, 10, 999],
                          labels=['<0.5亿', '0.5-1亿', '1-3亿', '3-5亿', '5-10亿', '>10亿'])
    gF = df.pivot_table(index='首板换手档', columns='成交额档',
                       values='二板', aggfunc=lambda x: f'{x.mean()*100:.1f}%' if len(x)>0 else '-',
                       observed=True)
    print(gF.to_string())

    print(f'\n明细: {os.path.join(OUT_DIR, "limit_up_chain_turnover_1y.csv")}')

if __name__ == '__main__':
    main()
