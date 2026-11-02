# -*- coding: utf-8 -*-
"""首板次日K线形态买入(接tushare daily_basic换手率) 实证。

流程:
  Day0 = 首板日(涨幅>=9.79%, 昨日未涨停);
  Day1 = 次日, 判定形态1(光脚阴线)/形态2(高开抗跌), volume<=2*昨日量,
         turnover_rate>10%(tushare daily_basic);
  尾盘(Day1收盘)买入, Day2 开盘/收盘/最高 三种退出。
输出: output/fb_pattern_turnover_20261021.csv
"""
import os
import sys
import time
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKDB_DIR = os.path.join(BASE, 'stockdb_csv')
OUT = os.path.join(BASE, 'output')
CACHE = os.path.join(OUT, '_daily_basic_cache')
os.makedirs(CACHE, exist_ok=True)

TOKEN = os.environ.get('TUSHARE_TOKEN', '')
START = pd.Timestamp('2021-08-01')
LU_TH = 9.79
EPS = 0.001
FEE = 0.0015

ONLY = sys.argv[1] if len(sys.argv) > 1 else 'all'   # all / scan / report


def load(fp):
    df = pd.read_csv(fp, encoding='gbk', skiprows=2, header=None,
                     names=['date', 'open', 'high', 'low', 'close',
                            'volume', 'amount', 'turnover'])
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date'])
    return df[df['date'] >= START - pd.Timedelta(days=10)].reset_index(drop=True)


def scan_events():
    """stockdb_csv 扫首板 + Day1形态 + Day2收益 (不含换手率)"""
    events = []
    for fn in os.listdir(STOCKDB_DIR):
        if not fn.endswith('.csv'):
            continue
        code6 = fn.split('#')[1][:6]
        exch = fn.split('#')[0]
        try:
            df = load(os.path.join(STOCKDB_DIR, fn))
        except Exception:
            continue
        if len(df) < 30:
            continue
        o, h, lo, c, v = (df[k].values for k in
                          ['open', 'high', 'low', 'close', 'volume'])
        dt = df['date'].values
        pct = np.concatenate([[np.nan], np.diff(c) / c[:-1] * 100])
        lu = pct >= LU_TH
        fb = lu & ~np.roll(lu, 1)          # 首板
        fb[0] = False
        for i in np.where(fb & (dt >= START))[0]:
            if i + 2 >= len(df):
                continue
            # Day1 形态
            p1 = (o[i + 1] < o[i]) and (c[i + 1] < o[i + 1]) and \
                 (lo[i + 1] >= c[i + 1] * (1 - EPS))
            p2 = (o[i + 1] > o[i]) and (c[i + 1] >= c[i])
            vol_ok = v[i + 1] <= 2.0 * v[i]
            events.append({
                'code': f'{code6}.{exch}',
                'd0': pd.Timestamp(dt[i]).date(),
                'd1': pd.Timestamp(dt[i + 1]).date(),
                'p1': bool(p1 and vol_ok),
                'p2': bool(p2 and vol_ok),
                'close_prem1': round((c[i + 1] / c[i] - 1) * 100, 2),
                'ret_o2': round((o[i + 2] / c[i + 1] - 1) * 100, 2),
                'ret_c2': round((c[i + 2] / c[i + 1] - 1) * 100, 2),
                'ret_h2': round((h[i + 2] / c[i + 1] - 1) * 100, 2),
            })
    ev = pd.DataFrame(events)
    ev.to_csv(os.path.join(OUT, '_fb_pattern_raw.csv'), index=False,
              encoding='utf-8-sig')
    print(f'首板事件(形态已标注): {len(ev)}, p1={ev.p1.sum()}, p2={ev.p2.sum()}')
    return ev


def fetch_turnover_bs(codes):
    """baostock 按个股拉 5 年 turn(换手率), 存 output/_turn_bs.csv (增量)"""
    import baostock as bs
    fp_all = os.path.join(OUT, '_turn_bs.csv')
    done = set()
    if os.path.exists(fp_all):
        done = set(pd.read_csv(fp_all, usecols=['code'])['code'].astype(str).unique())
    todo = [c for c in sorted(set(codes)) if c not in done]
    print(f'baostock 换手率: 已缓存 {len(done)}, 待拉 {len(todo)}')
    if not todo:
        return
    bs.login()
    out_f = open(fp_all, 'a', encoding='utf-8')
    if not os.path.exists(fp_all) or os.path.getsize(fp_all) == 0:
        out_f.write('code,date,turn\n')
    for k, code in enumerate(todo):
        num, exch = code.split('.')
        bs_code = f'{exch.lower()}.{num}'
        try:
            rs = bs.query_history_k_data_plus(
                bs_code, 'date,turn', start_date=str(START.date()),
                end_date='2026-10-20', frequency='d')
            rows = []
            while rs.error_code == '0' and rs.next():
                rows.append(rs.get_row_data())
            for r in rows:
                if r[1]:
                    out_f.write(f'{code},{r[0]},{r[1]}\n')
        except Exception as e:
            print(f'  {code} 失败: {e}')
        if (k + 1) % 200 == 0:
            out_f.flush()
            print(f'  进度 {k + 1}/{len(todo)}')
    out_f.close()
    bs.logout()


def merge_report(ev):
    tb = pd.read_csv(os.path.join(OUT, '_turn_bs.csv'), dtype={'code': str})
    tb = tb.rename(columns={'date': 'd1'})
    tb['d1'] = pd.to_datetime(tb['d1']).dt.date.astype(str)
    ev['d1'] = ev['d1'].astype(str)
    tb = tb.drop_duplicates(subset=['code', 'd1'])
    ev = ev.merge(tb[['code', 'd1', 'turn']], on=['code', 'd1'], how='left')
    ev = ev.rename(columns={'turn': 'turnover_rate'})
    ev['turnover_rate'] = pd.to_numeric(ev['turnover_rate'], errors='coerce')
    ev.to_csv(os.path.join(OUT, 'fb_pattern_turnover_20261021.csv'),
              index=False, encoding='utf-8-sig')
    print(f'换手率匹配率: {ev["turnover_rate"].notna().mean() * 100:.1f}%')

    def stat(sub, tag):
        if len(sub) < 10:
            print(f'{tag}: 样本过少 n={len(sub)}')
            return
        net = sub['ret_c2'] - FEE * 100
        print(f'--- {tag} (n={len(sub)}) ---')
        print(f'  Day2开盘卖: 中位{sub["ret_o2"].median():+.2f}% 均值{sub["ret_o2"].mean():+.2f}% 胜率{(sub["ret_o2"] > 0).mean() * 100:.1f}%')
        print(f'  Day2收盘卖: 中位{sub["ret_c2"].median():+.2f}% 均值{sub["ret_c2"].mean():+.2f}% 胜率{(sub["ret_c2"] > 0).mean() * 100:.1f}% 扣费{net.mean():+.2f}%')
        print(f'  盘中冲高>1%率: {(sub["ret_h2"] > 1).mean() * 100:.1f}%  最高中位{sub["ret_h2"].median():+.2f}%')

    print('\n========== 无换手率过滤 ==========')
    stat(ev[ev['p1']], '形态1 光脚阴线')
    stat(ev[ev['p2']], '形态2 高开抗跌')
    print('\n========== 换手率>10% ==========')
    h = ev[ev['turnover_rate'] > 10]
    stat(h[h['p1']], '形态1+换手>10%')
    stat(h[h['p2']], '形态2+换手>10%')
    print('\n========== 换手率分档(形态2) ==========')
    ev['to档'] = pd.cut(ev['turnover_rate'], [0, 3, 5, 10, 15, 20, 999],
                    labels=['<3%', '3~5%', '5~10%', '10~15%', '15~20%', '>20%'])
    g = ev[ev['p2']].groupby('to档', observed=True)
    print(pd.DataFrame({
        'n': g.size(),
        '收盘卖_均值%': g['ret_c2'].mean().round(2),
        '扣费后%': (g['ret_c2'].mean() - FEE * 100).round(2),
        '胜率%': g['ret_c2'].apply(lambda s: (s > 0).mean() * 100).round(1),
        '冲高>1%率%': g['ret_h2'].apply(lambda s: (s > 1).mean() * 100).round(1),
    }).to_string())
    print('\n========== 换手率分档(形态1) ==========')
    g = ev[ev['p1']].groupby('to档', observed=True)
    print(pd.DataFrame({
        'n': g.size(),
        '收盘卖_均值%': g['ret_c2'].mean().round(2),
        '扣费后%': (g['ret_c2'].mean() - FEE * 100).round(2),
        '胜率%': g['ret_c2'].apply(lambda s: (s > 0).mean() * 100).round(1),
        '冲高>1%率%': g['ret_h2'].apply(lambda s: (s > 1).mean() * 100).round(1),
    }).to_string())


if __name__ == '__main__':
    raw_fp = os.path.join(OUT, '_fb_pattern_raw.csv')
    ev = pd.read_csv(raw_fp, encoding='utf-8-sig', dtype={'code': str})
    if ONLY in ('all', 'scan'):
        fetch_turnover_bs(list(ev['code'].unique()))
    if ONLY == 'scan':
        sys.exit(0)
    merge_report(ev)
