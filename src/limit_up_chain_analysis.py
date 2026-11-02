# -*- coding: utf-8 -*-
"""首板→二板→三板 全链路分析 (最近一年, 按题材/时间段分组)

口径:
  首板 = 当日涨停 且 前一日未涨停, 剔ST, 上市>5日
  二板 = 首板次日(T+1)涨停
  三板 = 二板次日(T+2)涨停
  二板溢价% = 二板开盘价/收盘价/最高价 对首板收盘价的溢价
  三板溢价% = 三板开盘价/收盘价/最高价 对二板收盘价的溢价
  连板率   = 下一板涨停数量 / 上一板有次日数据的数量

时间段:
  前期 = 2025-10-21 ~ 2026-06-30 (下跌期)
  后期 = 2026-07-01 ~ 2026-10-20 (反弹期)

用法: python src/limit_up_chain_analysis.py
"""
import os, sys, io
import pandas as pd
import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKDB_DIR = os.path.join(ROOT, 'stockdb_csv')
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
    """返回日期列表 + OHLCV(元) 列表"""
    d, o, h, lo, c, v, a = [], [], [], [], [], [], []
    with open(fp, 'r', encoding='gbk', errors='ignore') as f:
        lines = f.readlines()
    for ln in lines[2:]:
        p = ln.strip().split(',')
        if len(p) in (7, 8) and p[0][:4].isdigit() and '-' in p[0]:
            d.append(p[0]); o.append(float(p[1])); h.append(float(p[2]))
            lo.append(float(p[3])); c.append(float(p[4])); v.append(float(p[5]))
            a.append(float(p[6]))
    return d, o, h, lo, c, v, a

def main():
    ind = pd.read_csv(IND_FILE, dtype=str)
    ind['code6'] = ind['ts_code'].str.split('.').str[0]
    name_map = ind.set_index('code6')['name'].to_dict()
    ind_map = ind.set_index('code6')['industry'].to_dict()

    files = sorted([f for f in os.listdir(STOCKDB_DIR) if f.endswith('.csv')])
    print(f'扫描 {len(files)} 只股票, 窗口 {START}~{END}, 分割点 {SPLIT}')

    records = []  # 每行=一个首板事件
    for fn in files:
        code6 = fn.split('#')[1].split('.')[0]
        name = name_map.get(code6, '')
        industry = ind_map.get(code6, '未知') or '未知'
        if 'ST' in str(name).upper():
            continue
        thr = limit_threshold(code6, name)

        fp = os.path.join(STOCKDB_DIR, fn)
        try:
            d, o, h, lo, c, v, a = load_stock(fp)
        except Exception:
            continue
        if len(d) < 10:
            continue

        # 找到所有涨停日
        limit_up = [False] * len(d)
        for i in range(1, len(d)):
            pct = (c[i] / c[i-1] - 1) * 100
            limit_up[i] = pct >= thr

        # 找首板 (当日涨停, 前一日不涨停)
        for i in range(1, len(d) - 2):  # 留2天给二板、三板
            if not (START <= d[i] <= END):
                continue
            if not limit_up[i]:
                continue
            if limit_up[i-1]:
                continue  # 非首板

            # 首板特征
            first_sealed = (lo[i] / c[i-1] - 1) * 100 >= thr  # 一封到底
            first_pct = (c[i] / c[i-1] - 1) * 100
            first_amt = a[i] / 1e8  # 亿元
            # 20日量比
            if i >= 20:
                avg20 = sum(v[i-20:i]) / 20
                first_vr = v[i] / avg20 if avg20 > 0 else np.nan
            else:
                first_vr = np.nan

            # 二板
            has_t1 = i + 1 < len(d)
            is_2nd = has_t1 and limit_up[i+1]
            second_sealed = None
            second_pct = None
            second_vr = None
            second_amt = None
            p2_open = p2_close = p2_high = np.nan
            if has_t1:
                p2_open = (o[i+1] / c[i] - 1) * 100
                p2_close = (c[i+1] / c[i] - 1) * 100
                p2_high = (h[i+1] / c[i] - 1) * 100
                if is_2nd:
                    second_sealed = (lo[i+1] / c[i] - 1) * 100 >= thr
                    second_pct = (c[i+1] / c[i] - 1) * 100
                    second_amt = a[i+1] / 1e8
                    if i + 1 >= 20:
                        avg20b = sum(v[i+1-20:i+1]) / 20
                        second_vr = v[i+1] / avg20b if avg20b > 0 else np.nan

            # 三板
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
                '首板涨幅%': round(first_pct, 2),
                '首板成交额亿': round(first_amt, 2),
                '首板量比': round(first_vr, 2) if not np.isnan(first_vr) else None,
                '首板一封到底': first_sealed,
                '二板': is_2nd,
                '二板开盘溢价%': round(p2_open, 2) if has_t1 else None,
                '二板收盘溢价%': round(p2_close, 2) if has_t1 else None,
                '二板最高溢价%': round(p2_high, 2) if has_t1 else None,
                '二板一封到底': second_sealed,
                '二板量比': round(second_vr, 2) if second_vr is not None else None,
                '二板成交额亿': round(second_amt, 2) if second_amt is not None else None,
                '三板': is_3rd,
                '三板开盘溢价%': round(p3_open, 2) if has_t2 else None,
                '三板收盘溢价%': round(p3_close, 2) if has_t2 else None,
                '三板最高溢价%': round(p3_high, 2) if has_t2 else None,
            })

    df = pd.DataFrame(records)
    if df.empty:
        print('无数据'); return

    df = df.sort_values(['首板日', '代码']).reset_index(drop=True)
    df.to_csv(os.path.join(OUT_DIR, 'limit_up_chain_1y.csv'),
              index=False, encoding='utf-8-sig')

    print(f'\n总首板样本: {len(df)}, 涉及交易日: {df["首板日"].nunique()}')
    print(f'前期(下跌期): {len(df[df["时期"]=="前期"])}  后期(反弹期): {len(df[df["时期"]=="后期"])}')

    # ========== 1. 按题材分组: 首板→二板→三板 连板率 & 溢价率 ==========
    print('\n' + '='*80)
    print('1. 按题材分组 (首板数>=5)')
    print('='*80)

    g1 = df.groupby('题材').agg(
        首板数=('代码', 'count'),
        二板数=('二板', 'sum'),
        三板数=('三板', 'sum'),
        二板率=('二板', lambda x: x.mean() * 100),
        三板率=('三板', lambda x: x.mean() * 100),
        二板开盘均=('二板开盘溢价%', 'mean'),
        二板收盘均=('二板收盘溢价%', 'mean'),
        二板最高均=('二板最高溢价%', 'mean'),
        三板开盘均=('三板开盘溢价%', 'mean'),
        三板收盘均=('三板收盘溢价%', 'mean'),
    ).round(2)
    g1 = g1[g1['首板数'] >= 5].sort_values('二板率', ascending=False)

    print(f'{"题材":<30}{"首板":>4}{"二板":>4}{"三板":>4}{"二板率":>8}{"三板率":>8}'
          f'{"二板开":>8}{"二板收":>8}{"二板高":>8}{"三板开":>8}{"三板收":>8}')
    for ix, r in g1.iterrows():
        print(f'{ix:<30}{r["首板数"]:>4}{r["二板数"]:>4}{r["三板数"]:>4}'
              f'{r["二板率"]:>7.1f}%{r["三板率"]:>7.1f}%'
              f'{r["二板开盘均"]:>7.2f}%{r["二板收盘均"]:>7.2f}%{r["二板最高均"]:>7.2f}%'
              f'{r["三板开盘均"]:>7.2f}%{r["三板收盘均"]:>7.2f}%')

    # 溢价率最低 = 二板收盘溢价均值最低
    print('\n--- 溢价率最低 (二板收盘溢价均值升序, 前10) ---')
    worst = g1.nsmallest(10, '二板收盘均')
    for ix, r in worst.iterrows():
        print(f'{ix:<30} 首板{r["首板数"]:>3} 二板率{r["二板率"]:>6.1f}% 二板收{r["二板收盘均"]:>6.2f}%')

    # ========== 2. 七月前后对比 ==========
    print('\n' + '='*80)
    print(f'2. 七月前后对比 (分割点 {SPLIT})')
    print('='*80)

    for period in ['前期', '后期']:
        sub = df[df['时期'] == period]
        print(f'\n--- {period} ({sub["首板日"].min()} ~ {sub["首板日"].max()}, n={len(sub)}) ---')
        print(f'首板数: {len(sub)}, 二板数: {sub["二板"].sum()}, 三板数: {sub["三板"].sum()}')
        print(f'二板率: {sub["二板"].mean()*100:.1f}%  三板率: {sub["三板"].mean()*100:.1f}%')
        print(f'二板开盘溢价: 均值{sub["二板开盘溢价%"].mean():.2f}% 中位{sub["二板开盘溢价%"].median():.2f}%')
        print(f'二板收盘溢价: 均值{sub["二板收盘溢价%"].mean():.2f}% 中位{sub["二板收盘溢价%"].median():.2f}%')
        print(f'三板开盘溢价: 均值{sub["三板开盘溢价%"].mean():.2f}% 中位{sub["三板开盘溢价%"].median():.2f}%')
        print(f'三板收盘溢价: 均值{sub["三板收盘溢价%"].mean():.2f}% 中位{sub["三板收盘溢价%"].median():.2f}%')

    # 按题材分组的前后对比
    print('\n--- 分题材前后对比 (首板数>=5, 二板率差值排序) ---')
    g2 = df.groupby(['题材', '时期']).agg(
        首板数=('代码', 'count'),
        二板率=('二板', lambda x: x.mean() * 100),
        三板率=('三板', lambda x: x.mean() * 100),
        二板收盘均=('二板收盘溢价%', 'mean'),
    ).round(2).reset_index()
    pivot2 = g2.pivot(index='题材', columns='时期', values=['首板数', '二板率', '三板率', '二板收盘均'])
    pivot2.columns = [f'{c[1]}_{c[0]}' for c in pivot2.columns]
    pivot2['首板总数'] = pivot2.get('前期_首板数', 0).fillna(0) + pivot2.get('后期_首板数', 0).fillna(0)
    pivot2['二板率差'] = pivot2.get('后期_二板率', 0).fillna(0) - pivot2.get('前期_二板率', 0).fillna(0)
    pivot2['三板率差'] = pivot2.get('后期_三板率', 0).fillna(0) - pivot2.get('前期_三板率', 0).fillna(0)
    pivot2 = pivot2[pivot2['首板总数'] >= 5].sort_values('二板率差', ascending=False)

    print(f'{"题材":<30}{"总n":>4}{"前二板":>8}{"后二板":>8}{"差":>8}{"前三板":>8}{"后三板":>8}{"差":>8}')
    for ix, r in pivot2.iterrows():
        f2 = r.get('前期_二板率', np.nan)
        l2 = r.get('后期_二板率', np.nan)
        f3 = r.get('前期_三板率', np.nan)
        l3 = r.get('后期_三板率', np.nan)
        print(f'{ix:<30}{r["首板总数"]:>4}'
              f'{f2:>7.1f}%{l2:>7.1f}%{r["二板率差"]:>+7.1f}pct'
              f'{f3:>7.1f}%{l3:>7.1f}%{r["三板率差"]:>+7.1f}pct')

    # ========== 3. 首板数据对二板的影响 ==========
    print('\n' + '='*80)
    print('3. 首板数据 → 二板的影响')
    print('='*80)

    # 3a. 一封到底 vs 非一封到底
    print('\n--- 首板是否一封到底 ---')
    for sealed in [True, False]:
        sub = df[df['首板一封到底'] == sealed]
        label = '一封到底' if sealed else '非一封到底'
        print(f'{label}: n={len(sub)}, 二板率{sub["二板"].mean()*100:.1f}%, '
              f'二板开{sub["二板开盘溢价%"].mean():.2f}%, 二板收{sub["二板收盘溢价%"].mean():.2f}%')

    # 3b. 首板量比分组
    print('\n--- 首板量比(当日量/20日均量) ---')
    df['量比档'] = pd.cut(df['首板量比'], bins=[0, 0.5, 0.8, 1.2, 2.0, 5.0, 999],
                        labels=['<0.5', '0.5-0.8', '0.8-1.2', '1.2-2.0', '2.0-5.0', '>5'])
    g3 = df.groupby('量比档', observed=True).agg(
        n=('代码', 'count'),
        二板率=('二板', lambda x: x.mean() * 100),
        二板开均=('二板开盘溢价%', 'mean'),
        二板收均=('二板收盘溢价%', 'mean'),
        三板率=('三板', lambda x: x.mean() * 100),
    ).round(2)
    print(g3.to_string())

    # 3c. 首板成交额分组
    print('\n--- 首板成交额 ---')
    df['成交额档'] = pd.cut(df['首板成交额亿'], bins=[0, 0.5, 1, 3, 5, 10, 999],
                          labels=['<0.5亿', '0.5-1亿', '1-3亿', '3-5亿', '5-10亿', '>10亿'])
    g4 = df.groupby('成交额档', observed=True).agg(
        n=('代码', 'count'),
        二板率=('二板', lambda x: x.mean() * 100),
        二板开均=('二板开盘溢价%', 'mean'),
        二板收均=('二板收盘溢价%', 'mean'),
    ).round(2)
    print(g4.to_string())

    # ========== 4. 二板数据对三板的影响 ==========
    print('\n' + '='*80)
    print('4. 二板数据 → 三板的影响 (仅二板样本)')
    print('='*80)

    df2 = df[df['二板'] == True].copy()
    print(f'二板总样本: {len(df2)}')

    # 4a. 二板一封到底
    print('\n--- 二板是否一封到底 ---')
    for sealed in [True, False]:
        sub = df2[df2['二板一封到底'] == sealed]
        label = '一封到底' if sealed else '非一封到底'
        if len(sub) > 0:
            print(f'{label}: n={len(sub)}, 三板率{sub["三板"].mean()*100:.1f}%, '
                  f'三板开{sub["三板开盘溢价%"].mean():.2f}%, 三板收{sub["三板收盘溢价%"].mean():.2f}%')

    # 4b. 二板量比
    print('\n--- 二板量比 ---')
    df2['量比档'] = pd.cut(df2['二板量比'], bins=[0, 0.5, 0.8, 1.2, 2.0, 5.0, 999],
                         labels=['<0.5', '0.5-0.8', '0.8-1.2', '1.2-2.0', '2.0-5.0', '>5'])
    g5 = df2.groupby('量比档', observed=True).agg(
        n=('代码', 'count'),
        三板率=('三板', lambda x: x.mean() * 100),
        三板开均=('三板开盘溢价%', 'mean'),
        三板收均=('三板收盘溢价%', 'mean'),
    ).round(2)
    print(g5.to_string())

    # 4c. 二板成交额
    print('\n--- 二板成交额 ---')
    df2['成交额档'] = pd.cut(df2['二板成交额亿'], bins=[0, 0.5, 1, 3, 5, 10, 999],
                           labels=['<0.5亿', '0.5-1亿', '1-3亿', '3-5亿', '5-10亿', '>10亿'])
    g6 = df2.groupby('成交额档', observed=True).agg(
        n=('代码', 'count'),
        三板率=('三板', lambda x: x.mean() * 100),
        三板开均=('三板开盘溢价%', 'mean'),
        三板收均=('三板收盘溢价%', 'mean'),
    ).round(2)
    print(g6.to_string())

    print(f'\n明细已保存: {os.path.join(OUT_DIR, "limit_up_chain_1y.csv")}')

if __name__ == '__main__':
    main()
