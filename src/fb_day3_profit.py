# -*- coding: utf-8 -*-
"""首板次日收盘买入 → 第三日收益实证, 及"买入当日题材发酵"的预测力。

样本: topic_map_20261021.csv 已转录的 330 只首板(9/23~10/15, 非ST主板10cm)。
定义:
  Day0 = 首板日; Day1 = 次日(收盘买入); Day2 = 第三日(验证收益)。
  收益: Day2 开盘/收盘/最高 相对 Day1 收盘。
  发酵: 同题材(图内分类, 已归一化)在 Day1 的涨停家数 n_lu1,
        相对 Day0 的变化 delta, 以及 Day1 同题材"新首板"家数(扩散)。
输出: output/fb_day3_profit_20261021.csv (事件明细), 控制台打印分组统计。
"""
import os
import pandas as pd
import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, 'output')
STOCKDB_DIR = os.path.join(BASE, 'stockdb_csv')

NORMALIZE = {
    '传媒': 'AI应用/传媒', 'AI应用': 'AI应用/传媒',
    '大消费/冰雪产业': '大消费',
    '算力/半导体产业链': '算力/半导体',
    '军工/航天': '航天/低空经济', '航天': '航天/低空经济',
    '化工': '化工/染料', '染料': '化工/染料', '化工/大农业': '化工/染料',
    '风电': '电力/风电', '电力': '电力/风电', '电力/数据中心供电': '电力/风电',
    '煤炭/煤化工': '煤炭',
}

LU_TH = 9.79  # 主板10cm涨停判定(含容差), 与项目口径一致


def tail_rows(fp, n=60):
    """只读 CSV 尾部 n 行(stockdb_csv GBK 格式), 避免全量读全历史。"""
    with open(fp, 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 16384))
        chunk = f.read().decode('gbk', errors='ignore')
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    rows = []
    for ln in lines[-n:]:
        parts = ln.split(',')
        if len(parts) < 7:
            continue
        try:
            rows.append((parts[0], float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4]),
                         float(parts[5]), float(parts[6])))
        except ValueError:
            continue
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low',
                                     'close', 'volume', 'amount'])
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
    df['pct'] = df['close'].pct_change() * 100
    df['lu'] = df['pct'] >= LU_TH
    return df


def code_to_path(code6):
    exch = 'SH' if code6[0] in ('6', '9') else 'SZ'
    return os.path.join(STOCKDB_DIR, f'{exch}#{code6}.csv')


def main():
    prem = pd.read_csv(os.path.join(OUT, 'first_board_premium_20261021.csv'),
                       encoding='utf-8-sig')
    tmap = pd.read_csv(os.path.join(OUT, 'topic_map_20261021.csv'),
                       encoding='utf-8-sig')
    tmap['topic'] = tmap['topic'].replace(NORMALIZE)
    prem['代码'] = prem['代码'].astype(str).str.zfill(6)

    ev = tmap.merge(prem[['日期', '名称', '代码']],
                    left_on=['date', 'name'], right_on=['日期', '名称'],
                    how='left')[['date', 'name', '代码', 'topic']]
    ev = ev.dropna(subset=['代码']).drop_duplicates(subset=['date', 'name'])
    ev['date'] = pd.to_datetime(ev['date'])

    # 题材股票池(转录期间所有归属过该题材的票)
    roster = ev.groupby('topic')['代码'].apply(lambda s: sorted(set(s))).to_dict()

    # 每票尾部行情缓存
    cache = {}

    def get_df(code6):
        if code6 not in cache:
            fp = code_to_path(code6)
            cache[code6] = tail_rows(fp) if os.path.exists(fp) else None
        return cache[code6]

    # 全窗口 题材×日期 涨停家数 / 新首板家数
    all_dates = sorted(ev['date'].unique())
    lu_cnt = {}   # (topic, date) -> 涨停家数
    newfb_cnt = {}  # (topic, date) -> 当日新首板家数(昨未涨停今涨停)
    for topic, codes in roster.items():
        for c in codes:
            df = get_df(c)
            if df is None:
                continue
            for _, r in df.iterrows():
                if r['date'] < pd.Timestamp('2026-09-20'):
                    continue
                if r['lu']:
                    key = (topic, r['date'])
                    lu_cnt[key] = lu_cnt.get(key, 0) + 1
    for topic, codes in roster.items():
        for c in codes:
            df = get_df(c)
            if df is None:
                continue
            sub = df[df['date'] >= pd.Timestamp('2026-09-20')]
            for i in range(1, len(sub)):
                if sub['lu'].iloc[i] and not sub['lu'].iloc[i - 1]:
                    key = (topic, sub['date'].iloc[i])
                    newfb_cnt[key] = newfb_cnt.get(key, 0) + 1

    rows = []
    for _, e in ev.iterrows():
        df = get_df(e['代码'])
        if df is None:
            continue
        idx = df.index[df['date'] == e['date']]
        if len(idx) == 0:
            continue
        i = idx[0]
        if i + 2 >= len(df) + 0 and i + 2 > len(df) - 1:
            continue  # 需要 Day1/Day2 都存在
        if i + 1 > len(df) - 1 or i + 2 > len(df) - 1:
            continue
        d0, d1, d2 = df.iloc[i], df.iloc[i + 1], df.iloc[i + 2]
        c0, c1 = d0['close'], d1['close']
        lu1 = bool(d1['lu'])
        close_prem = (c1 / c0 - 1) * 100
        high_prem = (d1['high'] / c0 - 1) * 100
        open_prem = (d1['open'] / c0 - 1) * 100
        touched_lu = high_prem >= LU_TH
        vr = d1['volume'] / d0['volume'] if d0['volume'] > 0 else np.nan
        t = e['topic']
        n_lu0 = lu_cnt.get((t, d0['date']), 0)
        n_lu1 = lu_cnt.get((t, d1['date']), 0)
        n_newfb1 = newfb_cnt.get((t, d1['date']), 0)
        rows.append({
            'date0': d0['date'].date(), 'code': e['代码'], 'name': e['name'],
            'topic': t,
            'open_prem1': round(open_prem, 2), 'close_prem1': round(close_prem, 2),
            'high_prem1': round(high_prem, 2),
            'lu1': lu1, 'touched_lu1': touched_lu, 'vol_ratio1': round(vr, 2),
            'n_lu0': n_lu0, 'n_lu1': n_lu1, 'delta_lu': n_lu1 - n_lu0,
            'newfb1': n_newfb1,
            'ret_o2': round((d2['open'] / c1 - 1) * 100, 2),
            'ret_c2': round((d2['close'] / c1 - 1) * 100, 2),
            'ret_h2': round((d2['high'] / c1 - 1) * 100, 2),
        })
    df_ev = pd.DataFrame(rows)
    df_ev.to_csv(os.path.join(OUT, 'fb_day3_profit_20261021.csv'),
                 index=False, encoding='utf-8-sig')

    def stat(sub):
        return pd.Series({
            'n': len(sub),
            'Day2开盘_中': sub['ret_o2'].median(),
            'Day2收盘_中': sub['ret_c2'].median(),
            'Day2收盘_均': round(sub['ret_c2'].mean(), 2),
            'Day2最高_中': sub['ret_h2'].median(),
            '胜率%': round((sub['ret_c2'] > 0).mean() * 100, 1),
            '盘中有利%': round((sub['ret_h2'] > 0.5).mean() * 100, 1),
        })

    print(f'有效事件: {len(df_ev)}')
    print('\n=== 基线(全体) ===')
    print(stat(df_ev).to_string())

    # 条件1: Day1 收盘强度
    def strength(r):
        if r['lu1']:
            return 'A.连板(收涨停)'
        if r['close_prem1'] >= 3:
            return 'B.收涨3~10%'
        if r['close_prem1'] >= 0:
            return 'C.收涨0~3%'
        if r['close_prem1'] >= -3:
            return 'D.收跌0~3%'
        return 'E.收跌>3%'
    df_ev['Day1强度'] = df_ev.apply(strength, axis=1)
    print('\n=== 按 Day1 收盘强度分组 ===')
    print(df_ev.groupby('Day1强度').apply(stat, include_groups=False).to_string())

    # 条件2: 题材发酵(Day1 同题材涨停家数)
    def ferm(r):
        if r['n_lu1'] >= 5:
            return 'A.>=5家(强发酵)'
        if r['n_lu1'] >= 3:
            return 'B.3~4家'
        if r['n_lu1'] == 2:
            return 'C.2家'
        return 'D.1家(仅自己,无发酵)'
    df_ev['发酵'] = df_ev.apply(ferm, axis=1)
    print('\n=== 按 Day1 题材发酵(涨停家数)分组 ===')
    print(df_ev.groupby('发酵').apply(stat, include_groups=False).to_string())

    # 条件3: 发酵方向
    df_ev['发酵方向'] = pd.cut(df_ev['delta_lu'], [-99, -1, 0, 99],
                           labels=['减少', '持平', '增加'])
    print('\n=== 按 发酵方向(Day1-Day0 涨停家数变化) 分组 ===')
    print(df_ev.groupby('发酵方向', observed=True).apply(stat, include_groups=False).to_string())

    # 条件4: 交叉 发酵 × Day1强度
    df_ev['发酵二分'] = np.where(df_ev['n_lu1'] >= 3, '发酵>=3家', '发酵<3家')
    df_ev['强度三分'] = np.where(df_ev['lu1'], '连板',
                          np.where(df_ev['close_prem1'] >= 0, '收涨非板', '收跌'))
    print('\n=== 交叉: 发酵 × Day1强度 (Day2收盘中位 / 胜率%) ===')
    piv_r = df_ev.pivot_table(index='强度三分', columns='发酵二分',
                              values='ret_c2', aggfunc='median')
    piv_w = df_ev.pivot_table(index='强度三分', columns='发酵二分',
                              values='ret_c2', aggfunc=lambda s: round((s > 0).mean() * 100, 1))
    piv_n = df_ev.pivot_table(index='强度三分', columns='发酵二分',
                              values='ret_c2', aggfunc='size')
    print('Day2收盘中位:'); print(piv_r.round(2).to_string())
    print('胜率%:'); print(piv_w.to_string())
    print('样本数:'); print(piv_n.to_string())

    # 条件5: 同题材 Day1 新首板(扩散)
    df_ev['扩散'] = np.where(df_ev['newfb1'] >= 2, '新首板>=2',
                      np.where(df_ev['newfb1'] == 1, '新首板=1', '无新首板'))
    print('\n=== 按 Day1 同题材新首板(扩散) 分组 ===')
    print(df_ev.groupby('扩散').apply(stat, include_groups=False).to_string())

    # 条件6: 量比
    df_ev['量比档'] = pd.cut(df_ev['vol_ratio1'], [0, 0.5, 1, 2, 999],
                         labels=['<0.5(缩)', '0.5~1', '1~2', '>=2(爆)'])
    print('\n=== 按 Day1/Day0 量比 分组 ===')
    print(df_ev.groupby('量比档', observed=True).apply(stat, include_groups=False).to_string())


if __name__ == '__main__':
    main()
