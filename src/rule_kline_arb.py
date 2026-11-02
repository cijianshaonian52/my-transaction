# -*- coding: utf-8 -*-
"""用户K线形态次日套利策略 全市场实证(stockdb_csv 全市场, 前复权)。

形态1(光脚阴线): open < prev_open 且 close < open(阴线) 且 low == close(无下影线)
形态2(高开抗跌): open > prev_open 且 close >= prev_close
共同: volume <= 2 * prev_volume
[注] 换手率>10% 本地无流通股本数据, 未执行(见报告备注)。

买入: 信号日收盘价(cheat-on-close口径, 即尾盘确认形态后买入)。
退出: 次日开盘 / 次日收盘 / 次日盘中最高(上限)。
费用: 买万五 + 卖万五 + 印花万五 ≈ 双边0.15%。
"""
import os
import sys
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKDB_DIR = os.path.join(BASE, 'stockdb_csv')
OUT = os.path.join(BASE, 'output')

START = pd.Timestamp(sys.argv[1]) if len(sys.argv) > 1 else pd.Timestamp('2021-08-01')
EPS = 0.001          # 无下影线容差: low >= close*(1-0.1%)
FEE = 0.0015         # 双边费用


def load(fp):
    df = pd.read_csv(fp, encoding='gbk', skiprows=2, header=None,
                     names=['date', 'open', 'high', 'low', 'close',
                            'volume', 'amount', 'turnover'])
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date'])
    return df[df['date'] >= START - pd.Timedelta(days=10)].reset_index(drop=True)


def scan():
    events = []
    files = [f for f in os.listdir(STOCKDB_DIR) if f.endswith('.csv')]
    for k, fn in enumerate(files):
        code6 = fn.split('#')[1][:6]
        try:
            df = load(os.path.join(STOCKDB_DIR, fn))
        except Exception:
            continue
        if len(df) < 30:
            continue
        o = df['open'].values
        h = df['high'].values
        lo = df['low'].values
        c = df['close'].values
        v = df['volume'].values
        dt = df['date'].values
        po = np.roll(o, 1)
        pc = np.roll(c, 1)
        pv = np.roll(v, 1)
        # 形态1: 光脚阴线 (open<昨开, 收阴, low==close)
        m1 = (o < po) & (c < o) & (lo >= c * (1 - EPS))
        # 形态2: 高开抗跌 (open>昨开, close>=昨收)
        m2 = (o > po) & (c >= pc)
        vol_ok = (v <= 2.0 * pv) & (pv > 0)
        sig1 = m1 & vol_ok
        sig2 = m2 & vol_ok
        n = len(df)
        for i in np.where((sig1 | sig2) & (dt >= START))[0]:
            if i == 0 or i + 1 >= n:
                continue
            events.append({
                'date': dt[i], 'code': code6,
                'type': 1 if sig1[i] else 2,
                'close': c[i],
                'chg%': (c[i] / pc[i] - 1) * 100,
                'ret_o': (o[i + 1] / c[i] - 1) * 100,
                'ret_c': (c[i + 1] / c[i] - 1) * 100,
                'ret_h': (h[i + 1] / c[i] - 1) * 100,
                'ret_h5': (h[i + 1] / c[i] - 1) * 100,  # 占位
            })
    return pd.DataFrame(events)


def stat(sub, tag):
    if len(sub) == 0:
        print(f'{tag}: 无样本')
        return
    net_c = sub['ret_c'] - FEE * 100
    net_o = sub['ret_o'] - FEE * 100
    print(f'--- {tag} (n={len(sub)}) ---')
    print(f'  次日开盘卖: 中位{sub["ret_o"].median():+.2f}% 均值{sub["ret_o"].mean():+.2f}% '
          f'胜率{(sub["ret_o"] > 0).mean() * 100:.1f}% 扣费均值{net_o.mean():+.2f}%')
    print(f'  次日收盘卖: 中位{sub["ret_c"].median():+.2f}% 均值{sub["ret_c"].mean():+.2f}% '
          f'胜率{(sub["ret_c"] > 0).mean() * 100:.1f}% 扣费均值{net_c.mean():+.2f}%')
    print(f'  盘中最高:   中位{sub["ret_h"].median():+.2f}% '
          f'冲高>0.15%(覆盖费用)率{(sub["ret_h"] > FEE * 100).mean() * 100:.1f}% '
          f'冲高>1%率{(sub["ret_h"] > 1).mean() * 100:.1f}%')


def main():
    ev = scan()
    ev['date'] = pd.to_datetime(ev['date'])
    ev.to_csv(os.path.join(OUT, 'kline_arb_20261021.csv'),
              index=False, encoding='utf-8-sig')
    print(f'窗口: {START.date()} ~ 2026-10-20  总信号: {len(ev)}')
    print(f'日均信号: {len(ev) / ev["date"].nunique():.1f} 只/交易日\n')

    stat(ev, '全体')
    print()
    stat(ev[ev['type'] == 1], '形态1 光脚阴线')
    print()
    stat(ev[ev['type'] == 2], '形态2 高开抗跌')
    print()

    # 按信号日涨跌幅分档
    ev['chg档'] = pd.cut(ev['chg%'], [-99, -5, -2, 0, 2, 5, 99],
                     labels=['<-5%', '-5~-2%', '-2~0%', '0~2%', '2~5%', '>5%'])
    print('=== 按信号日涨跌幅 × 形态 ===')
    for t in [1, 2]:
        print(f'[形态{t}] 次日收盘卖(扣费后期望=均值-0.15%):')
        g = ev[ev['type'] == t].groupby('chg档', observed=True)
        tab = pd.DataFrame({
            'n': g.size(),
            '收盘卖_中位%': g['ret_c'].median().round(2),
            '收盘卖_均值%': g['ret_c'].mean().round(2),
            '胜率%': (g['ret_c'].apply(lambda s: (s > 0).mean() * 100)).round(1),
            '冲高>1%率%': (g['ret_h'].apply(lambda s: (s > 1).mean() * 100)).round(1),
        })
        print(tab.to_string())
        print()

    # 分年
    ev['year'] = ev['date'].dt.year
    print('=== 分年(次日收盘卖) ===')
    for t in [1, 2]:
        g = ev[ev['type'] == t].groupby('year')
        tab = pd.DataFrame({
            'n': g.size(),
            '均值%': g['ret_c'].mean().round(2),
            '中位%': g['ret_c'].median().round(2),
            '胜率%': (g['ret_c'].apply(lambda s: (s > 0).mean() * 100)).round(1),
        })
        print(f'[形态{t}]')
        print(tab.to_string())
        print()


if __name__ == '__main__':
    main()
