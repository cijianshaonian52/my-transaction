# -*- coding: utf-8 -*-
"""验证"买不到"假说: 一封到底首板次日的一字板占比 + 能买到/买不到的表现差异

口径:
  买不到 = 次日一字板 (次日开盘价>=涨停价 且 次日最低价>=涨停价)
  能买到 = 次日开盘价<涨停价 (盘中有买入窗口)

对「首板+换手率<2%+一封到底+量比<0.5」最近一年全样本验证。
"""
import os, sys, io
import pandas as pd
import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, 'stockdb_csv')
IND_FILE = os.path.join(ROOT, 'cache_data', '_stock_industry.csv')

START = '2025-10-21'
END   = '2026-10-20'
SPLIT = '2026-07-01'

def limit_threshold(code6, name):
    if code6.startswith(('300','301','688','689')):
        return 19.8
    if code6.startswith(('43','83','87','920')):
        return 29.8
    if name and 'ST' in str(name).upper():
        return 4.8
    return 9.8

def load_stock(fp):
    d,o,h,lo,c,v,a,tor = [],[],[],[],[],[],[],[]
    with open(fp, 'r', encoding='gbk', errors='ignore') as f:
        lines = f.readlines()
    for ln in lines[2:]:
        p = ln.strip().split(',')
        if len(p)>=8 and p[0][:4].isdigit() and '-' in p[0]:
            d.append(p[0]); o.append(float(p[1])); h.append(float(p[2]))
            lo.append(float(p[3])); c.append(float(p[4])); v.append(float(p[5]))
            a.append(float(p[6])); tor.append(float(p[7]))
    return d,o,h,lo,c,v,a,tor

def main():
    ind = pd.read_csv(IND_FILE, dtype=str)
    ind['code6'] = ind['ts_code'].str.split('.').str[0]
    name_map = ind.set_index('code6')['name'].to_dict()
    ind_map = ind.set_index('code6')['industry'].to_dict()

    recs = []
    for fn in sorted(os.listdir(DATA_DIR)):
        if not fn.endswith('.csv'): continue
        code6 = fn.split('#')[1].split('.')[0]
        name = name_map.get(code6, '')
        industry = ind_map.get(code6, '未知') or '未知'
        if 'ST' in str(name).upper(): continue
        thr = limit_threshold(code6, name)

        fp = os.path.join(DATA_DIR, fn)
        try:
            d,o,h,lo,c,v,a,tor = load_stock(fp)
        except Exception:
            continue
        if len(d) < 25: continue

        limit_up = [False]*len(d)
        for i in range(1, len(d)):
            limit_up[i] = (c[i]/c[i-1]-1)*100 >= thr

        for i in range(1, len(d)-1):
            if not (START <= d[i] <= END): continue
            if not limit_up[i] or limit_up[i-1]: continue
            # 策略条件
            if tor[i] >= 2.0: continue
            if not ((lo[i]/c[i-1]-1)*100 >= thr): continue  # 一封到底
            avg20 = sum(v[i-20:i])/20 if i>=20 else np.nan
            if avg20 <= 0 or v[i]/avg20 >= 0.5: continue

            # 次日数据
            t1_open_pct = (o[i+1]/c[i]-1)*100
            t1_close_pct = (c[i+1]/c[i]-1)*100
            t1_high_pct = (h[i+1]/c[i]-1)*100
            t1_low_pct = (lo[i+1]/c[i]-1)*100
            is_2nd = limit_up[i+1]

            # 次日是否一字板(买不到): 开盘即涨停 且 最低价也涨停(全天没打开)
            buy_limit_price = c[i] * (1 + thr/100)
            t1_one_word = (o[i+1] >= buy_limit_price * 0.999) and \
                          (lo[i+1] >= buy_limit_price * 0.999)

            # 若能买到(开盘价<涨停价): 按开盘价买入
            can_buy = o[i+1] < buy_limit_price * 0.999
            buy_return_close = (c[i+1]/o[i+1] - 1)*100 if can_buy else np.nan
            buy_return_high = (h[i+1]/o[i+1] - 1)*100 if can_buy else np.nan

            recs.append({
                '首板日': d[i], '代码': code6, '名称': name, '题材': industry,
                '时期': '前期' if d[i] < SPLIT else '后期',
                '首板换手率%': round(tor[i],2),
                '首板量比': round(v[i]/avg20,2),
                '次日开盘%': round(t1_open_pct,2),
                '次日收盘%': round(t1_close_pct,2),
                '次日最高%': round(t1_high_pct,2),
                '次日一字板(买不到)': t1_one_word,
                '次日二板': is_2nd,
                '能买到': can_buy,
                '能买_收盘盈亏%': round(buy_return_close,2) if can_buy else None,
                '能买_最高盈亏%': round(buy_return_high,2) if can_buy else None,
            })

    df = pd.DataFrame(recs)
    if df.empty:
        print('无样本'); return

    df.to_csv(os.path.join(ROOT,'output','fb_strategy_buyability.csv'),
              index=False, encoding='utf-8-sig')

    n_total = len(df)
    n_one_word = df['次日一字板(买不到)'].sum()
    n_can_buy = df['能买到'].sum()
    n_2nd = df['次日二板'].sum()

    print(f'全年样本 (首板+换手<2%+一封到底+量比<0.5): {n_total} 只')
    print(f'次日一字板(买不到): {n_one_word} 只 ({n_one_word/n_total*100:.1f}%)')
    print(f'次日能买到(开盘未涨停): {n_can_buy} 只 ({n_can_buy/n_total*100:.1f}%)')
    print(f'次日真二板(涨停收盘): {n_2nd} 只 ({n_2nd/n_total*100:.1f}%)')

    print('\n' + '='*70)
    print('1. 买不到(一字板) vs 能买到(非一字) 的二板率对比')
    print('='*70)
    print(f'一字板(买不到): 二板率 {df[df["次日一字板(买不到)"]]["次日二板"].mean()*100:.1f}%')
    print(f'能买到(非一字): 二板率 {df[df["能买到"]]["次日二板"].mean()*100:.1f}%')
    print(f'\n→ 一字板几乎必连板(确定性最高), 但买不到; 能买到的连板率大打折扣')

    print('\n' + '='*70)
    print('2. 能买到样本的实际盈亏 (开盘买入)')
    print('='*70)
    can_buy_df = df[df['能买到']]
    if len(can_buy_df) > 0:
        print(f'可买入样本: {len(can_buy_df)} 只 ({len(can_buy_df)/n_total*100:.1f}%)')
        print(f'收盘卖出: 均值 {can_buy_df["能买_收盘盈亏%"].mean():+.2f}%  '
              f'中位 {can_buy_df["能买_收盘盈亏%"].median():+.2f}%  '
              f'胜率 {(can_buy_df["能买_收盘盈亏%"]>0).mean()*100:.1f}%')
        print(f'最高卖出: 均值 {can_buy_df["能买_最高盈亏%"].mean():+.2f}%  '
              f'中位 {can_buy_df["能买_最高盈亏%"].median():+.2f}%')

    print('\n' + '='*70)
    print('3. 按时期分组')
    print('='*70)
    for period in ['前期','后期']:
        sub = df[df['时期']==period]
        if len(sub)==0: continue
        n_ow = sub['次日一字板(买不到)'].sum()
        n_cb = sub['能买到'].sum()
        cb = sub[sub['能买到']]
        print(f'\n[{period}] n={len(sub)}')
        print(f'  一字板(买不到): {n_ow} ({n_ow/len(sub)*100:.1f}%)')
        print(f'  能买到:        {n_cb} ({n_cb/len(sub)*100:.1f}%)')
        if len(cb)>0:
            print(f'  能买到收盘盈亏: 均值{cb["能买_收盘盈亏%"].mean():+.2f}%  '
                  f'胜率{(cb["能买_收盘盈亏%"]>0).mean()*100:.1f}%')

    print('\n' + '='*70)
    print('4. 全部理论盈亏 (假设所有都能按开盘价买入, 包括一字板)')
    print('='*70)
    # 一字板的开盘买入价=涨停价, 收盘盈亏=收盘溢价-开盘溢价
    df['理论开盘买收盘盈亏%'] = df['次日收盘%'] - df['次日开盘%']
    df['理论开盘买最高盈亏%'] = df['次日最高%'] - df['次日开盘%']
    print(f'理论收盘盈亏: 均值 {df["理论开盘买收盘盈亏%"].mean():+.2f}%  '
          f'中位 {df["理论开盘买收盘盈亏%"].median():+.2f}%  '
          f'胜率 {(df["理论开盘买收盘盈亏%"]>0).mean()*100:.1f}%')
    print(f'理论最高盈亏: 均值 {df["理论开盘买最高盈亏%"].mean():+.2f}%')

    print('\n' + '='*70)
    print('5. 关键反差')
    print('='*70)
    print(f'全年理论(全部能买): 收盘盈亏均值 {df["理论开盘买收盘盈亏%"].mean():+.2f}%')
    if len(can_buy_df)>0:
        print(f'实际能买到样本:    收盘盈亏均值 {can_buy_df["能买_收盘盈亏%"].mean():+.2f}%')
    print(f'\n差额来自「选择偏差」: 一字板的标的几乎必涨停(次日收盘溢价≈开盘溢价≈+10%),')
    print(f'但实际买不到; 真正能买到的，本身就是开盘后走弱、被市场抛弃的那批。')

if __name__ == '__main__':
    main()
