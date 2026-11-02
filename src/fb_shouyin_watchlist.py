# -*- coding: utf-8 -*-
"""首阴观察池扫描器（龙头断板反包战法）

断板定义: 连续涨停>=2板后, 某日不再涨停（断板日）。
逐条检查6条件（规则源自龙头首阴战法）:
  C1 江湖地位: 断板前连板数>=5 且 >= 当日全市场最高连板-1（至少5板才配叫龙头）
  C2 良性首阴: 断板日不跌停(即非天地板)
  C3 不破底线: 断板日收盘价 >= 最后一板当天的最低价
  C4 换手非出货: 断板日量/断板前10日均量 in [1.5, 3.5]，且非近250日最大量(历史天量=出货)
  C5 板块撑场: 断板日所属概念涨停家数>=3 或 同行业有涨停(低位小弟撑场)
  C6 次日弱转强(若次日已发生): 次日开盘>=断板收盘*0.97 且 次日最低>=断板最低*0.99
输出: output/shouyin_watchlist.csv + 控制台逐票检查表
用法: python src/fb_shouyin_watchlist.py [回溯天数=5]
"""
import os, sys, io
import numpy as np, pandas as pd
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOKBACK = int(sys.argv[1]) if len(sys.argv) > 1 else 5   # 扫描最近N个交易日内的断板
HIST = 260                                                 # 每只股票加载的历史天数

JUNK = ('股通', '融资', '罗素', 'MSCI', '标普', '富时', '转融', '上证', '深证', '中证', '恒生')
BLACKLIST = ('华为', '新能源', '国企改革', 'DeepSeek', '专精特新', '贬值', '预增', '股权转让',
             '并购重组', '高股息', '创投', '一带一路', '西部大开发', '粤港澳', '乡村振兴',
             '海峡两岸', '京津冀')


def load_index_ret():
    """读取基准指数日线, 返回 {code前缀: DataFrame(date->close)} 及按日期的涨跌幅"""
    p = os.path.join(ROOT, 'cache_data', '_index_daily.csv')
    if not os.path.exists(p):
        return {}
    df = pd.read_csv(p, dtype={'date': str})
    out = {}
    for idx, grp in df.groupby('index'):
        grp = grp.sort_values('date').reset_index(drop=True)
        grp['pct'] = grp['close'].pct_change() * 100
        out[idx] = grp.set_index('date')
    return out


def idx_of(code):
    return 'sh.000001' if code.startswith('6') else 'sz.399001'


def index_ret(idmap, code, d, n):
    """指数近n日累计涨幅(截止日d)。d 为日期字符串, n 为交易日数"""
    g = idmap.get(idx_of(code))
    if g is None or d not in g.index:
        return 0.0
    j = g.index.get_loc(d)
    if j < n:
        return 0.0
    base = g['close'].iloc[j - n]
    return (g['close'].iloc[j] / base - 1) * 100 if base > 0 else 0.0


def load_maps():
    sw = pd.read_csv(os.path.join(ROOT, 'cache_data', '_stock_sw_industry.csv'), dtype=str)
    sw['c6'] = sw['ts_code'].str.split('.').str[0]
    cs = pd.read_csv(os.path.join(ROOT, 'cache_data', '_stock_industry.csv'), dtype=str)
    cs['c6'] = cs['ts_code'].str.split('.').str[0]
    cp = pd.read_csv(os.path.join(ROOT, 'cache_data', '_stock_concept.csv'), dtype=str)
    cp['c6'] = cp['ts_code'].str.split('.').str[0]
    cp = cp[~cp['concept'].str.contains('|'.join(JUNK), na=False)]
    cp = cp[~cp['concept'].str.contains('|'.join(BLACKLIST), na=False)]
    return (sw.set_index('c6')['sw_l2'].to_dict(),
            cs.set_index('c6')['name'].to_dict(),
            cp.groupby('c6')['concept'].apply(list).to_dict(),
            cp.groupby('concept')['c6'].apply(set).to_dict())


WLIST = os.path.join(ROOT, 'desktop_widget', 'watchlist.txt')
MARK_B = '# [首阴观察池]'
MARK_E = '# [/首阴观察池]'


def update_widget_watchlist(pool):
    """把观察池写入桌面组件的 watchlist.txt 托管区块(不动用户手动添加的行)"""
    try:
        old = open(WLIST, encoding='utf-8').read().splitlines()
    except Exception:
        old = []
    out, inb = [], False
    for ln in old:
        if ln.strip() == MARK_B:
            inb = True
            continue
        if ln.strip() == MARK_E:
            inb = False
            continue
        if not inb:
            out.append(ln)
    block = [MARK_B, '# 由 fb_shouyin_watchlist.py 自动维护, 手动自选股写在此区块外']
    for _, r in pool.iterrows():
        qc = ('sh' if r['code'][0] == '6' else 'sz') + r['code']
        block.append(qc)
        block.append('#   %s %s %d板断板 止损线%.2f'
                     % (r['断板日'], r['name'], r['断板前连板'], r['止损线(断板日最低)']))
    block.append(MARK_E)
    os.makedirs(os.path.dirname(WLIST), exist_ok=True)
    open(WLIST, 'w', encoding='utf-8').write('\n'.join(out + block) + '\n')


IDX = {}


def main():
    global IDX
    IDX = load_index_ret()
    m2, nm, c2m, con2mem = load_maps()
    bars = {}
    alldays = set()
    for fn in os.listdir(os.path.join(ROOT, 'stockdb_csv')):
        if not fn.endswith('.csv'):
            continue
        code = fn.split('#')[1].split('.')[0]
        if code[:2] in ('30', '68'):      # 剔除创业板/科创板
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
        df = df.tail(HIST).reset_index(drop=True)
        if len(df) < 30:
            continue
        bars[code] = df
        alldays |= set(df['date'])
    days = sorted(alldays)
    scan_days = days[-LOOKBACK:]
    print('股票 %d 只; 扫描断板日: %s' % (len(bars), ', '.join(scan_days := scan_days if False else scan_days)))

    # 预计算每只股票的涨停序列与连板数（向量化到全局日历）
    didx = {d: i for i, d in enumerate(days)}
    LU = {}
    for code, df in bars.items():
        c = df['close'].astype(float).values
        pct = np.zeros(len(c)); pct[1:] = (c[1:] / c[:-1] - 1) * 100
        lim = 19.8 if code[:2] in ('30', '68') else 9.8
        lu = pct >= lim
        LU[code] = {didx[d]: lu[i] for i, d in enumerate(df['date']) if d in didx}

    # 每日全市场最高连板数 & 每只票每日连板数
    def streak(code, gi):
        s = 0
        while gi - s >= 0 and LU[code].get(gi - s, False):
            s += 1
        return s

    ind2codes = defaultdict(list)
    for c in bars:
        ind2codes[m2.get(c, '')].append(c)

    rows = []
    for gd in scan_days:
        gi = didx[gd]
        if gi < 2:
            continue
        # 昨日(断板前一日)连板梯队
        prev_lu = {c: streak(c, gi - 1) for c in bars if LU[c].get(gi - 1, False)}
        if not prev_lu:
            continue
        mkt_max = max(prev_lu.values())
        # 当日各概念涨停家数（C5）
        lu_today = [c for c in bars if LU[c].get(gi, False)]
        concept_lu = defaultdict(int)
        for c in lu_today:
            for x in c2m.get(c, []):
                concept_lu[x] += 1

        for code, stk in prev_lu.items():
            if LU[code].get(gi, False):
                continue                       # 还在板上，不是断板
            df = bars[code]
            j = df.index[df['date'] == gd]
            if len(j) == 0:
                continue
            j = j[0]
            o, h, l, cl = (float(df.loc[j, k]) for k in ('open', 'high', 'low', 'close'))
            v = float(df.loc[j, 'volume'])
            jl = j - 1                          # 最后一板
            low_last_board = float(df.loc[jl, 'low'])
            pct_d = (float(df.loc[j, 'close']) / float(df.loc[jl, 'close']) - 1) * 100
            lim = 19.8 if code[:2] in ('30', '68') else 9.8

            c1 = stk >= 5 and stk >= mkt_max - 1   # 至少5板才配叫龙头
            c2 = pct_d > -lim + 0.3                      # 不跌停即可(不天地板已被包含)
            c3 = float(cl) >= low_last_board
            v10 = df.loc[max(0, j - 10):j - 1, 'volume'].astype(float).mean()
            v250 = df.loc[max(0, j - 250):j - 1, 'volume'].astype(float).max()
            vratio = v / v10 if v10 > 0 else 99
            c4 = 1.5 <= vratio <= 3.5 and v < v250
            ind = m2.get(code, '')
            ind_lu = sum(1 for cc in ind2codes.get(ind, ()) if LU.get(cc, {}).get(gi, False))
            c5 = max([concept_lu.get(x, 0) for x in c2m.get(code, [])] or [0]) >= 3 or ind_lu >= 1
            # C6 次日弱转强
            c6 = None
            if j + 1 < len(df):
                no, nl = float(df.loc[j + 1, 'open']), float(df.loc[j + 1, 'low'])
                c6 = no >= float(cl) * 0.97 and nl >= l * 0.99

            # 监管风险按【最新交易日】实时计算，偏离值=个股涨幅-指数涨幅
            lj = len(df) - 1                       # 最新一日
            lc = float(df.loc[lj, 'close'])
            abn = 0
            for i in range(max(3, lj - 10), lj + 1):
                cq3 = float(df.loc[i - 3, 'close'])
                if cq3 <= 0:
                    continue
                sdev = (float(df.loc[i, 'close']) / cq3 - 1) * 100 - index_ret(IDX, code, str(df['date'].iloc[i]), 3)
                if sdev >= 19.9:
                    abn += 1
            cq3t = float(df.loc[lj - 3, 'close']) if lj >= 3 else 0
            ld = str(df['date'].iloc[lj])
            ret3 = ((lc / cq3t - 1) * 100 if cq3t > 0 else 0) - index_ret(IDX, code, ld, 3)
            gap_abn = (20 - ret3) / 120 * 100                 # >0: 距异动还差%; <=0: 已触发
            base10 = float(df.loc[max(0, lj - 10), 'close'])
            ret10 = ((lc / base10 - 1) * 100 if base10 > 0 else 0) - index_ret(IDX, code, ld, 10)
            gap100 = 100 - ret10                              # >0: 距停牌线还剩%; <=0: 已触发

            latest_pct = (float(df['close'].iloc[-1]) / float(df['close'].iloc[-2]) - 1) * 100

            rows.append({
                '断板日': gd, 'code': code, 'name': nm.get(code, ''), 'ind': m2.get(code, ''),
                '断板前连板': stk, '当日市场最高板': mkt_max,
                '断板日涨跌%': round(pct_d, 2), '量比(10日)': round(vratio, 1),
                '最新日': str(df['date'].iloc[-1]), '最新日涨跌%': round(latest_pct, 2),
                '近10日异动次数': abn, '三日累计涨幅%': round(ret3, 1),
                '距异动%': round(gap_abn, 1), '距停牌线%': round(gap100, 1),
                '是否历史天量': v >= v250,
                'C1地位': c1, 'C2良性': bool(c2), 'C3不破底': c3, 'C4换手': c4,
                'C5板块': c5, 'C6次日弱转强': c6,
                '断板收盘': float(cl), '断板最低': l, '止损线(断板日最低)': l,
                '通过数': sum([c1, bool(c2), c3, c4, c5]) + (1 if c6 else 0),
            })

    R = pd.DataFrame(rows)
    if R.empty:
        print('最近 %d 天无断板事件' % LOOKBACK)
        return
    R = R[R['C1地位']]            # 只看够格的龙头(>=5板)
    if R.empty:
        print('最近 %d 天无龙头(>=5板)断板事件' % LOOKBACK)
        update_widget_watchlist(pd.DataFrame())
        return
    R = R.sort_values(['断板日', '断板前连板'], ascending=[False, False])
    out = os.path.join(ROOT, 'output', 'shouyin_watchlist.csv')
    R.to_csv(out, index=False, encoding='utf-8-sig')

    for _, r in R.iterrows():
        marks = ''.join('✓' if r[k] else ('—' if k == 'C6次日弱转强' and r[k] is None else '✗')
                        for k in ('C1地位', 'C2良性', 'C3不破底', 'C4换手', 'C5板块', 'C6次日弱转强'))
        c6txt = {True: '弱转强✓', False: '走弱✗', None: '待明日竞价'}.get(r['C6次日弱转强'], str(r['C6次日弱转强']))
        print('%s %s %-6s %2d板断板(市场最高%d板) 断板涨跌%+6.2f%% 最新%s %+5.2f%% 量比%.1f%s | C1-C6 %s | %s | 异动%d次 距异动%s 距停牌线%+.1f%%'
              % (r['断板日'], r['code'], r['name'], r['断板前连板'], r['当日市场最高板'],
                 r['断板日涨跌%'], r['最新日'][5:], r['最新日涨跌%'], r['量比(10日)'], ' 历史天量!' if r['是否历史天量'] else '',
                 marks, c6txt, r['近10日异动次数'],
                 ('已触发%+.1f%%' % r['距异动%']) if r['距异动%'] <= 0 else ('+%.1f%%' % r['距异动%']),
                 r['距停牌线%']))

    # 进行中: 最新交易日仍在连板的 >=5板 龙头(未断板)
    glast = len(days) - 1
    live = []
    for c in bars:
        s = streak(c, glast)
        if s < 5:
            continue
        df = bars[c]
        j = len(df) - 1
        cl = float(df.loc[j, 'close'])
        abn = 0
        for i in range(max(3, j - 10), j + 1):
            cq3 = float(df.loc[i - 3, 'close'])
            if cq3 <= 0:
                continue
            sdev = (float(df.loc[i, 'close']) / cq3 - 1) * 100 - index_ret(IDX, c, str(df['date'].iloc[i]), 3)
            if sdev >= 19.9:
                abn += 1
        ld = str(df['date'].iloc[j])
        cq3t = float(df.loc[j - 3, 'close']) if j >= 3 else 0
        ret3 = ((cl / cq3t - 1) * 100 if cq3t > 0 else 0) - index_ret(IDX, c, ld, 3)
        gap_abn = max(0.0, (20 - ret3) / 120 * 100)
        base10 = float(df.loc[max(0, j - 10), 'close'])
        gap100 = 100 - (((cl / base10 - 1) * 100 if base10 > 0 else 0) - index_ret(IDX, c, ld, 10))
        daypct = (cl / float(df.loc[j - 1, 'close']) - 1) * 100 if j >= 1 else 0
        live.append((s, c, nm.get(c, ''), daypct, abn, gap_abn, gap100))
    live.sort(key=lambda x: -x[0])
    print('\n进行中(未断板) ≥5板龙头 %d 只:' % len(live))
    for s, c, n, dp, abn, ga, g1 in live:
        print('  %s %s %-6s %d板  当日%+5.2f%%  异动%d次 距异动%s 距停牌线%+.1f%%'
              % (days[-1], c, n, s, dp, abn,
                 ('已触发%+.1f%%' % ga) if ga <= 0 else ('+%.1f%%' % ga), g1))
    print('\n已存 %s' % out)
    pool = R[(R['C1地位']) & (R['C2良性']) & (R['C3不破底']) & (R['C4换手']) & (R['C5板块'])]
    update_widget_watchlist(pool)
    print('通过 C1~C5 进观察池: %d 只 (已同步到 desktop_widget/watchlist.txt)' % len(pool))
    for _, r in pool.iterrows():
        print('  ★ %s %s %s %d板断板, 止损线 %.2f' % (r['断板日'], r['code'], r['name'], r['断板前连板'], r['止损线(断板日最低)']))


if __name__ == '__main__':
    main()
