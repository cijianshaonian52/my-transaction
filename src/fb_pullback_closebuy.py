# -*- coding: utf-8 -*-
"""首板回调·尾盘买入版（正式策略代码，base 出仓规则）

选股（与 _chainsens.py 双爆发链条件完全一致）:
  1. 首板涨停（当日涨幅>=9.8% 且前一日未涨停；剔除 ST、无申万二级行业映射、
     行业黑名单：化学制品/计算机应用；剔除概念黑名单）
  2. 题材双爆发: D0(首板日)所属概念涨停>=6 且 D1>=6，随后每日>=3（链不断）
  3. 概念当日热度 topK(3)（概念涨停家数排名前3且>=5）
  4. 行业一致: 概念内涨停股中与个股同申万二级行业的>=2只
  5. 回调日 T: 缩量比(信号日量/D0~T窗口最大量)<0.7 且当日非涨停；ds=T-D0<=5
  6. 同一次首板只触发一次（去重）

买卖点（base 规则）:
  买入 = 信号日 T 尾盘收盘价
  卖出 = 以下任一先触发，最多持有5个交易日:
    a) T+1 高开>=1% → 开盘价止盈
    b) 某日收盘翻红(>买入价) → 当日收盘止盈
    c) 某日收盘跌<=-3% → 当日收盘止损
    d) 5日都未触发 → T+5 收盘兜底卖出
  费用 = 双边合计 0.15%

回测窗口: 近两年（2025-01-01 起）。用法: python src/fb_pullback_closebuy.py
"""
import os, sys, io
import numpy as np, pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEE = 0.15                 # 双边费用合计 %
STOP = 3.0                 # 收盘止损阈值 %
START = '2025-01-01'       # 评估窗口（近两年）
LOAD_FROM = '2024-11-01'   # 数据加载起点（留首板判定余量）
DSMAX, TOPK = 5, 3

JUNK = ('股通', '融资', '罗素', 'MSCI', '标普', '富时', '转融', '上证', '深证', '中证', '恒生')
BLACKLIST = ('华为', '新能源', '国企改革', 'DeepSeek', '专精特新', '贬值', '预增', '股权转让',
             '并购重组', '高股息', '创投', '一带一路', '西部大开发', '粤港澳', '乡村振兴',
             '海峡两岸', '京津冀')
IND_BLACKLIST = {'化学制品', '计算机应用'}


def load_maps():
    cp = pd.read_csv(os.path.join(ROOT, 'cache_data', '_stock_concept.csv'), dtype=str)
    cp['c6'] = cp['ts_code'].str.split('.').str[0]
    cp = cp[~cp['concept'].str.contains('|'.join(JUNK), na=False)]
    cp = cp[~cp['concept'].str.contains('|'.join(BLACKLIST), na=False)]
    sw = pd.read_csv(os.path.join(ROOT, 'cache_data', '_stock_sw_industry.csv'), dtype=str)
    sw['c6'] = sw['ts_code'].str.split('.').str[0]
    cs = pd.read_csv(os.path.join(ROOT, 'cache_data', '_stock_industry.csv'), dtype=str)
    cs['c6'] = cs['ts_code'].str.split('.').str[0]
    return (cp.groupby('concept')['c6'].apply(set).to_dict(),
            cp.groupby('c6')['concept'].apply(list).to_dict(),
            sw.set_index('c6')['sw_l2'].to_dict(),
            cs.set_index('c6')['name'].to_dict())


def load_bars(m2, nm):
    codes, bars = [], {}
    for fn in os.listdir(os.path.join(ROOT, 'stockdb_csv')):
        if not fn.endswith('.csv'):
            continue
        code = fn.split('#')[1].split('.')[0]
        ind = m2.get(code)
        if not isinstance(ind, str) or not ind or ind == 'nan':
            continue
        if 'ST' in str(nm.get(code, '')).upper():
            continue
        try:
            df = pd.read_csv(os.path.join(ROOT, 'stockdb_csv', fn), encoding='gbk', skiprows=2,
                             header=None,
                             names=['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'turnover'])
        except Exception:
            continue
        df['date'] = df['date'].astype(str)
        df = df[df['date'] >= LOAD_FROM].reset_index(drop=True)
        if len(df) < 30:
            continue
        codes.append(code)
        bars[code] = df
    return codes, bars


def base_exit(o, c, t, buy, n):
    """base 出仓：高开止盈 / 收盘翻红止盈 / 收盘-3%止损 / T+5兜底
    返回 (收益率%, 卖出日偏移k, 卖出方式)"""
    for k in range(1, 6):
        j = t + k
        if j >= n:
            return round((c[n - 1] / buy - 1) * 100 - FEE, 2), k - 1, '末日兜底'
        g = o[j] / buy - 1
        if k == 1 and g >= 0.01:
            return round(g * 100 - FEE, 2), k, '高开止盈'
        rc = c[j] / buy - 1
        if rc > 0:
            return round(rc * 100 - FEE, 2), k, '翻红止盈'
        if rc <= -STOP / 100:
            return round(rc * 100 - FEE, 2), k, '止损-%g%%' % STOP
    j = min(t + 5, n - 1)
    return round((c[j] / buy - 1) * 100 - FEE, 2), j - t, 'T+5兜底'


def scan():
    concept_codes, code_concepts, m2, nm = load_maps()
    codes, bars = load_bars(m2, nm)
    alldays = sorted(set().union(*[set(df['date']) for df in bars.values()]))
    didx = {d: i for i, d in enumerate(alldays)}
    N = len(alldays)
    print('股票 %d 只, 交易日 %d 天' % (len(bars), N))

    row = {c: i for i, c in enumerate(codes)}
    LU = np.zeros((len(codes), N), dtype=bool)
    for ci, code in enumerate(codes):
        df = bars[code]
        c = df['close'].astype(float).values
        pct = np.zeros(len(c)); pct[1:] = (c[1:] / c[:-1] - 1) * 100
        for i, dd in enumerate(df['date']):
            if pct[i] >= 9.8:
                LU[ci, didx[dd]] = True

    concept_cnt = {}
    for name, members in concept_codes.items():
        rows = [row[m] for m in members if m in row]
        if len(rows) >= 5:
            concept_cnt[name] = LU[rows].sum(axis=0)
    cnames = list(concept_cnt)
    CM = np.vstack([concept_cnt[x] for x in cnames])

    topk = []
    for gi in range(N):
        col = CM[:, gi]
        idx = np.argsort(-col)[:TOPK]
        topk.append(set(cnames[j] for j in idx if col[j] >= 5))

    def ind_ok(concept_name, gi, stock_ind):
        if not stock_ind:
            return False
        rows_c = [row[m] for m in concept_codes[concept_name] if m in row]
        hot = [codes[r] for r in rows_c if LU[r, gi]]
        return [m2.get(c) for c in hot if c in m2].count(stock_ind) >= 2

    S = []
    last_buy = {}
    for code in sorted(bars):
        df = bars[code]
        d = df['date'].values
        c = df['close'].astype(float).values
        o = df['open'].astype(float).values
        v = df['volume'].astype(float).values
        n = len(c)
        pct = np.zeros(n); pct[1:] = (c[1:] / c[:-1] - 1) * 100
        lu = pct >= 9.8
        first = np.zeros(n, bool); first[1:] = lu[1:] & ~lu[:-1]
        cons = [x for x in code_concepts.get(code, []) if x in concept_cnt]
        if not cons:
            continue
        ind = m2.get(code, '')
        if ind in IND_BLACKLIST:
            continue
        for i in range(1, n - 1):
            if not first[i]:
                continue
            gi0 = didx[d[i]]
            hot0 = [x for x in cons if x in topk[gi0] and concept_cnt[x][gi0] >= 6
                    and ind_ok(x, gi0, ind)]
            if not hot0:
                continue
            gi1 = didx[d[i + 1]] if i + 1 < n else -1
            hot0 = [x for x in hot0 if gi1 >= 0 and concept_cnt[x][gi1] >= 6]
            if not hot0:
                continue
            for t in range(i + 2, min(i + DSMAX + 1, n)):
                chain_ok = False
                for x in hot0:
                    ok = True
                    for k in range(i + 2, t + 1):
                        if concept_cnt[x][didx[d[k]]] < 3:
                            ok = False
                            break
                    if ok:
                        chain_ok = True
                        break
                if not chain_ok:
                    break
                wv = np.max(v[i:t + 1])
                if wv <= 0:
                    continue
                vsh = v[t] / wv
                if vsh >= 0.7 or abs(pct[t]) >= 9.5:
                    continue
                if last_buy.get(code, -1) == i:
                    continue
                if d[t] < START:
                    last_buy[code] = i
                    continue
                buy = c[t]                                # T 尾盘买
                ret, days, how = base_exit(o, c, t, buy, n)
                sell_j = min(t + days, n - 1)
                S.append({'T': d[t], 'code': code, 'name': nm.get(code, ''), 'ind': ind,
                          'ds': t - i, 'vshrink': round(vsh, 3), 'pct': round(pct[t], 2),
                          'buy': round(float(buy), 2), 'sell_date': d[sell_j],
                          'exit': how, 'days': days, 'ret': ret})
                last_buy[code] = i
    return pd.DataFrame(S)


def rpt(g, tag):
    r = g['ret'].astype(float)
    print('  %-14s n=%4d 均%+6.2f%% 中%+6.2f%% 胜%5.1f%%' %
          (tag, len(r), r.mean(), r.median(), (r > 0).mean() * 100))


def main():
    S = scan()
    out = os.path.join(ROOT, 'output', 'fb_pullback_closebuy_signals.csv')
    S.to_csv(out, index=False, encoding='utf-8-sig')
    print('\n信号 %d 笔（%s 起），已存 %s' % (len(S), START, out))

    print('\n== 尾盘买 + base出仓 逐笔统计 ==')
    rpt(S, '近两年')
    for y, g in S.groupby(S['T'].str[:4]):
        rpt(g, y)

    print('\n== 卖出方式分布 ==')
    for how, g in S.groupby('exit'):
        rpt(g, how)

    print('\n== 逐月 ==')
    for ym, g in S.groupby(S['T'].str[:7]):
        rpt(g, ym)

    print('\n== 最近一个月交割单 ==')
    last_month = S['T'].str[:7].max()
    O = S[S['T'].str[:7] == last_month].sort_values(['T', 'ret'])
    for _, r in O.iterrows():
        print('  %s %s %-6s ds=%d vsh=%.2f 买%.2f→%s(%s) 盈亏%+6.2f%%'
              % (r['T'], r['code'], r['name'], r['ds'], r['vshrink'], r['buy'],
                 r['sell_date'][5:], r['exit'], r['ret']))
    rpt(O, last_month)


if __name__ == '__main__':
    main()
