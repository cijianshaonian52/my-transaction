# -*- coding: utf-8 -*-
"""首板接力策略最近一个月盈亏回测
策略条件: 首板 + 换手率<2% + 一封到底 + 量比<0.5
买入点: 次日开盘价 (竞价接力)
卖出点: 次日收盘价 / 次日最高价 / 次日开盘价(开盘即卖)

口径同 first_board_premium.py
"""
import os, sys, io
import pandas as pd
import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, 'stockdb_csv')
IND_FILE = os.path.join(ROOT, 'cache_data', '_stock_industry.csv')

START = '2026-09-20'
END   = '2026-10-20'

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
            if avg20 <= 0 or v[i]/avg20 >= 0.5: continue  # 量比<0.5

            buy_price = o[i+1]
            if buy_price <= 0 or np.isnan(buy_price): continue

            recs.append({
                '首板日': d[i], '代码': code6, '名称': name, '题材': industry,
                '首板收盘价': c[i], '次日开盘价': buy_price,
                '次日收盘价': c[i+1], '次日最高价': h[i+1], '次日最低价': lo[i+1],
                '首板换手率%': round(tor[i],2),
                '首板量比': round(v[i]/avg20,2),
                '一封到底': '是',
                '开盘溢价%': round((o[i+1]/c[i]-1)*100,2),
                '收盘溢价%': round((c[i+1]/c[i]-1)*100,2),
                '最高溢价%': round((h[i+1]/c[i]-1)*100,2),
                '最低溢价%': round((lo[i+1]/c[i]-1)*100,2),
                '次日涨停': '是' if (c[i+1]/c[i]-1)*100 >= thr else '否',
                '次日开盘买收盘卖%': round((c[i+1]/buy_price-1)*100,2),
                '次日开盘买最高卖%': round((h[i+1]/buy_price-1)*100,2),
                '次日开盘买开盘卖%': 0.0,  # 同价
            })

    df = pd.DataFrame(recs)
    if df.empty:
        print('最近一个月无符合条件的标的'); return

    df = df.sort_values('首板日').reset_index(drop=True)

    print(f'最近一个月 ({START} ~ {END}) 符合「首板+换手<2%+一封到底+量比<0.5」的标的: {len(df)} 只')
    print(f'涉及交易日: {df["首板日"].nunique()} 天')
    print('\n' + '='*100)
    print(f'{"日期":<12}{"代码":<8}{"名称":<10}{"题材":<18}{"换手":>6}{"量比":>6}{"开盘溢价":>8}{"收盘溢价":>8}{"最高溢价":>8}{"次日涨停":>8}{"开盘买收盘卖":>12}')
    print('='*100)
    for _, r in df.iterrows():
        print(f'{r["首板日"]:<12}{r["代码"]:<8}{r["名称"]:<10}{r["题材"]:<18}'
              f'{r["首板换手率%"]:>5.1f}%{r["首板量比"]:>6.2f}'
              f'{r["开盘溢价%"]:>7.2f}%{r["收盘溢价%"]:>7.2f}%{r["最高溢价%"]:>7.2f}%'
              f'{r["次日涨停"]:>8}{r["次日开盘买收盘卖%"]:>10.2f}%')

    print('\n' + '='*60)
    print('汇总统计 (次日开盘买入)')
    print('='*60)
    print(f'样本数: {len(df)}')
    print(f'次日开盘溢价: 均值 {df["开盘溢价%"].mean():.2f}%  中位 {df["开盘溢价%"].median():.2f}%')
    print(f'次日收盘溢价(开盘买→收盘卖): 均值 {df["次日开盘买收盘卖%"].mean():.2f}%  中位 {df["次日开盘买收盘卖%"].median():.2f}%  胜率 {(df["次日开盘买收盘卖%"]>0).mean()*100:.1f}%')
    print(f'次日最高溢价(开盘买→最高卖): 均值 {df["次日开盘买最高卖%"].mean():.2f}%  中位 {df["次日开盘买最高卖%"].median():.2f}%')
    print(f'次日再涨停率: {(df["次日涨停"]=="是").mean()*100:.1f}%')

    # 按日期汇总
    print('\n--- 按首板日汇总 ---')
    gd = df.groupby('首板日').agg(
        n=('代码','count'),
        收盘均=('次日开盘买收盘卖%','mean'),
        收盘中=('次日开盘买收盘卖%','median'),
        涨停率=('次日涨停',lambda x: (x=='是').mean()*100),
    ).round(2)
    for ix, r in gd.iterrows():
        print(f'{ix}  {r["n"]:>2}只  收盘均{r["收盘均"]:>6.2f}%  中位{r["收盘中"]:>6.2f}%  涨停率{r["涨停率"]:>5.1f}%')

    out = os.path.join(ROOT, 'output', 'fb_strategy_recent_month.csv')
    df.to_csv(out, index=False, encoding='utf-8-sig')
    print(f'\n明细已保存: {out}')

if __name__ == '__main__':
    main()
