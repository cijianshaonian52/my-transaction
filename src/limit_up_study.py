# -*- coding: utf-8 -*-
"""涨停复盘观察的全市场实证验证(2026-09)

验证 9/16~9/21 盘面复盘中提出的四个命题:
  检验1: 大涨日(指数共振日)首板质量更高?
  检验2: 涨停家数递减预示退潮(对次日连板率有预测力)?
  检验3: 同板块多股涨停时, 量比/规模/连板高度能否区分次日强者("卡位/切换")?
  检验4: 一字板不给买入机会的机会成本("看得见≠拿得到")?

数据: stockdb_csv/ 前复权日线(全市场, 2021-08 ~ 2026-09-23) + idx_data/000300.SH.csv(至09-16)
涨停口径与 scan_signals.py 一致: 双创19.8 / ST4.8 / 主板9.8 (0.2pct容差), 上市前5日不计。
量比 = 当日成交量 / 前20日均量(不含当日)。一字板 = 当日最低价涨幅 >= 涨停阈值。
封死开盘(不可成交) = 次日开盘涨幅 >= 阈值-0.2。
输出: 控制台 + output/limit_up_study_YYYYMMDD.txt
"""
import sys, io, os, math, datetime
import numpy as np
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKDB_DIR = os.path.join(ROOT, 'stockdb_csv')
IDX_FILE = os.path.join(ROOT, 'idx_data', '000300.SH.csv')
IND_FILE = os.path.join(ROOT, 'cache_data', '_stock_industry.csv')
TODAY = datetime.date.today().strftime('%Y%m%d')
OUT_FILE = os.path.join(ROOT, 'output', f'limit_up_study_{TODAY}.txt')

IDX_LAST = pd.Timestamp('2026-09-16')   # 指数数据最后日, 事件日上限(检验1)
DATA_LAST = pd.Timestamp('2026-09-23')  # 个股数据最后日


def limit_threshold(code6, name):
    if code6.startswith(('300', '301', '688', '689')):
        return 19.8
    if code6.startswith(('43', '83', '87', '920')):
        return 29.8
    if name and 'ST' in str(name).upper():
        return 4.8
    return 9.8


def load_stock(fp, code6, name, industry):
    """加载单票日线, 返回(涨停事件DataFrame, 首板机会成本记录list)"""
    df = pd.read_csv(fp, encoding='gbk', skiprows=2, header=None,
                     names=['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'turnover'])
    df['date'] = pd.to_datetime(df['date'].astype(str), errors='coerce')
    df = df.dropna(subset=['date']).reset_index(drop=True)
    if len(df) < 40:
        return None, None, None, None, 0
    d = df['date']
    o = df['open'].astype(float); h = df['high'].astype(float)
    l = df['low'].astype(float);  c = df['close'].astype(float)
    v = df['volume'].astype(float); a = df['amount'].astype(float)

    pc = c.shift(1)
    pct = (c / pc - 1) * 100
    thr = limit_threshold(code6, name)
    idx = np.arange(len(df))
    valid = idx >= 5                      # 次新前5日无涨跌幅限制
    lu = ((pct >= thr) & valid).fillna(False)
    low_pct = (l / pc - 1) * 100
    one_word = ((low_pct >= thr) & valid).fillna(False)
    # 连板高度: 截至当日连续涨停天数
    grp = (~lu).cumsum()
    board_h = lu.groupby(grp).cumsum()
    first_lu = lu & ~lu.shift(1, fill_value=False)

    vol_ma20 = v.shift(1).rolling(20).mean()
    vol_ratio = v / vol_ma20
    amt20 = a.shift(1).rolling(20).mean() / 1e8

    # 次日字段
    lu_next = lu.shift(-1).astype(float)          # 1/0/NaN, 供groupby均值统计
    o_next = o.shift(-1)
    open_pct_next = (o_next / c - 1) * 100
    seal_next = open_pct_next >= (thr - 0.2)      # 次日开盘封死 → 不可成交
    r_oc3 = (c.shift(-3) / o_next - 1) * 100      # t+1开盘买 → t+3收盘
    r_oc5 = (c.shift(-5) / o_next - 1) * 100

    ev = pd.DataFrame({
        'date': d, 'code': code6, 'name': name, 'industry': industry,
        'thr': thr, 'board_h': board_h, 'first_lu': first_lu,
        'one_word': one_word, 'vol_ratio': vol_ratio, 'amt20': amt20,
        'lu_next': lu_next, 'seal_next': seal_next,
        'r_oc3': r_oc3, 'r_oc5': r_oc5,
    })
    ev = ev[lu].copy()

    # ── 检验5b: 半路伏击基线 —— 冲高未板(收盘涨5%~阈值)事件 ──
    near = ((~lu) & (pct >= 5) & (pct < thr) & valid).fillna(False)
    near_ev = pd.DataFrame({
        'date': d, 'code': code6, 'pct': pct, 'vol_ratio': vol_ratio,
        'lu_next': lu_next,                          # 次日是否涨停
        'r_close3': (c.shift(-3) / c - 1) * 100,     # 当日收盘买 → t+3收盘
    })
    near_ev = near_ev[near].copy()

    # ── 检验4: 首板后首次可成交日τ的路径 ──
    opp = []
    oa = o.values; ca = c.values; na = len(df)
    ow = one_word.values
    for t in np.where(first_lu.values)[0]:
        if t + 1 >= na:
            continue
        tau, gap = None, None
        for k in range(1, 11):
            if t + k >= na:
                break
            op = (oa[t + k] / ca[t + k - 1] - 1) * 100
            if op < thr - 0.2:                    # 开盘未封死, 可成交
                tau, gap = t + k, op
                break
        r_tau3 = (ca[tau + 3] / oa[tau] - 1) * 100 if (tau is not None and tau + 3 < na) else np.nan
        r_paper = (ca[t + 3] / ca[t] - 1) * 100 if t + 3 < na else np.nan  # 纸面: 首板收盘买持有3日
        opp.append({
            'date': d.iloc[t], 'code': code6, 'name': name, 'thr': thr,
            'one_word': bool(ow[t]),                                 # 首板是否一封到底
            'wait_days': (tau - t) if tau is not None else np.nan,  # 1=次日即可买
            'no_chance_10d': tau is None,
            'gap': (oa[tau] / ca[t] - 1) * 100 if tau is not None else np.nan,
            'r_tau3': r_tau3, 'r_paper': r_paper,
        })
    # ── 检验6: 昨日涨停 & 今日开盘未封死(9:30以开盘价可买) ──
    open_pct = (o / pc - 1) * 100
    cont = (lu.shift(1, fill_value=False) & (open_pct < thr - 0.2) & valid).fillna(False)
    cont_ev = pd.DataFrame({
        'date': d, 'code': code6, 'thr': thr,
        'prev_bh': board_h.shift(1),               # 昨日连板高度(≥1)
        'open_pct': open_pct, 'lu': lu.astype(float),
        'intraday': (c / o - 1) * 100,             # 开盘买当日浮盈
        'r_next_open': (o.shift(-1) / o - 1) * 100,  # T+1: 次日开盘卖
        'r_next_close': (c.shift(-1) / o - 1) * 100,  # 次日收盘卖
    })
    cont_ev = cont_ev[cont].copy()

    n_bars96 = int((d >= '1996-12-16').sum())     # 涨跌停制度后的样本日数, 作基准率分母
    return ev, near_ev, opp, cont_ev, n_bars96


def welch_t(x, y):
    """Welch t检验, 大样本正态近似双尾p"""
    x = pd.Series(x).dropna().values; y = pd.Series(y).dropna().values
    if len(x) < 30 or len(y) < 30:
        return np.nan
    mx, my = x.mean(), y.mean()
    vx, vy = x.var(ddof=1), y.var(ddof=1)
    se = math.sqrt(vx / len(x) + vy / len(y))
    if se < 1e-12:
        return np.nan
    t = (mx - my) / se
    return math.erfc(abs(t) / math.sqrt(2))


def star(p):
    if pd.isna(p): return ''
    return '***' if p < 0.001 else ('**' if p < 0.01 else ('*' if p < 0.05 else ''))


def fmt_row(label, n, lu_rate, trd_rate, r3, r5):
    med3 = np.nanmedian(r3) if len(r3) else np.nan
    win3 = np.nanmean(np.array(r3) > 0) * 100 if len(r3) else np.nan
    return (f'{label:<14}{n:>8}{lu_rate * 100:>9.1f}%{trd_rate * 100:>9.1f}%'
            f'{np.nanmean(r3):>9.2f}%{med3:>8.2f}%{win3:>8.1f}%{np.nanmean(r5):>9.2f}%')


def main():
    lines = []
    def emit(s=''):
        print(s); lines.append(s)

    # 行业/名称映射
    ind = pd.read_csv(IND_FILE, dtype=str)
    ind['code6'] = ind['ts_code'].str.split('.').str[0]
    name_map = ind.set_index('code6')['name'].to_dict()
    ind_map = ind.set_index('code6')['industry'].to_dict()

    emit('加载 stockdb_csv 全市场日线 ...')
    events, nears, opps, conts = [], [], [], []
    total_bars = 0
    files = sorted(os.listdir(STOCKDB_DIR))
    for i, fn in enumerate(files):
        if not fn.endswith('.csv'):
            continue
        code6 = fn.split('#')[1].split('.')[0]
        name = name_map.get(code6, '')
        industry = ind_map.get(code6, '')
        industry = industry if isinstance(industry, str) and industry else '未知'
        ev, ne, op, ce, nb = load_stock(os.path.join(STOCKDB_DIR, fn), code6, name, industry)
        total_bars += nb
        if ev is not None:
            events.append(ev)
        if ne is not None:
            nears.append(ne)
        if ce is not None:
            conts.append(ce)
        if op:
            opps.extend(op)
        if (i + 1) % 800 == 0:
            print(f'  {i + 1}/{len(files)} ...')
    E = pd.concat(events, ignore_index=True)
    N = pd.concat(nears, ignore_index=True) if nears else pd.DataFrame()
    C = pd.concat(conts, ignore_index=True) if conts else pd.DataFrame()
    O = pd.DataFrame(opps)
    # 涨跌停制度 1996-12-16 才设立, 之前无限制, 剔除早期数据
    E = E[E['date'] >= '1996-12-16'].reset_index(drop=True)
    N = N[N['date'] >= '1996-12-16'].reset_index(drop=True)
    C = C[C['date'] >= '1996-12-16'].reset_index(drop=True)
    O = O[O['date'] >= '1996-12-16'].reset_index(drop=True)
    E['is_st'] = E['thr'] < 5
    emit(f'涨停事件 {len(E):,} 条 ({E["code"].nunique()} 只票, {E["date"].min().date()} ~ {E["date"].max().date()})')
    emit('注: stockdb_csv 票池含主板10cm/双创20cm/北交30cm, 涨跌停阈值按代码板块识别')

    # 沪深300日涨幅
    idx = pd.read_csv(IDX_FILE)
    idx['date'] = pd.to_datetime(idx['date'])
    idx['idx_pct'] = idx['close'].pct_change() * 100
    E = E.merge(idx[['date', 'idx_pct']], on='date', how='left')

    # ═══════════ 检验1: 大涨日首板质量 ═══════════
    emit('\n' + '=' * 100)
    emit('检验1: 指数大涨日的首板, 次日连板率/后续收益是否更高?  (事件: 首板, 前一日非涨停)')
    emit('收益口径: t+1开盘买入(剔除开盘封死不可成交样本) → t+3/t+5收盘; 事件日 ≤ 2026-09-16')
    emit('-' * 100)
    f1 = E[E['first_lu'] & E['lu_next'].notna() & (E['date'] <= IDX_LAST)].copy()
    f1['r3'] = f1['r_oc3'].where(~f1['seal_next'])
    f1['r5'] = f1['r_oc5'].where(~f1['seal_next'])

    def report_group(sub, title):
        emit(f'\n[{title}]  n={len(sub):,}')
        emit(f'{"指数当日涨幅":<14}{"首板数":>8}{"次日连板率":>10}{"可成交率":>10}'
             f'{"3日均值":>9}{"3日中位":>9}{"3日胜率":>9}{"5日均值":>9}')
        bins = [-np.inf, 0, 1, 2, np.inf]
        labels = ['跌(<0)', '小涨(0~1%)', '中涨(1~2%)', '大涨(≥2%)']
        sub = sub.copy()
        sub['g'] = pd.cut(sub['idx_pct'], bins=bins, labels=labels)
        res = {}
        for lb in labels:
            g = sub[sub['g'] == lb]
            r3 = g['r3'].dropna().values; r5 = g['r5'].dropna().values
            res[lb] = (g, r3)
            emit(fmt_row(lb, len(g), g['lu_next'].mean(), (~g['seal_next']).mean(), r3, r5))
        return res

    res_all = report_group(f1, '全部首板')
    res_10 = report_group(f1[~f1['is_st']], '剔除ST')
    # 大涨(≥1.5%) vs 其余 显著性
    for res, tag in ((res_all, '全部'), (res_10, '非ST')):
        hi = pd.concat([res['中涨(1~2%)'][0], res['大涨(≥2%)'][0]])
        lo = pd.concat([res['跌(<0)'][0], res['小涨(0~1%)'][0]])
        p_lu = welch_t(hi['lu_next'].astype(float), lo['lu_next'].astype(float))
        p_r3 = welch_t(hi['r3'], lo['r3'])
        emit(f'  [{tag}] 指数涨≥1%日 vs <1%日: 连板率 {hi["lu_next"].mean() * 100:.1f}% vs {lo["lu_next"].mean() * 100:.1f}% '
             f'(p={p_lu:.4f}{star(p_lu)}); 3日收益 {np.nanmean(hi["r3"]):.2f}% vs {np.nanmean(lo["r3"]):.2f}% '
             f'(p={p_r3:.4f}{star(p_r3)})')

    # ═══════════ 检验2: 涨停家数递减 → 退潮 ═══════════
    emit('\n' + '=' * 100)
    emit('检验2: 全市场涨停家数递减, 是否预示次日连板率下降(退潮)?')
    emit('次日连板率 = 今日全部涨停股中, 明日继续涨停的比例')
    emit('-' * 100)
    daily = E.groupby('date').agg(lu_n=('code', 'count'), nbr=('lu_next', 'mean')).dropna()
    daily['first_n'] = E[E['first_lu']].groupby('date').size()
    daily['first_n'] = daily['first_n'].fillna(0)
    daily['ratio5'] = daily['lu_n'] / daily['lu_n'].shift(1).rolling(5).mean()
    daily['chg1'] = daily['lu_n'].pct_change()
    # 连续下降天数
    dn = (daily['lu_n'] < daily['lu_n'].shift(1)).astype(int)
    daily['dn_streak'] = dn.groupby((dn == 0).cumsum()).cumsum()
    up = (daily['lu_n'] > daily['lu_n'].shift(1)).astype(int)
    daily['up_streak'] = up.groupby((up == 0).cumsum()).cumsum()

    d2 = daily.dropna(subset=['ratio5'])
    c1 = np.corrcoef(d2['ratio5'], d2['nbr'])[0, 1]
    c2 = np.corrcoef(d2['chg1'], d2['nbr'])[0, 1]
    emit(f'样本日数 {len(d2)};  corr(家数/5日均值, 次日连板率) = {c1:.3f};  corr(家数环比, 次日连板率) = {c2:.3f}')
    lo_g = d2[d2['ratio5'] < 0.8]; hi_g = d2[d2['ratio5'] > 1.25]; mid = d2[(d2['ratio5'] >= 0.8) & (d2['ratio5'] <= 1.25)]
    p = welch_t(hi_g['nbr'], lo_g['nbr'])
    emit(f'家数骤降(<5日均值80%)日 n={len(lo_g)}: 次日连板率 {lo_g["nbr"].mean() * 100:.1f}%')
    emit(f'家数平稳日 n={len(mid)}: 次日连板率 {mid["nbr"].mean() * 100:.1f}%')
    emit(f'家数激增(>5日均值125%)日 n={len(hi_g)}: 次日连板率 {hi_g["nbr"].mean() * 100:.1f}%  (激增vs骤降 p={p:.4f}{star(p)})')
    for k in (1, 2, 3):
        g_dn = daily[daily['dn_streak'] >= k]; g_up = daily[daily['up_streak'] >= k]
        if len(g_dn) > 30 and len(g_up) > 30:
            p2 = welch_t(g_up['nbr'], g_dn['nbr'])
            emit(f'连续{k}天家数增加 n={len(g_up)}: 次日连板率 {g_up["nbr"].mean() * 100:.1f}%  |  '
                 f'连续{k}天减少 n={len(g_dn)}: {g_dn["nbr"].mean() * 100:.1f}%  (p={p2:.4f}{star(p2)})')
    # 退潮日的首板表现(可交易视角)
    dflag = daily[['ratio5']].copy()
    E2 = f1.merge(dflag, left_on='date', right_index=True, how='left')
    g_lo = E2[E2['ratio5'] < 0.8]; g_hi = E2[E2['ratio5'] > 1.25]
    p3 = welch_t(g_hi['r3'], g_lo['r3'])
    emit(f'退潮日(家数<均值80%)首板的3日收益 {np.nanmean(g_lo["r3"]):.2f}% vs 高潮日(>125%) {np.nanmean(g_hi["r3"]):.2f}% '
         f'(p={p3:.4f}{star(p3)});  连板率 {g_lo["lu_next"].mean() * 100:.1f}% vs {g_hi["lu_next"].mean() * 100:.1f}%')

    # ═══════════ 检验3: 同板块多股涨停, 量比/规模/高度因子 ═══════════
    emit('\n' + '=' * 100)
    emit('检验3: 同行业(CSRC)当日≥2只涨停时, 谁次日更强?  (剔除ST, 用户的"卡位/切换"语境)')
    emit('-' * 100)
    e3 = E[(~E['is_st']) & (E['industry'] != '未知') & E['lu_next'].notna()].copy()
    gsize = e3.groupby(['date', 'industry'])['code'].transform('count')
    e3 = e3[gsize >= 2].copy()
    e3['vr_rank'] = e3.groupby(['date', 'industry'])['vol_ratio'].rank(pct=True)   # 低=量更冷
    e3['amt_rank'] = e3.groupby(['date', 'industry'])['amt20'].rank(pct=True)     # 低=规模更小
    e3['r3'] = e3['r_oc3'].where(~e3['seal_next'])
    e3['grp_n'] = gsize[gsize >= 2]
    ok = e3.dropna(subset=['vr_rank'])
    emit(f'同板块多股涨停事件 {len(ok):,} 条')
    emit(f'corr(量比行业内分位, 次日连板) = {np.corrcoef(ok["vr_rank"], ok["lu_next"].astype(float))[0, 1]:.3f}')
    r_ok = ok.dropna(subset=['r3'])
    emit(f'corr(量比行业内分位, 3日收益) = {np.corrcoef(r_ok["vr_rank"], r_ok["r3"])[0, 1]:.3f}  (n={len(r_ok):,})')
    a_ok = ok.dropna(subset=['amt_rank'])
    emit(f'corr(规模行业内分位, 次日连板) = {np.corrcoef(a_ok["amt_rank"], a_ok["lu_next"].astype(float))[0, 1]:.3f}')
    # 行业内量比最低 vs 其余
    lo1 = ok[ok.groupby(['date', 'industry'])['vol_ratio'].rank(method='first') == 1]
    rest = ok.drop(lo1.index)
    p = welch_t(lo1['lu_next'].astype(float), rest['lu_next'].astype(float))
    pr = welch_t(lo1['r3'], rest['r3'])
    emit(f'板块内量比最低1只: 连板率 {lo1["lu_next"].mean() * 100:.1f}% vs 其余 {rest["lu_next"].mean() * 100:.1f}% '
         f'(p={p:.4f}{star(p)});  3日收益 {np.nanmean(lo1["r3"]):.2f}% vs {np.nanmean(rest["r3"]):.2f}% (p={pr:.4f}{star(pr)})')
    # 连板高度: 首板 vs 2板 vs 3板+
    emit('\n按当日连板高度分组(同板块多股涨停样本):')
    emit(f'{"高度":<12}{"n":>8}{"次日连板率":>10}{"3日收益":>9}{"3日胜率":>9}')
    for lb, mask in (('首板', ok['board_h'] == 1), ('2板', ok['board_h'] == 2), ('3板+', ok['board_h'] >= 3)):
        g = ok[mask]; r3 = g['r3'].dropna()
        emit(f'{lb:<12}{len(g):>8}{g["lu_next"].mean() * 100:>9.1f}%{np.nanmean(r3):>9.2f}%{(r3 > 0).mean() * 100:>8.1f}%')
    # 板块梯队: 板块当日涨停家数 → 板块内个股次日表现
    emit('\n按板块当日涨停家数分组(板块梯队完整性):')
    emit(f'{"板块涨停数":<12}{"n":>8}{"次日连板率":>10}{"3日收益":>9}')
    for lb, mask in (('2只', ok['grp_n'] == 2), ('3~4只', ok['grp_n'].between(3, 4)),
                     ('5~9只', ok['grp_n'].between(5, 9)), ('≥10只', ok['grp_n'] >= 10)):
        g = ok[mask]; r3 = g['r3'].dropna()
        emit(f'{lb:<12}{len(g):>8}{g["lu_next"].mean() * 100:>9.1f}%{np.nanmean(r3):>9.2f}%')

    # ═══════════ 检验4: 一字板机会成本 ═══════════
    emit('\n' + '=' * 100)
    emit('检验4: 首板后的可成交性 —— "补涨股一字板买不进"的机会成本 (剔除ST)')
    emit('-' * 100)
    o4 = O[O['thr'] > 5].copy()
    o4['r_tau3'] = o4['r_tau3'].where(o4['date'] <= DATA_LAST - pd.Timedelta(days=20))
    o4['r_paper'] = o4['r_paper'].where(o4['date'] <= DATA_LAST - pd.Timedelta(days=20))
    n_all = len(o4)
    n_d1 = (o4['wait_days'] == 1).sum()
    n_d23 = o4['wait_days'].isin([2, 3]).sum()
    n_d4p = (o4['wait_days'] >= 4).sum()
    n_no = o4['no_chance_10d'].sum()
    emit(f'首板样本 {n_all:,} 条: 次日开盘即可买 {n_d1 / n_all * 100:.1f}%, 等2~3天 {n_d23 / n_all * 100:.1f}%, '
         f'等≥4天 {n_d4p / n_all * 100:.1f}%, 10日内全程一字买不进 {n_no / n_all * 100:.1f}%')
    emit(f'{"等待天数":<12}{"n":>8}{"买入溢价":>10}{"买后3日均值":>12}{"中位":>9}{"胜率":>9}')
    for lb, mask in (('次日(D+1)', o4['wait_days'] == 1), ('D+2', o4['wait_days'] == 2),
                     ('D+3', o4['wait_days'] == 3), ('D+4~10', o4['wait_days'] >= 4)):
        g = o4[mask].dropna(subset=['r_tau3'])
        if len(g) < 10:
            continue
        emit(f'{lb:<12}{len(g):>8}{g["gap"].mean():>9.2f}%{g["r_tau3"].mean():>11.2f}%'
             f'{g["r_tau3"].median():>9.2f}%{(g["r_tau3"] > 0).mean() * 100:>8.1f}%')
    g1 = o4[o4['wait_days'] == 1].dropna(subset=['r_tau3'])
    gw = o4[o4['wait_days'] >= 2].dropna(subset=['r_tau3'])
    p = welch_t(g1['r_tau3'], gw['r_tau3'])
    emit(f'次日可买(D+1) vs 等到D+2以后: 买后3日收益 {g1["r_tau3"].mean():.2f}% vs {gw["r_tau3"].mean():.2f}% '
         f'(p={p:.4f}{star(p)})')
    emit(f'纸面对比: 若首板收盘即买入持有3日, 全样本均值 {o4["r_paper"].mean():.2f}%  '
         f'vs 实际可成交路径 {o4["r_tau3"].mean():.2f}%')

    # ═══════════ 检验5: "提前买"三条路径的可行性 ═══════════
    emit('\n' + '=' * 100)
    emit('检验5: "提前瞄准卡位股买"行不行 —— 打首板 / 半路伏击 / 完全埋伏 (剔除ST)')
    emit('-' * 100)
    base_lu = len(E) / total_bars * 100 if total_bars else np.nan

    # 5a 打首板: 一封到底(排不进) vs 盘中开过(可成交), 首板收盘≈涨停价买入持有3日(纸面)
    o5 = O[(O['thr'] > 5) & (O['date'] <= DATA_LAST - pd.Timedelta(days=20))].copy()
    seal = o5[o5['one_word']].dropna(subset=['r_paper'])
    opbd = o5[~o5['one_word']].dropna(subset=['r_paper'])
    p = welch_t(seal['r_paper'], opbd['r_paper'])
    emit('\n[5a 打首板] 首板涨停价买入持有3日(纸面, 未扣打板滑点):')
    emit(f'  一封到底(排不进去) {len(seal):,}条({len(seal) / len(o5) * 100:.1f}%): '
         f'均值 {seal["r_paper"].mean():.2f}% 中位 {seal["r_paper"].median():.2f}% 胜率 {(seal["r_paper"] > 0).mean() * 100:.1f}%')
    emit(f'  盘中开过(能成交)   {len(opbd):,}条({len(opbd) / len(o5) * 100:.1f}%): '
         f'均值 {opbd["r_paper"].mean():.2f}% 中位 {opbd["r_paper"].median():.2f}% 胜率 {(opbd["r_paper"] > 0).mean() * 100:.1f}%'
         f'  (p={p:.4f}{star(p)})')

    # 5b 半路伏击: 收盘涨5%~涨停但未封板的事件
    n5 = N[N['lu_next'].notna() & (N['date'] <= DATA_LAST - pd.Timedelta(days=8))].copy()
    hit = n5['lu_next'].mean() * 100
    r = n5['r_close3'].dropna()
    emit(f'\n[5b 半路伏击] 收盘涨5%以上但未封板事件 {len(n5):,} 条:')
    emit(f'  次日涨停率 {hit:.2f}%  vs  全市场单日基准涨停率 {base_lu:.2f}%  (约 {hit / base_lu:.1f} 倍)')
    emit(f'  当日收盘买入持有3日: 均值 {r.mean():.2f}% 中位 {r.median():.2f}% 胜率 {(r > 0).mean() * 100:.1f}%')
    cold = n5[n5['vol_ratio'] < 1]; hot = n5[n5['vol_ratio'] >= 3]
    emit(f'  其中 冷门(量比<1) n={len(cold):,}: 次日涨停率 {cold["lu_next"].mean() * 100:.2f}%, '
         f'3日收益 {cold["r_close3"].mean():.2f}%')
    emit(f'       爆量(量比≥3) n={len(hot):,}: 次日涨停率 {hot["lu_next"].mean() * 100:.2f}%, '
         f'3日收益 {hot["r_close3"].mean():.2f}%')

    # 5c 完全埋伏
    emit(f'\n[5c 完全埋伏] 全市场单日基准涨停率 {base_lu:.2f}%, '
         f'即一只股票平均 {1 / (base_lu / 100) / 244:.1f} 年才轮到1次涨停')
    emit('  事前无任何涨停信号时埋伏, 其余约98%的日子持仓收益自负')

    # ═══════════ 检验6: 昨日涨停, 今日9:30开盘未封死就买 ═══════════
    emit('\n' + '=' * 100)
    emit('检验6: 昨日涨停股, 今日开盘未封死(9:30以开盘价可买) → 当日封板率与T+1收益 (剔除ST)')
    emit('口径: T+1制度当日买入不可卖; r_次日开 = 开盘买入→次日开盘卖出')
    emit('-' * 100)
    c6 = C[(C['thr'] > 5) & C['r_next_open'].notna()].copy()

    def r6(label, g):
        emit(f'{label:<14}{len(g):>8}{g["lu"].mean() * 100:>9.1f}%{g["intraday"].mean():>9.2f}%'
             f'{g["r_next_open"].mean():>9.2f}%{g["r_next_open"].median():>9.2f}%'
             f'{(g["r_next_open"] > 0).mean() * 100:>8.1f}%{g["r_next_close"].mean():>9.2f}%')

    emit(f'\n{"开盘涨幅档":<14}{"n":>8}{"当日封板率":>10}{"当日浮盈":>10}'
         f'{"次开卖均值":>10}{"中位":>10}{"胜率":>9}{"次收卖均值":>10}')
    for lb, m in (('低开(<0%)', c6['open_pct'] < 0),
                  ('平开(0~3%)', c6['open_pct'].between(0, 3)),
                  ('中开(3~6%)', c6['open_pct'].between(3, 6)),
                  ('高开(6~9.6%)', c6['open_pct'].between(6, 9.6))):
        r6(lb, c6[m])
    emit(f'\n{"昨日连板高度":<14}{"n":>8}{"当日封板率":>10}{"当日浮盈":>10}'
         f'{"次开卖均值":>10}{"中位":>10}{"胜率":>9}{"次收卖均值":>10}')
    for lb, m in (('昨日首板→2板', c6['prev_bh'] == 1),
                  ('昨日2板→3板', c6['prev_bh'] == 2),
                  ('昨日3板+→高位', c6['prev_bh'] >= 3)):
        r6(lb, c6[m])

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'\n已保存: {OUT_FILE}')


if __name__ == '__main__':
    main()
