# -*- coding: utf-8 -*-
"""A股情绪规则判定层 —— 在情绪快照序列之上, 把固定规则翻译成当日行动结论

规则体系(用户固定的 A 股交易情绪与择时规则):
  1 宽度×高度 : 市场有宽度没高度低吸 / 有高度没宽度打板
  2 大盘环境  : 大盘杀跌找抱团 / 大盘上涨找共振
  3 主线状态  : 主线转弱节点不碰支线、不碰任何缩量板 / 主线转强节点闭眼买前排核心辨识度
  4 情绪拐点  : 情绪转弱第一天杜绝一切盘中涨停板 / 情绪转强第一天抢第一时间窗口的辨识度
  5 加速票    : 任何加速票一概不接(连板>=3 且一字/缩量)
  6 超预期    : 超预期不看主板跟风, 观察创业板弹性
  7 龙头标准  : 断板前至少 5 板才配称为龙头; 板块龙头打板、市场龙头低吸
  8 仓位      : 阶段 → 仓位档位; 单票 ≤5 成, 高位票(≥4板) ≤3 成, 禁止满仓单票隔夜
  9 异动反馈  : 密切关注异动品种反馈(炸板/跌停/溢价)
  ── 以下整合自 10.docx(选股/退潮框架) 与 投机者76(执行教训) ──
 10 板块分层  : 龙头(最高板) / 连板 / 补涨(首板); 打板只打龙头, 补涨只做最先封板的那只
 11 题材结束  : 板块最高标断板 + 后排 0 首板 = 题材结束, 当日禁做该板块
 12 板块未退潮: 昨涨停股今日过半有溢价(覆盖率≥50%) 且龙头/补涨无跌停 → 仍可接力
 13 资金合力  : 涨停分散(top3≤45%) + 昨涨停溢价<1% → 无合力, 降仓/空仓, 不排板
 14 兑现纪律  : 昨涨停股冲高≥+5% 后回落≥5pct → 无条件减仓; 高位断板不接反包不补仓

前置: 先跑 src\\sentiment_snapshot.py 生成 output/sentiment_daily.csv
用法:
  .venv\\Scripts\\python.exe src\\sentiment_rules.py                      # 最新交易日
  .venv\\Scripts\\python.exe src\\sentiment_rules.py --date 2026-10-27    # 历史回看
  .venv\\Scripts\\python.exe src\\sentiment_rules.py --dir stockdb_csv  # 明细扫描数据源
"""
import os, sys, io, glob, argparse
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR_DEFAULT = 'stockdb_csv'
SNAP_FILE = os.path.join(ROOT, 'output', 'sentiment_daily.csv')
IND_FILE = os.path.join(ROOT, 'cache_data', '_stock_industry.csv')
IND_SENTI_FILE = os.path.join(ROOT, 'output', 'sentiment_industry.csv')   # 情绪快照的行业级序列
CON_SENTI_FILE = os.path.join(ROOT, 'output', 'sentiment_concept.csv')    # 情绪快照的概念级序列

# —— 规则常量(可调) ——
BREADTH_HI, BREADTH_LO = 45, 25      # 有宽度 / 无宽度(非ST涨停家数)
HEIGHT_HI, HEIGHT_LO = 5, 2          # 有高度(龙头标准 5 板) / 无高度
HUG_TOP1, HUG_TOP3 = 35, 60          # 抱团: top1 占比>=35% 或 top3 占比>=60%
RES_TOP3, RES_MIN_ZT = 45, 30        # 共振: top3 占比<=45%(涨停>=30 家才判)
RISING_MIN = 6                       # 温度分日变动 >=6 转强 / <=-6 转弱
DRAGON_BOARD = 5                     # 市场龙头标准: 连板 >= 5
MAIN_N, MAIN_MULT = 3, 1.5           # 主线转强: top1 涨停 >=3 家 且 >= 昨日 1.5 倍(或新高板晋级)
MAIN_WEAK_RATIO = 0.5                # 主线转弱: top1 家数 <= 昨日一半(或主线龙头断板)
JIA_SU_BOARD = 3                     # 加速票: 连板 >= 3 且 (一字 或 缩量)
OVER_PREM, OVER_TEMP = 4.0, 85       # 超预期: 溢价>=4% 或 温度>=85
SECTOR_MIN_ZT = 3                    # 板块退潮: 昨日涨停 >= 3 家才判
SECTOR_COV_LO = 50                   # 覆盖率 < 50% → 预警
THEME_CLIMAX_LOOKBACK = 60           # 新题材"未高潮"回看: 近 N 个交易日
THEME_CLIMAX_ZT = 5                  # 同行业单日涨停 >= 该家数 视为题材高潮
SECTOR_COV_DT = 60                   # 有 1 家跌停时, 覆盖率 < 60% 才预警
SECTOR_DT_HI = 2                     # 跌停 >= 2 家 → 预警
SECTOR_LEADER_MIN = 3                # 板块最高标: 昨日该行业最高板 >= 3 才跟踪断板
ACC_HIGH_BOARD = 4                   # 高位段: 连板 >= 4 一律不追板
FADE_TOP, FADE_GAP = 5.0, 5.0        # 兑现预警: 今日最高 >=+5% 且 (最高-收盘) >=5pct
SINGLE_POS_CAP = 50                  # 单票 <= 50% 总资产
HIGH_POS_CAP = 30                    # 高位票(>=4板) 单票 <= 30%
HELI_PREM_MIN = 1.0                  # 资金合力: 昨涨停溢价 < 1% 且 top3 分散 → 无合力

POSITION = {
    '冰点': '空仓/极轻仓, 等待, 观察谁先起来',
    '修复': '试错仓 1~2 成, 打第一个卡位的龙头',
    '发酵': '5~7 成, 持有龙头可加仓',
    '高潮': '≤3 成, 减仓不追高, 准备离场',
    '退潮': '空仓(最难也最值钱的操作)',
}
STAGE_SENSE = {
    '冰点': '空仓等修复', '修复': '试错打首个卡位', '发酵': '主线持有可加仓',
    '高潮': '减仓不追高', '退潮': '空仓',
}


def limit_threshold(code6, name):
    if code6.startswith(('300', '301', '688', '689')):
        return 19.8
    if code6.startswith(('43', '83', '87', '920')):
        return 29.8
    if name and 'ST' in str(name).upper():
        return 4.8
    return 9.8


def load_stock(fp):
    try:
        lines = open(fp, encoding='gbk', errors='ignore').read().splitlines()
    except Exception:
        return '', [], [], [], [], []
    name = ''
    if lines:
        parts = lines[0].strip().split()
        if len(parts) >= 2 and parts[0][:4].isdigit():
            name = parts[1]
    d, o, h, c, v = [], [], [], [], []
    for ln in lines[1:]:
        p = ln.split(',')
        if len(p) not in (7, 8):
            continue
        if not (p[0][:4].isdigit() and '-' in p[0]):
            continue
        try:
            d.append(p[0]); o.append(float(p[1])); h.append(float(p[2]))
            c.append(float(p[4])); v.append(float(p[5]))
        except ValueError:
            continue
    return name, d, o, h, c, v


def scan_detail(data_dir, T, T_prev):
    """扫描全部股票 → (涨停明细 {date: [rec]}, T 日全样本统计 {code: {pct, high_gap}})"""
    ind = pd.read_csv(IND_FILE, dtype=str)
    ind['code6'] = ind['ts_code'].str.split('.').str[0]
    name_map = ind.set_index('code6')['name'].to_dict()
    ind_map = ind.set_index('code6')['industry'].to_dict()
    recs = {T: [], T_prev: []}
    stats = {}
    files = sorted([f for f in os.listdir(data_dir) if f.endswith('.csv')])
    for fn in files:
        code6 = fn.split('#')[1].split('.')[0]
        name, d, o, h, c, v = load_stock(os.path.join(data_dir, fn))
        if len(d) < 6:
            continue
        name = name_map.get(code6) or name           # 个股文件表头名称可能被截断, 优先用行业表全称
        if not name:
            continue
        nm = str(name).upper()
        if 'ST' in nm or '退' in nm:
            continue
        thr = limit_threshold(code6, name)
        streak = 0
        got = {}
        for i in range(5, len(d)):
            pct = (c[i] / c[i - 1] - 1) * 100
            if d[i] == T:                                                # 兑现纪律要用当日最高价
                stats[code6] = {'pct': pct, 'high_gap': (h[i] / c[i - 1] - 1) * 100}
            if pct >= thr:
                streak += 1
            else:
                streak = 0
                continue
            if d[i] not in (T, T_prev):
                continue
            rec = {
                'code': code6, 'name': name,
                'industry': ind_map.get(code6, '未知') or '未知',
                'board': streak,
                'yizi': (o[i] / c[i - 1] - 1) * 100 >= thr,
                'suoliang': v[i] < v[i - 1],
                'open_gap': (o[i] / c[i - 1] - 1) * 100,
                'close': c[i],
            }
            got[d[i]] = rec
        for dt, rec in got.items():
            recs[dt].append(rec)
    return recs, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='回看指定交易日, 默认最新')
    ap.add_argument('--dir', default=DATA_DIR_DEFAULT, help='明细扫描数据源')
    args = ap.parse_args()

    df = pd.read_csv(SNAP_FILE, dtype={'date': str})
    if args.date:
        if args.date not in df['date'].values:
            print(f'!! {args.date} 不在快照序列内'); return
        T = args.date
    else:
        T = df.iloc[-1]['date']
    i_f = df.index[df['date'] == T][0]
    if i_f < 1:
        print('快照序列不足 2 行, 无法判定'); return
    r, rp = df.iloc[i_f], df.iloc[i_f - 1]
    T_prev = rp['date']
    # 转强/转弱及第一天(需再前一日)
    def flag(cur, prev):
        return ('r' if cur['temp'] - prev['temp'] >= RISING_MIN else
                'f' if cur['temp'] - prev['temp'] <= -RISING_MIN else '-')
    fl = []
    for k in range(1, len(df)):
        fl.append(flag(df.iloc[k], df.iloc[k - 1]))
    f_cur, f_prev = fl[i_f - 1], fl[i_f - 2] if i_f >= 2 else '-'
    first_weaken = (r['stage'] == '退潮' and rp['stage'] != '退潮') or (f_cur == 'f' and f_prev != 'f')
    # 退潮优先级最高: 退潮期内温度反弹不算"转强第一天"
    first_strengthen = (r['stage'] != '退潮'
                        and ((r['stage'] in ('修复', '发酵', '高潮') and rp['stage'] in ('冰点', '退潮'))
                             or (f_cur == 'r' and f_prev != 'r')))
    accelerate = r['temp'] >= OVER_TEMP and f_cur == 'r'

    zt, bb = r['zt'], r['max_board']
    breadth = '有宽度' if zt >= BREADTH_HI else ('无宽度' if zt <= BREADTH_LO else '宽度中性')
    height = '有高度' if bb >= HEIGHT_HI else ('无高度' if bb <= HEIGHT_LO else '高度中性')
    top1_n, top3_share = r['top1_n'], r['top3_share']
    hug = top1_n / zt * 100 >= HUG_TOP1 or top3_share >= HUG_TOP3 if zt else False
    res = top3_share <= RES_TOP3 and zt >= RES_MIN_ZT if zt else False
    # 大盘
    prem = r['premium'] if pd.notna(r['premium']) else None
    hs, ma20 = r['hs300'], r['hs300_ma20']
    idx_ok = pd.notna(hs) and pd.notna(ma20)
    up_env = hs > ma20 if idx_ok else None
    if not idx_ok:
        last_idx = df[pd.notna(df['hs300'])]
        last_idx_date = last_idx.iloc[-1]['date'] if len(last_idx) else '无'
        idx_warn = f'指数数据仅到 {last_idx_date}, 未覆盖当日'
    else:
        idx_warn = ''
    # 主线判定: 既成主线 = 昨日 top1(家数达标才有资格); 新主线爆发 = 今日 top1 家数跳增
    t_ind = r['top1_ind']
    pr_txt = f'{prem:.2f}%' if prem is not None else '—'
    recs, stats = scan_detail(os.path.join(ROOT, args.dir), T, T_prev)
    recT, recP = recs[T], recs[T_prev]
    # 行业级快照(可选): 昨涨停股今日溢价覆盖/跌停/首板/最高标断板
    ind_rows_f = None
    pk = None
    if os.path.exists(IND_SENTI_FILE):
        di = pd.read_csv(IND_SENTI_FILE, dtype={'date': str})
        ind_rows_f = di[di['date'] == T].copy()
        for col in ('prem_avg', 'pos_ratio', 'promo_ratio',
                    'first_board_n', 'leader_board', 'leader_break', 'leader_dt'):
            ind_rows_f[col] = pd.to_numeric(ind_rows_f[col], errors='coerce')
        # 各行业近 N 个交易日单日涨停家数峰值(严格早于当日), 用于"未高潮"判定
        cal_i = sorted(di['date'].unique())
        prior = [d for d in cal_i if d < T][-THEME_CLIMAX_LOOKBACK:]
        pk = di[di['date'].isin(prior)].groupby('industry')['zt_n'].max()
    else:
        print('!! 缺 output/sentiment_industry.csv → 板块分层/板块温度/分歧日新题材首板跳过(先跑 sentiment_snapshot.py)')
    # 概念级快照(可选): 题材横跨 CSRC 行业, 行业级指标切不开(机器人 = C34 + I65)
    con_rows_f = None
    if os.path.exists(CON_SENTI_FILE):
        dcc = pd.read_csv(CON_SENTI_FILE, dtype={'date': str})
        con_rows_f = dcc[dcc['date'] == T].copy()
        for col in ('prem_avg', 'pos_ratio', 'promo_ratio',
                    'first_board_n', 'leader_board', 'leader_break', 'leader_dt'):
            con_rows_f[col] = pd.to_numeric(con_rows_f[col], errors='coerce')
    else:
        print('!! 缺 output/sentiment_concept.csv → 题材温度跳过(概念口径缺失时仅行业口径)')
    zt_codes_t = {x['code'] for x in recT}
    t_recs = [x for x in recT if x['industry'] == t_ind]          # 今日最强行业今日涨停
    t_yday = [x for x in recP if x['industry'] == t_ind]          # 该行业昨日涨停
    t_max = max(x['board'] for x in t_recs) if t_recs else 0
    y_ind, y_n = rp['top1_ind'], rp['top1_n']                     # 昨日 top1(既成主线)
    y_ok = pd.notna(y_ind) and y_n >= MAIN_N
    y_rec_t = [x for x in recT if x['industry'] == y_ind] if y_ok else []
    y_rec_p = [x for x in recP if x['industry'] == y_ind] if y_ok else []
    main_weak = main_strong = False
    weak_reason = strong_reason = ''
    if y_ok:
        if len(y_rec_t) <= y_n * MAIN_WEAK_RATIO:
            main_weak = True
            weak_reason = f'{y_ind} 今日 {len(y_rec_t)} 家, 昨日 {y_n} 家(腰斩)'
        elif y_rec_p:
            ymax = max(x['board'] for x in y_rec_p)
            yleaders = {x['code'] for x in y_rec_p if x['board'] == ymax and x['board'] >= 3}
            if yleaders and not (yleaders & {x['code'] for x in y_rec_t}):
                main_weak = True
                weak_reason = f'{y_ind} 龙头断板(昨最高 {ymax} 板)'
    if top1_n >= MAIN_N and t_max >= 2:
        if not t_yday or top1_n >= len(t_yday) * MAIN_MULT:
            main_strong = True
            strong_reason = f'{t_ind} 涨停 {top1_n} 家(昨 {len(t_yday)})'
        elif t_recs and t_yday and t_max > max(x['board'] for x in t_yday):
            main_strong = True
            strong_reason = f'{t_ind} 新高板晋级 {t_max} 板'
    theme_burst = top1_n >= MAIN_N and t_max <= 1   # 有家数无梯队 = 新题材侦察, 不是主线转强

    dragons = sorted([x for x in recT if x['board'] >= DRAGON_BOARD],
                     key=lambda x: -x['board'])
    broken_dragons = sorted([x for x in recP if x['board'] >= DRAGON_BOARD
                             and x['code'] not in zt_codes_t],
                            key=lambda x: -x['board'])
    jasun = sorted([x for x in recT if JIA_SU_BOARD <= x['board'] < ACC_HIGH_BOARD
                    and (x['yizi'] or x['suoliang'])], key=lambda x: -x['board'])
    hi_acc = sorted([x for x in recT if x['board'] >= ACC_HIGH_BOARD], key=lambda x: -x['board'])
    hi_break = sorted([x for x in recP if x['board'] >= ACC_HIGH_BOARD
                       and x['code'] not in zt_codes_t], key=lambda x: -x['board'])
    # 兑现纪律(投机者76): 昨涨停股今日冲高后回落
    fade = []
    for x in recP:
        s = stats.get(x['code'])
        if not s:
            continue
        gap = s['high_gap'] - s['pct']
        if s['high_gap'] >= FADE_TOP and gap >= FADE_GAP:
            fade.append((x, s, gap))
    fade.sort(key=lambda t: -t[2])
    over_beyond = (prem is not None and prem >= OVER_PREM) or r['temp'] >= OVER_TEMP

    # —— 输出 ——
    print()
    print('=' * 70)
    print(f' A股情绪规则引擎 · {T}  (快照 {SNAP_FILE.split(chr(92))[-1]})')
    print('=' * 70)
    # 今日结论
    hdr = (f'情绪 {r["temp"]} 分 {r["stage"]} | 涨停 {zt} · 炸板 {r["zha"]} · 封板率 {r["seal"]}% | '
           f'最高板 {bb} | 溢价 {pr_txt} | 跌停 {r["dt"]}'
           + (' | 高标分歧' if r.get('diverge') else ''))
    print(f'【今日结论】{hdr}')
    sense = STAGE_SENSE[r['stage']]
    env_txt = '上涨' if (idx_ok and up_env) else ('杀跌' if idx_ok else '指数未更新')
    print(f'→ {breadth}×{height} | 大盘 {env_txt} | {sense}')
    print('-' * 70)
    # 1 阶段与拐点
    if r['stage'] == '退潮':
        trend_txt = '退潮中(反抽)' if f_cur == 'r' else '退潮中'
    else:
        trend_txt = '转强' if f_cur == 'r' else ('转弱' if f_cur == 'f' else '持平')
    print(f'[阶段] {r["stage"]}({r["temp"]} 分, 昨 {rp["temp"]}) {trend_txt}'
          f'{", 加速上行段" if accelerate else ""}')
    if first_strengthen:
        print('        → 情绪转强第一天: 抢第一时间窗口的辨识度(前排核心)')
    if first_weaken:
        print('        → 情绪转弱第一天: 杜绝一切盘中涨停板, 不接任何板')
    # 2 宽度×高度
    if zt >= BREADTH_HI and bb <= HEIGHT_LO:
        act = '低吸(有宽度没高度)'
    elif zt <= BREADTH_LO and bb >= HEIGHT_HI:
        act = '打板(有高度没宽度, 聚焦龙头抱团)'
    elif zt >= BREADTH_HI and bb >= HEIGHT_HI:
        act = '主线共振打法(打板前端 + 龙头低吸)'
    elif zt <= BREADTH_LO and bb <= HEIGHT_LO:
        act = '观望(宽阔双杀, 等冰点修复)'
    else:
        act = '中性(无优势组合, 以仓位纪律为准)'
    print(f'[宽度高度] 涨停 {zt} 家({breadth}) × 最高板 {bb}({height}) → {act}'
          f'{"  (退潮期该矩阵不作数, 以仓位为准)" if r["stage"] == "退潮" else ""}')
    # 3 大盘×抱团共振
    share_txt = f'top1 {t_ind} {top1_n} 家({top1_n / zt * 100:.0f}%) top3 {top3_share}%' if zt else '—'
    if idx_ok:
        print(f'[大盘环境] HS300 {hs:.0f} vs MA20 {ma20:.0f} → {"上涨" if up_env else "杀跌"} | 涨停分布: {share_txt}')
        if not up_env and hug:
            print('        → 大盘杀跌找抱团: 盯抱团主线, 低吸抱团核心')
        elif not up_env and res:
            print('        → 杀跌但共振分散: 抱团未形成, 耐心等抱团再动手')
        elif up_env and res:
            print('        → 大盘上涨找共振: 分散环境下做共振主线前排')
        elif up_env:
            print('        → 上涨但共振未形成: 观察, 不强行找共振')
        else:
            print('        → 杀跌且抱团未形成: 等抱团/低仓观察, 不动手')
    else:
        print(f'[大盘环境] {idx_warn} → 杀跌/共振判定跳过 (跑 sentiment_snapshot.py --update-index 更新)')
    # 3.5 资金合力(投机者76): 分散 + 溢价不升 = 无合力, 是最值钱的空仓信号(10/19 原话)
    no_heli = res and (prem is None or prem < HELI_PREM_MIN)
    if zt:
        if no_heli:
            print(f'[资金合力] top3 {top3_share}% ≤{RES_TOP3}%(分散) + 昨涨停溢价 {pr_txt} < {HELI_PREM_MIN}% '
                  '→ 无合力(老题材/新题材同拉, 谁都不强) → 降仓/空仓, 不排板')
        else:
            print(f'[资金合力] top3 {top3_share}% / 昨涨停溢价 {pr_txt} → 有合力, 按主线节奏做')
    # 4 主线
    print(f'[主线·既成] {y_ind if y_ok else "—"} 今日 {len(y_rec_t)} 家(昨 {y_n} 家)')
    if main_weak:
        print(f'        → 主线转弱({weak_reason}): 不碰支线, 不碰任何缩量板')
    elif y_ok:
        print('        → 主线延续: 聚焦前排核心辨识度, 常规打法')
    print(f'[主线·今日最强] {t_ind} {top1_n} 家 · 最高 {t_max} 板')
    if main_strong and main_weak:
        print(f'        → 主线切换信号({strong_reason}): 老主线退潮不接, 新主线关注首日核心')
    elif main_strong:
        if r['stage'] in ('冰点', '退潮') or no_heli:
            ctx = '冰点/退潮期' if r['stage'] in ('冰点', '退潮') else '资金无合力'
            print(f'        → 主线有转强苗头({strong_reason}), 但{ctx} → 先观察, 等修复/合力确认再打前排')
        else:
            print(f'        → 主线转强({strong_reason}): 闭眼买前排核心辨识度')
    elif theme_burst:
        print(f'        → 新题材爆发({t_ind} {top1_n} 家全 1 板): 梯队未成形, 观察次日晋级, 不抢首日 1 板')
    if t_recs:
        tops = sorted(t_recs, key=lambda x: -x['board'])[:5]
        print('         前排: ' + '  '.join(f'{x["name"]}({x["board"]}板)' for x in tops))
    # 4.6 板块分层: 龙头(最高板) / 连板 / 补涨(首板) —— 10.docx 选股框架
    if ind_rows_f is not None and len(ind_rows_f) and recT:
        layers = ind_rows_f.sort_values(['zt_n', 'industry'], ascending=[False, True]).head(3)
        print('[板块分层] 龙头 / 连板 / 补涨(首板):')
        for _, x in layers.iterrows():
            rows = [y for y in recT if y['industry'] == x['industry']]
            if not rows:
                continue
            lmax = max(y['board'] for y in rows)
            leaders = [y for y in rows if y['board'] == lmax]
            mids = sorted([y for y in rows if 1 < y['board'] < lmax],
                          key=lambda y: (-y['board'], y['name']))
            fbs = sorted([y for y in rows if y['board'] == 1],
                         key=lambda y: (-y['open_gap'], y['name']))
            if lmax == 1:
                seg = f'无梯队(全 {len(rows)} 家首板)'
            else:
                seg = '龙头 ' + ' '.join(f'{y["name"]}({y["board"]}板)' for y in leaders)
                if mids:
                    seg += ' | 连板 ' + ' '.join(f'{y["name"]}({y["board"]}板)' for y in mids[:5])
                seg += f' | 补涨首板 {len(fbs)} 家'
                if fbs:
                    seg += ': ' + ' '.join(f'{y["name"]}(开{y["open_gap"]:+.1f}%)' for y in fbs[:5])
            print(f'        {str(x["industry"]):<10} {seg}')
        print('        → 打板只打龙头; 补涨只做最先封板的那只; 后排 0 首板 = 题材结束')
        print('          (10.docx 竞价量比 1/20~1/10 为强; 日线无竞价量, 括号内为今日开盘涨幅近似)')
    # 4.5 板块温度: 昨涨停股今日溢价覆盖 / 跌停 / 最高标断板(10.docx + 日记判据)
    if ind_rows_f is not None and len(ind_rows_f):
        top = ind_rows_f.sort_values(['zt_n', 'zt_prev', 'industry'],
                                     ascending=[False, False, True]).head(5)
        warns, news, ok_ind = [], [], []
        print('[板块温度] 今日涨停行业(昨涨停股今日表现):')
        for _, x in top.iterrows():
            pr = f'{x["prem_avg"]:+.2f}%' if pd.notna(x['prem_avg']) else '—'
            cv = f'{x["pos_ratio"]:.0f}%' if pd.notna(x['pos_ratio']) else '—'
            fb = int(x['first_board_n']) if pd.notna(x['first_board_n']) else 0
            lb = bool(x['leader_break']) if pd.notna(x['leader_break']) else False
            ld = bool(x['leader_dt']) if pd.notna(x['leader_dt']) else False
            lead = ''
            if pd.notna(x['leader_board']) and x['leader_board'] >= SECTOR_LEADER_MIN:
                lead = (f' 昨龙头{str(x["leader_name"])}({int(x["leader_board"])}板)'
                        + ('今断板' if lb else ('今跌停' if ld else '今封板')))
            if x['zt_prev'] >= SECTOR_MIN_ZT and pd.notna(x['pos_ratio']):
                if lb and fb == 0:
                    tag = '题材结束'
                else:
                    bad = (x['pos_ratio'] < SECTOR_COV_LO or x['dt_next'] >= SECTOR_DT_HI
                           or (x['dt_next'] >= 1 and x['pos_ratio'] < SECTOR_COV_DT) or ld)
                    tag = '退潮预警' if bad else '健康(未退潮)'
                    if bad:
                        warns.append(str(x['industry']))
                    elif x['pos_ratio'] >= SECTOR_COV_LO:
                        ok_ind.append(str(x['industry']))
            elif x['zt_prev'] >= SECTOR_MIN_ZT:
                tag = '数据不足'
            elif x['zt_prev'] > 0:
                tag = '样本不足(昨涨停<3家)'
            elif x['zt_n'] >= 2 and x['zt_prev'] == 0 and pk.get(x['industry'], 0) < THEME_CLIMAX_ZT:
                tag = '新题材(昨无梯队·近期未高潮)'
                news.append(str(x['industry']))
            else:
                tag = '观察'
            print(f'        {str(x["industry"]):<10} 今{int(x["zt_n"])}家/昨{int(x["zt_prev"])}家 '
                  f'昨溢价{pr} 覆盖{cv} 跌停{int(x["dt_next"])}家 首板{fb}家{lead} → {tag}')
        ended_all = ind_rows_f[(ind_rows_f['zt_prev'] >= SECTOR_MIN_ZT)
                               & (ind_rows_f['leader_break'] == 1)
                               & (ind_rows_f['first_board_n'] == 0)]
        if len(ended_all):
            ended_all = ended_all.sort_values('zt_prev', ascending=False).head(5)
            txt = '、'.join(f'{x["industry"]}({x["leader_name"]}{int(x["leader_board"])}板断板)'
                            for _, x in ended_all.iterrows())
            print(f'        → 题材结束(最高标断板 + 后排 0 首板): {txt} → 当日禁做该板块(打板/低吸都不做)')
        if warns:
            print(f'        → 板块退潮信号: {"、".join(warns)} 昨日涨停股今日无溢价覆盖/出跌停, 该板块不接力')
        if ok_ind:
            print(f'        → 板块未退潮: {"、".join(ok_ind)} 昨涨停股今日过半有溢价, 该板块仍可接力')
        if news:
            print(f'        → 新题材承接: {"、".join(news)} (昨日无梯队, 今日首板成群, 观察次日晋级)')
    # 4.7 题材温度(概念口径): 机器人/算力等题材横跨行业, 行业健康不代表题材健康
    if con_rows_f is not None and len(con_rows_f):
        tc = con_rows_f[con_rows_f['zt_prev'] >= SECTOR_MIN_ZT].sort_values(
            ['zt_prev', 'zt_n', 'concept'], ascending=[False, False, True]).head(6)
        if len(tc):
            print('[题材温度] 昨日有涨停梯队的题材(概念口径):')
            warn_c, ok_c, end_c = [], [], []
            for _, x in tc.iterrows():
                pr = f'{x["prem_avg"]:+.2f}%' if pd.notna(x['prem_avg']) else '—'
                cv = f'{x["pos_ratio"]:.0f}%' if pd.notna(x['pos_ratio']) else '—'
                fb = int(x['first_board_n']) if pd.notna(x['first_board_n']) else 0
                lb = bool(x['leader_break']) if pd.notna(x['leader_break']) else False
                ld = bool(x['leader_dt']) if pd.notna(x['leader_dt']) else False
                lead = ''
                if pd.notna(x['leader_board']) and x['leader_board'] >= SECTOR_LEADER_MIN:
                    lead = (f' 昨龙头{str(x["leader_name"])}({int(x["leader_board"])}板)'
                            + ('今断板' if lb else ('今跌停' if ld else '今封板')))
                if lb and fb == 0:
                    tag = '题材结束'
                    end_c.append(str(x['concept']))
                else:
                    bad = (x['pos_ratio'] < SECTOR_COV_LO or x['dt_next'] >= SECTOR_DT_HI
                           or (x['dt_next'] >= 1 and x['pos_ratio'] < SECTOR_COV_DT) or ld
                           or lb)
                    tag = '退潮预警' if bad else '健康(未退潮)'
                    (warn_c if bad else ok_c).append(str(x['concept']))
                print(f'        {str(x["concept"]):<12} 今{int(x["zt_n"])}家/昨{int(x["zt_prev"])}家 '
                      f'昨溢价{pr} 覆盖{cv} 跌停{int(x["dt_next"])}家 首板{fb}家{lead} → {tag}')
            if end_c:
                print(f'        → 题材结束: {"、".join(end_c)} → 当日禁做该题材(打板/低吸都不做)')
            if warn_c:
                print(f'        → 题材退潮: {"、".join(warn_c)} 昨涨停股今日无溢价覆盖, 该题材不接力')
            if ok_c:
                print(f'        → 题材未退潮: {"、".join(ok_c)} 昨涨停股今日过半有溢价')
            print('        → 个股板块判定 = 行业 ∪ 所属题材 取较差者: 行业健康但题材已退潮, 按题材算')
    # 5 龙头梯队
    if dragons:
        print(f'[市场龙头] {"  ".join(f"{x["name"]}({x["board"]}板)" for x in dragons)}')
        print('        → 板块龙头打板、市场龙头低吸(断板前达到 5 板才算龙头)')
    else:
        print('[市场龙头] 无 5 板+ 龙头(最高板未达标, 不配称龙头)')
    # 6 断板观察
    if broken_dragons:
        print(f'[断板观察] 昨日 5 板+ 今日断板: {"  ".join(f"{x["name"]}(昨{x["board"]}板)" for x in broken_dragons)}')
        print('        → 龙头断板, 主线有转弱风险; 观察反包, 不宜接力')
    else:
        print('[断板观察] 无')
    # 6.5 分歧日新题材首板: 高标分歧/退潮期资金切低位新题材(昨日无梯队, 今日首板成群)
    if r.get('diverge') or r['stage'] == '退潮':
        if ind_rows_f is not None and len(ind_rows_f):
            fan = ind_rows_f[(ind_rows_f['zt_prev'] == 0) & (ind_rows_f['zt_n'] >= 2)
                             & (ind_rows_f['max_board'] == 1)]
            # "新题材"需近期未高潮: 该行业近60个交易日无单日 >=5 家涨停(刚炒过又歇一天的旧题材排除)
            fan = fan[fan['industry'].map(lambda nm: pk.get(nm, 0) < THEME_CLIMAX_ZT)]
            fan = fan.sort_values(['zt_n', 'industry'], ascending=[False, True]).head(3)
        else:
            fan = pd.DataFrame()
        if len(fan):
            print('[分歧日新题材首板] 资金切低位新题材(昨日无涨停梯队, 今日 2 家+ 全 1 板, '
                  f'近{THEME_CLIMAX_LOOKBACK}日无>={THEME_CLIMAX_ZT}家涨停潮):')
            for _, x in fan.iterrows():
                names = [y['name'] for y in recT
                         if y['industry'] == x['industry'] and y['board'] == 1][:5]
                print(f'        {str(x["industry"]):<10} {int(x["zt_n"])}家/昨0家 首板: '
                      + '  '.join(names))
            print('        → 只做梯队里最先封板的辨识度, 次日看竞价强弱定去留')
        else:
            print('[分歧日新题材首板] 今日无(无 2 家+ 全新题材且全 1 板)')
    # 6.7 兑现纪律(投机者76): 冲高不卖 / 炸板不走 / 高位断板不接
    if fade or hi_break:
        print('[兑现纪律] 冲高回落 / 高位断板:')
        if fade:
            print('        昨涨停股今日冲高回落: ' + '  '.join(
                f'{x["name"]}(昨{x["board"]}板 高{s["high_gap"]:+.1f}% 收{s["pct"]:+.1f}% 回落{gap:.1f}pct)'
                for x, s, gap in fade[:5]))
        if hi_break:
            print('        高位断板(昨≥4板 今日未封): ' + '  '.join(
                f'{x["name"]}(昨{x["board"]}板)' for x in hi_break[:5]))
        print('        → 冲高回落 ≥5pct 无条件减仓(10/28 上海九百冲高 9 个点没卖); '
              '高位断板不接反包不补仓; 炸板当天必须走')
    else:
        print('[兑现纪律] 无冲高回落/高位断板, 兑现反馈正常')
    # 7 禁区(3 板加速票 + 4 板以上高位段)
    if jasun:
        print(f'[禁区·加速票] 3 板一字/缩量一概不接: '
              f'{"  ".join(f"{x["name"]}({x["board"]}板{"一字" if x["yizi"] else "缩量"})" for x in jasun)}')
    else:
        print('[禁区·加速票] 无 3 板一字/缩量加速票')
    if hi_acc:
        top_hi = [x for x in hi_acc if x['board'] == bb]
        rest_hi = [x for x in hi_acc if x['board'] < bb]
        if top_hi:
            print(f'[高位段] {bb} 板最高标: {"  ".join(f"{x["name"]}({x["board"]}板)" for x in top_hi)}'
                  f' → 只可按市场龙头低吸, 单票 ≤{HIGH_POS_CAP}%, 不追板')
            if len(top_hi) > 1:
                print('        → 最高标 PK 未决(如大业 vs 京城): 不参与, 等竞价/换手分出胜负')
        if rest_hi:
            print(f'[高位段] 4 板+ 非最高标: {"  ".join(f"{x["name"]}({x["board"]}板)" for x in rest_hi)}'
                  ' → 一概不接(传智第 8 板/国芳第 5 板/光洋第 4 板次日都炸板)')
    if first_weaken:
        print('[禁区·拐点] 情绪转弱第一天 → 当天杜绝一切盘中涨停板')
    if main_weak:
        print('[禁区·主线] 主线转弱 → 不碰支线、不碰任何缩量板')
    # 8 超预期
    if over_beyond:
        print('[超预期] 情绪超预期(溢价/温度突破) → 不看主板跟风, 观察创业板弹性')
    # 9 仓位
    print(f'[仓位] {r["stage"]} → {POSITION[r["stage"]]}')
    print(f'        → 单票 ≤{SINGLE_POS_CAP}% 总资产, 高位票(≥{ACC_HIGH_BOARD}板) ≤{HIGH_POS_CAP}%; '
          '禁止满仓单票隔夜(10/29 光洋 98.3%, 一夜 -2.3%)')
    # 9b 打板排序门控(zt_rank.py 口径: 仅 修复/发酵/高潮 启用横截面排序)
    gate_on = r['stage'] in ('修复', '发酵', '高潮')
    print(f'[打板排序] 阶段 {r["stage"]} → '
          + ('启用, 当日名单见 python src\\zt_rank.py'
             if gate_on else '关闭(冰点/退潮不做横截面打板排序, 打板只在转强第一时间窗口)'))
    # 10 异动反馈
    feed = []
    if prem is not None and prem < 0:
        feed.append(f'昨涨停溢价转负({prem:.2f}%)打板客在亏钱')
    if r['zha'] > 0 and r['zha'] >= zt * 0.5:
        feed.append(f'炸板 {r["zha"]} 家(≥涨停一半)封板质量差')
    if r['dt'] >= 20:
        feed.append(f'跌停 {r["dt"]} 家恐慌加剧')
    print(f'[异动反馈] ' + ('、'.join(feed) if feed else '今日反馈正常, 继续紧盯上述指标变化'))
    print('=' * 70)
    print(f'近 5 日: ' + '  '.join(
        f'{x["date"][5:]} {x["stage"]}{x["temp"]}' for _, x in df.iloc[max(0, i_f - 4):i_f + 1].iterrows()))
    print()


if __name__ == '__main__':
    main()