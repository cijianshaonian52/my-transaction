# -*- coding: utf-8 -*-
"""同行业多股涨停时：连板高度 与 行业涨幅/市值增长 的关系

口径:
  - 数据: stockdb_csv/ 前复权日线(2021-08 ~ 最新) + cache_data/_stock_industry.csv(CSRC行业)
  - 涨停: 主板9.8 / 双创19.8 / 北交所29.8 / ST4.8 (0.2pct容差), 上市前5日不计
  - 连板高度: 截至当日连续涨停天数
  - 流通市值(亿元) = 成交额(元) / (换手率% / 100) / 1e8   (换手率来自 output/_turn_bs.csv)
  - 行业日度聚合: 只统计当日有成交的行业内个股
      ind_ret_ew  行业等权涨幅%
      ind_ret_cw  行业流通市值加权涨幅%
      ind_ret_ex  剔除当日涨停股后的等权涨幅%(跟风强度)
      lu_mv_gain  涨停股合计流通市值增量(亿元)
      ind_mv_gain 行业合计流通市值增量(亿元)
      ind_mv_ex   剔除涨停股后的行业市值增量(亿元)
  - 样本: 行业当日涨停家数 >= 3
输出: output/industry_lianban_mv_YYYYMMDD.csv + 控制台报告
"""
import sys, io, os, glob, datetime, re, math
import numpy as np
import pandas as pd

def spearmanr(x, y):
    """秩相关（跳过NaN），返回 (rho, 近似p值)"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    n = len(x)
    if n < 3:
        return np.nan, np.nan
    rx = pd.Series(x).rank().to_numpy(dtype=float, copy=True)
    ry = pd.Series(y).rank().to_numpy(dtype=float, copy=True)
    rx -= rx.mean()
    ry -= ry.mean()
    den = np.sqrt((rx ** 2).sum() * (ry ** 2).sum())
    rho = float((rx * ry).sum() / den) if den > 0 else np.nan
    if not np.isfinite(rho) or abs(rho) >= 1:
        return rho, 0.0
    t = rho * np.sqrt((n - 2) / (1 - rho ** 2))
    # 正态近似（n 较大时足够）
    p = float(2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))))
    return rho, p


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKDB_DIR = os.path.join(ROOT, 'stockdb_csv')
IND_FILE = os.path.join(ROOT, 'cache_data', '_stock_industry.csv')
TURN_FILE = os.path.join(ROOT, 'output', '_turn_bs.csv')
OUT = os.path.join(ROOT, 'output')
TODAY = datetime.date.today().strftime('%Y%m%d')
OUT_CSV = os.path.join(OUT, f'industry_lianban_mv_{TODAY}.csv')


def limit_threshold(code6, name):
    if code6.startswith(('300', '301', '688', '689')):
        return 19.8
    if code6.startswith(('43', '83', '87', '920')):
        return 29.8
    if name and 'ST' in str(name).upper():
        return 4.8
    return 9.8


def load_industry():
    """code6 -> (name, industry)"""
    df = pd.read_csv(IND_FILE, dtype=str).fillna('')
    mp = {}
    for _, r in df.iterrows():
        code6 = str(r['ts_code']).split('.')[0]
        ind = r['industry'].strip() or '未分类'
        mp[code6] = (r['name'].strip(), ind)
    return mp


def load_turn():
    """code6 -> (dates ndarray, turn ndarray)"""
    t = pd.read_csv(TURN_FILE, dtype={'code': str})
    t['code6'] = t['code'].str.split('.').str[0]
    t['turn'] = pd.to_numeric(t['turn'], errors='coerce')
    t['d'] = pd.to_datetime(t['date'], errors='coerce')
    t = t.dropna(subset=['d', 'turn'])
    out = {}
    for c, g in t.groupby('code6'):
        out[c] = (g['d'].values, g['turn'].values.astype(float))
    return out


def stock_frame(fp, code6, name, ind, turn_map):
    """单票日度明细（含市值、涨停、连板高度）"""
    with open(fp, 'r', encoding='gbk', errors='ignore') as f:
        first = f.readline().strip()
        if not name:
            parts = first.split()
            name = parts[1] if len(parts) > 1 else ''
    df = pd.read_csv(fp, encoding='gbk', skiprows=2, header=None,
                     names=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
    df['date'] = pd.to_datetime(df['date'].astype(str), errors='coerce')
    df = df.dropna(subset=['date']).reset_index(drop=True)
    if len(df) < 30:
        return None
    c = df['close'].astype(float)
    a = df['amount'].astype(float)
    pct = (c / c.shift(1) - 1) * 100
    thr = limit_threshold(code6, name)
    lu = ((pct >= thr) & (np.arange(len(df)) >= 5)).fillna(False).values
    grp = (~lu).cumsum()
    lbc = pd.Series(lu).groupby(grp).cumsum().values

    # 流通市值(亿元): 成交额 / 换手率
    if code6 in turn_map:
        td, tv = turn_map[code6]
        ts = pd.Series(tv, index=pd.DatetimeIndex(td))
        turn = ts.reindex(df['date']).values
    else:
        turn = np.full(len(df), np.nan)
    with np.errstate(divide='ignore', invalid='ignore'):
        cap = a.values / (turn / 100.0) / 1e8
    cap = np.where((turn > 0), cap, np.nan)
    cap_prev = np.r_[np.nan, cap[:-1]]

    return pd.DataFrame({
        'date': df['date'].values,
        'pct': pct.values,
        'cap': cap,
        'cap_prev': cap_prev,
        'lu': lu,
        'lbc': lbc,
    })


def aggregate_industry(g, ind):
    """行业 → 日度面板"""
    ok = g['pct'].notna()
    g = g[ok]
    lu = g['lu'].values
    pct = g['pct'].values
    cap = g['cap'].values
    cp = g['cap_prev'].values
    lbc = g['lbc'].values

    def s(mask, arr):
        return np.nansum(arr[mask])

    df = pd.DataFrame({
        'date': g['date'].values,
        'pct': pct, 'cap': cap, 'cap_prev': cp,
        'lu': lu.astype(float), 'lbc': lbc,
        'capw': cp * pct,          # 昨市值加权涨幅
    })
    # 全部个股
    allg = df.groupby('date').agg(
        cnt=('pct', 'size'),
        sum_pct=('pct', 'sum'),
        sum_capw=('capw', 'sum'),
        cap_now=('cap', 'sum'),
        cap_prev=('cap_prev', 'sum'),
    )
    # 涨停股
    lug = df[df['lu'] > 0].groupby('date').agg(
        n_lu=('pct', 'size'),
        lbc_sum=('lbc', 'sum'),
        lbc_max=('lbc', 'max'),
        lbc_mean=('lbc', 'mean'),
        n_lbc2=('lbc', lambda x: int((x >= 2).sum())),
        lu_cap_now=('cap', 'sum'),
        lu_cap_prev=('cap_prev', 'sum'),
        lu_capw=('capw', 'sum'),
    )
    # 非涨停股（跟风）
    ex = df[df['lu'] == 0].groupby('date').agg(
        cnt_ex=('pct', 'size'),
        sum_pct_ex=('pct', 'sum'),
        cap_now_ex=('cap', 'sum'),
        cap_prev_ex=('cap_prev', 'sum'),
        capw_ex=('capw', 'sum'),
    )
    r = allg.join(lug).join(ex).reset_index()
    r[['n_lu', 'lbc_sum', 'lbc_max', 'lbc_mean', 'n_lbc2',
       'lu_cap_now', 'lu_cap_prev', 'lu_capw']] = r[[
        'n_lu', 'lbc_sum', 'lbc_max', 'lbc_mean', 'n_lbc2',
        'lu_cap_now', 'lu_cap_prev', 'lu_capw']].fillna(0)
    r['industry'] = ind
    r['ind_ret_ew'] = r['sum_pct'] / r['cnt']
    r['ind_ret_cw'] = r['sum_capw'] / r['cap_prev'].replace(0, np.nan)
    r['ind_ret_ex'] = r['sum_pct_ex'] / r['cnt_ex'].replace(0, np.nan)
    # 市值增量 = Σ(昨市值 × 当日涨幅)，避开停牌/股本变动造成的口径跳变
    r['lu_mv_gain'] = r['lu_capw'] / 100
    r['lu_mv_pct'] = r['lu_capw'] / r['lu_cap_prev'].replace(0, np.nan)
    r['ind_mv_gain'] = r['sum_capw'] / 100
    r['ind_mv_pct'] = r['sum_capw'] / r['cap_prev'].replace(0, np.nan)
    r['ind_mv_gain_ex'] = r['capw_ex'] / 100
    r['ind_mv_pct_ex'] = r['capw_ex'] / r['cap_prev_ex'].replace(0, np.nan)
    return r


def build_panel():
    ind_map = load_industry()
    turn_map = load_turn()
    files = sorted(glob.glob(os.path.join(STOCKDB_DIR, '*#*.csv')))
    print(f'stockdb_csv 文件: {len(files)} 只, 换手率缓存: {len(turn_map)} 只')

    by_ind = {}
    for fp in files:
        m = re.search(r'([A-Z]{2})#(\d{6})\.csv$', fp)
        if not m:
            continue
        code6 = m.group(2)
        name, ind = ind_map.get(code6, ('', '未分类'))
        by_ind.setdefault(ind, []).append((fp, code6, name))

    panels = []
    for k, (ind, items) in enumerate(sorted(by_ind.items())):
        frames = []
        for fp, code6, name in items:
            try:
                sf = stock_frame(fp, code6, name, ind, turn_map)
                if sf is not None:
                    frames.append(sf)
            except Exception:
                continue
        if not frames:
            continue
        g = pd.concat(frames, ignore_index=True)
        p = aggregate_industry(g, ind)
        panels.append(p)
        print(f'  [{k+1}/{len(by_ind)}] {ind}: {len(items)} 只')

    panel = pd.concat(panels, ignore_index=True)
    panel = panel.sort_values(['industry', 'date']).reset_index(drop=True)
    panel['ind_ret_next'] = panel.groupby('industry')['ind_ret_ew'].shift(-1)
    panel['ind_mv_pct_next'] = panel.groupby('industry')['ind_mv_pct'].shift(-1)
    return panel


def sp(x, y, label, min_n=30):
    d = pd.DataFrame({'x': x, 'y': y}).dropna()
    if len(d) < min_n:
        return None
    rho, p = spearmanr(d['x'], d['y'])
    return (label, len(d), rho, p)


def report(panel):
    p = panel[panel['n_lu'] >= 3].copy()
    print('\n' + '=' * 78)
    print('样本: 行业当日涨停家数>=3   共 %d 个行业-日' % len(p))
    print('平均涨停家数 %.1f, 平均最高连板 %.2f' % (p['n_lu'].mean(), p['lbc_max'].mean()))
    if p.empty:
        return

    print('\n【一】连板高度 vs 同日行业表现 (Spearman, 全样本)')
    print('%-38s %8s %8s %8s' % ('关系', 'N', 'rho', 'p'))
    tests = [
        ('最高连板 ↔ 行业等权涨幅%', p['lbc_max'], p['ind_ret_ew']),
        ('最高连板 ↔ 行业市值加权涨幅%', p['lbc_max'], p['ind_ret_cw']),
        ('最高连板 ↔ 剔除涨停股后等权涨幅%', p['lbc_max'], p['ind_ret_ex']),
        ('最高连板 ↔ 涨停股市值增量(亿)', p['lbc_max'], p['lu_mv_gain']),
        ('最高连板 ↔ 涨停股市值增幅%', p['lbc_max'], p['lu_mv_pct']),
        ('最高连板 ↔ 行业市值增量(亿)', p['lbc_max'], p['ind_mv_gain']),
        ('最高连板 ↔ 剔涨停股行业市值增量(亿)', p['lbc_max'], p['ind_mv_gain_ex']),
        ('平均连板 ↔ 行业等权涨幅%', p['lbc_mean'], p['ind_ret_ew']),
        ('平均连板 ↔ 剔除涨停股后等权涨幅%', p['lbc_mean'], p['ind_ret_ex']),
        ('连板股只数 ↔ 行业等权涨幅%', p['n_lbc2'], p['ind_ret_ew']),
        ('连板股只数 ↔ 剔除涨停股后等权涨幅%', p['n_lbc2'], p['ind_ret_ex']),
        ('涨停家数 ↔ 行业等权涨幅%', p['n_lu'], p['ind_ret_ew']),
        ('涨停家数 ↔ 行业市值增量(亿)', p['n_lu'], p['ind_mv_gain']),
    ]
    for lab, x, y in tests:
        r = sp(x, y, lab)
        if r:
            print('%-38s %8d %8.3f %8.4f' % (r[0], r[1], r[2], r[3]))

    print('\n【二】按最高连板分档 (涨幅=均值, 市值增量=中位数)')
    bins = [0, 1, 2, 3, 4, 99]
    labels = ['1板', '2板', '3板', '4板', '5板+']
    p['lbc_bin'] = pd.cut(p['lbc_max'], bins=bins, labels=labels, right=True)
    p['lbc_share'] = p['n_lbc2'] / p['n_lu']
    ret_cols = ['n_lu', 'ind_ret_ew', 'ind_ret_cw', 'ind_ret_ex', 'lu_mv_pct', 'ind_ret_next']
    mv_cols = ['lu_mv_gain', 'ind_mv_gain', 'ind_mv_gain_ex']
    g_ret = p.groupby('lbc_bin', observed=True)[ret_cols].mean()
    g_mv = p.groupby('lbc_bin', observed=True)[mv_cols].median()
    g = g_ret.join(g_mv)
    g.insert(0, '样本数', p.groupby('lbc_bin', observed=True).size())
    print(g.round(2).to_string())

    print('\n【三】按涨停家数分档 (涨幅=均值, 市值增量=中位数)')
    p['n_bin'] = pd.cut(p['n_lu'], bins=[2, 3, 5, 8, 12, 999],
                        labels=['3只', '4-5只', '6-8只', '9-12只', '12只+'])
    g2_ret = p.groupby('n_bin', observed=True)[ret_cols + ['lbc_max', 'lbc_share']].mean()
    g2_mv = p.groupby('n_bin', observed=True)[mv_cols].median()
    g2 = g2_ret.join(g2_mv)
    g2.insert(0, '样本数', p.groupby('n_bin', observed=True).size())
    print(g2.round(2).to_string())

    print('\n【四】控制涨停家数后: 最高连板 ↔ 行业表现 (按 n_lu 分档内部 Spearman)')
    rows = []
    for lab in ['ind_ret_ew', 'ind_ret_ex', 'ind_mv_pct', 'lu_mv_pct']:
        parts = []
        for nb, gg in p.groupby('n_bin', observed=True):
            d2 = gg[['lbc_max', lab]].dropna()
            if len(d2) >= 50 and d2['lbc_max'].nunique() >= 2:
                rho, _ = spearmanr(d2['lbc_max'], d2[lab])
                parts.append((len(d2), rho, d2['lbc_max'].std()))
        if parts:
            n = sum(x[0] for x in parts)
            rho = sum(x[0] * x[1] for x in parts) / n
            print('%-22s N=%-5d 加权rho=%+.3f  分档rho=[%s]' % (
                lab, n, rho, ', '.join('%+.2f' % x[1] for x in parts)))
    del rows

    print('\n【五】连板股占比 (n_lbc2/n_lu) 与行业表现')
    for lab, x, y in [
        ('连板股占比 ↔ 行业等权涨幅%', p['lbc_share'], p['ind_ret_ew']),
        ('连板股占比 ↔ 剔除涨停股后等权涨幅%', p['lbc_share'], p['ind_ret_ex']),
        ('连板股占比 ↔ 行业市值增幅%', p['lbc_share'], p['ind_mv_pct']),
    ]:
        r = sp(x, y, lab)
        if r:
            print('%-38s %8d %8.3f %8.4f' % (r[0], r[1], r[2], r[3]))

    print('\n【六】同日横截面: 连板高度高的行业是否涨幅/市值增长更大 (逐日Spearman均值)')
    rows = []
    for d, gg in p.groupby('date'):
        if len(gg) < 3:
            continue
        for ycol, lab in [('ind_ret_ew', '行业等权涨幅'),
                          ('ind_ret_ex', '剔涨停股等权涨幅'),
                          ('ind_mv_pct', '行业市值增幅'),
                          ('ind_mv_gain', '行业市值增量')]:
            d2 = gg[['lbc_max', ycol]].dropna()
            if len(d2) >= 3 and d2['lbc_max'].nunique() >= 2:
                rho, _ = spearmanr(d2['lbc_max'], d2[ycol])
                rows.append({'date': d, 'y': lab, 'rho': rho})
    if rows:
        rr = pd.DataFrame(rows)
        gg = rr.groupby('y')['rho'].agg(['size', 'mean', 'std'])
        gg['t'] = gg['mean'] / (gg['std'] / np.sqrt(gg['size']))
        print(gg.round(3).to_string())
        print('(逐日横截面可比日期数: %d)' % rr['date'].nunique())

    print('\n【七】分年度稳定性: 最高连板 ↔ 行业等权涨幅 / 剔涨停股涨幅')
    p['year'] = pd.to_datetime(p['date']).dt.year
    for y, gg in p.groupby('year'):
        r1 = sp(gg['lbc_max'], gg['ind_ret_ew'], '')
        r2 = sp(gg['lbc_max'], gg['ind_ret_ex'], '')
        if r1 and r2:
            print('%d  N=%-5d  rho(行业涨幅)=%+.3f  rho(剔涨停股)=%+.3f' % (
                y, len(gg), r1[2], r2[2]))

    print('\n【八】同一行业内对照 (扣除行业均值后 Spearman, 排除行业成分差异)')
    for lab in ['ind_ret_ew', 'ind_ret_ex', 'ind_mv_pct', 'lu_mv_pct']:
        d2 = p[['industry', 'lbc_max', lab]].dropna()
        xd = d2.groupby('industry')['lbc_max'].transform(lambda s: s - s.mean())
        yd = d2.groupby('industry')[lab].transform(lambda s: s - s.mean())
        r = sp(xd, yd, '')
        if r:
            print('%-22s N=%-5d rho=%+.3f  p=%.4f' % (lab, r[1], r[2], r[3]))

    print('\n【九】次日表现 (T+1 行业等权涨幅/市值增幅, 按最高连板分档)')
    g3 = p.groupby('lbc_bin', observed=True)[['ind_ret_next', 'ind_mv_pct_next']].mean()
    g3.insert(0, '样本数', p.groupby('lbc_bin', observed=True).size())
    print(g3.round(2).to_string())
    for lab, x, y in [('最高连板 ↔ 次日行业涨幅', p['lbc_max'], p['ind_ret_next']),
                      ('最高连板 ↔ 次日行业市值增幅', p['lbc_max'], p['ind_mv_pct_next'])]:
        r = sp(x, y, lab)
        if r:
            print('%-38s %8d %8.3f %8.4f' % (r[0], r[1], r[2], r[3]))

    print('\n【十】市值增长拆解 (最高连板分档, 亿元/中位数)')
    g4 = p.groupby('lbc_bin', observed=True)[
        ['lu_cap_prev', 'lu_mv_gain', 'ind_mv_gain', 'ind_mv_gain_ex']].median()
    g4['涨停股占行业市值增量%'] = (p.groupby('lbc_bin', observed=True)
                               .apply(lambda x: x['lu_mv_gain'].sum() /
                                      x['ind_mv_gain'].sum() * 100,
                                      include_groups=False))
    print(g4.round(2).to_string())


def main():
    if '--reuse' in sys.argv and os.path.exists(OUT_CSV):
        panel = pd.read_csv(OUT_CSV, parse_dates=['date'])
        print('复用已生成面板: %s (%d 行)' % (OUT_CSV, len(panel)))
    else:
        panel = build_panel()
        panel.to_csv(OUT_CSV, index=False, encoding='utf-8-sig')
    report(panel)
    print('\n面板已保存: %s  (%d 行)' % (OUT_CSV, len(panel)))


if __name__ == '__main__':
    main()
