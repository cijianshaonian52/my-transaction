# -*- coding: utf-8 -*-
"""全市场情绪快照 —— 由日线数据合成每日短线情绪指标序列

口径(全部剔除 ST/*退/上市前5日, 涨停阈值沿用 first_board_premium 口径):
  涨停      收盘涨幅 >= 阈值(主板9.8/双创19.8/北交29.8/ST4.8, 即涨停价留0.2pct容差)
  炸板      盘中最高触及涨停阈值, 收盘未封住(从涨停板上掉下来的股票)
  封板率    涨停家数 / (涨停家数 + 炸板家数)
  昨涨停溢价 昨日涨停股今日平均收盘涨跌幅 (赚钱效应核心)
  晋级率    昨日涨停股今日再次涨停比例
  最高板    全市场最高连板数(连续涨停天数)
  连板家数  连板 >= 2 的家数
  跌停      收盘跌幅 <= -阈值
  一字板    开盘涨幅即达阈值(开盘即封)
  缩量板    涨停且当日成交量 < 前一日
  温度分    以上核心项加权合成 0-100(权重见常量区), 扣跌停惩罚
  阶段      覆盖规则: 退潮 = 溢价<=-1.5%(立即生效); 温度<=20 立即冰点(风控不等确认)
            其余按温度分 <30 冰点 / 30-45 修复 / 45-70 发酵 / >=70 高潮
            并加滞回(跨档需越过边界4分)+最短持有3日, 抑制日频抖动(585天切换由379次降至200次)
            高标分歧(最高板>=4 且封板率<62%)不再覆盖阶段, 作为并列标签 diverge 输出
  温度分位  temp_pct = 当日温度在近 120 个交易日(至少 60 日历史)中的百分位, 0 最冷 / 100 最热
            仅作参考列(判断当前温度相对近半年偏冷偏热), 不参与任何规则/门控
  行业级    zt_n/zt_prev/prem_avg/pos_ratio/dt_next/promo_ratio/max_board
            + first_board_n(当日首板家数) + leader(昨日该行业最高标, >=3板)
              leader_break(今日未封) / leader_dt(今日跌停)
  概念级    同行业级口径, 但按同花顺概念(题材)成分聚合。原因: 机器人/算力这类题材横跨多个
            CSRC 行业(如机器人 = C34 通用设备 + I65 软件), 行业口径切不开题材,
            会出现"行业健康但题材已崩"的误判(002943 宇晶 10-29 案例)。
            行业 ∪ 概念 取较差者, 才是对个股有效的板块退潮判定。

用法:
  .venv\\Scripts\\python.exe src\\sentiment_snapshot.py                        # 默认 stockdb_csv, 2026 全年
  .venv\\Scripts\\python.exe src\\sentiment_snapshot.py --dir stockdb_csv --start 2025-10-01
  .venv\\Scripts\\python.exe src\\sentiment_snapshot.py --date 2026-10-28      # 只看某日行 + 邻近日轨迹
输出: output/sentiment_daily.csv(全窗口序列) + output/sentiment_industry.csv(行业级)
      + output/sentiment_concept.csv(概念级) + 控制台报告
"""
import os, sys, io, glob, argparse, datetime, re
from collections import Counter
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR_DEFAULT = 'stockdb_csv'          # 当前最新数据源（前复权逐股 CSV）
IND_FILE = os.path.join(ROOT, 'cache_data', '_stock_industry.csv')
CONCEPT_FILE = os.path.join(ROOT, 'cache_data', '_stock_concept.csv')   # 同花顺概念成分
IDX_FILE = os.path.join(ROOT, 'idx_data', '000300.SH.csv')
OUT = os.path.join(ROOT, 'output', 'sentiment_daily.csv')
INDUSTRY_OUT = os.path.join(ROOT, 'output', 'sentiment_industry.csv')
CONCEPT_OUT = os.path.join(ROOT, 'output', 'sentiment_concept.csv')

MIN_ACTIVE = 1500          # 当日有行情记录的最少股票数, 低于视为非完整交易日
# —— 温度分映射与权重(可调) ——
BREADTH_FULL = 60          # 涨停家数 60 家 → 满分
SEAL_LO, SEAL_HI = 50, 85  # 封板率 50%→0分, 85%→满分
PREM_LO, PREM_HI = -2.0, 4.0    # 昨涨停溢价 -2%→0分, +4%→满分
PROM_LO, PROM_HI = 15, 45  # 晋级率 15%→0分, 45%→满分
BOARD_FULL = 8             # 最高板 8 板 → 满分
DT_REF, DT_PENALTY = 50, 25      # 跌停 50 家 → 扣满 25 分
W_B, W_S, W_P, W_G, W_H = 0.22, 0.16, 0.26, 0.16, 0.20
# —— 阶段判定 ——
RETREAT_PREM = -1.5        # 溢价 <= 该值 → 退潮(最高优先级)
DIVERGE_BOARD = 4          # 高标分歧标签: 最高板 >= 该值 且 封板率 < 62% → diverge=1
STAGE_HYST = 4             # 阶段滞回: 跨档需越过边界 N 分, 抑制阈值附近抖动
STAGE_MIN_HOLD = 3         # 阶段最短持有交易日(退潮/暴跌冰点不受限)
T_ICE_HARD = 20            # 温度 <= 该值立即判冰点, 不等最短持有(风控优先)
DIVERGE_SEAL = 62
T_ICE, T_REPAIR, T_FERM, T_HIGH = 30, 45, 70, 100
# —— 板块退潮(行业级) ——
SECTOR_MIN_ZT = 3          # 昨日涨停 >= 3 家才判该板块
SECTOR_COV_LO = 50         # 覆盖率 < 50% → 预警
SECTOR_COV_DT = 60         # 有 1 家跌停时, 覆盖率 < 60% 才预警(单个跌停不误报)
SECTOR_DT_HI = 2           # 跌停 >= 2 家 → 预警
SECTOR_LEADER_MIN = 3      # 板块最高标: 昨日该行业最高板 >= 3 才跟踪断板/跌停
# —— 概念成分过滤(沿用 fb_pullback_closebuy 口径) ——
# 这些"概念"是资金属性/持仓/牌照/宽泛政策/生态标签, 聚合出来常是伪题材或大盘镜像;
# 作为"最强题材"时双窗口跑输可买基准(文化传媒/腾讯/AI应用因伤最近一月暂未列入)
CONCEPT_JUNK = ('股通', '融资', '融券', '罗素', 'MSCI', '标普', '富时', '转融', 'ST板块')
CONCEPT_BLACKLIST = (
    '华为', '新能源', '国企改革', 'DeepSeek', '专精特新', '贬值', '预增',
    '预亏', '股权转让', '并购重组', '高股息', '破净', '创投', '一带一路',
    '西部大开发', '粤港澳', '乡村振兴', '海峡两岸', '京津冀',
    '证金持股', '基金重仓', '参股银行', '参股券商', '参股保险', '信托概念',
    '国家大基金持股', '村镇银行', '金控平台', '互联网保险', '互联网券商',
    '参股外资金融', 'T+0基金', '基金三方销售', '科技保险',
    '碳中和', '工业互联网', 'AI智能体', '无人机', '智能医疗', '智能电网',
    '数据要素', '虚拟现实', '网红经济', '统一大市场', '雄安新区',
    '抖音概念', '特斯拉概念',
)


def limit_threshold(code6, name):
    """涨停判定阈值, 同 first_board_premium 口径(0.2pct 容差)"""
    if code6.startswith(('300', '301', '688', '689')):
        return 19.8
    if code6.startswith(('43', '83', '87', '920')):
        return 29.8
    if name and 'ST' in str(name).upper():
        return 4.8
    return 9.8


def load_stock(fp):
    """返回 (名称, 日期/开/高/低/收/量 列表); 兼容 7 或 8 字段的行"""
    try:
        lines = open(fp, encoding='gbk', errors='ignore').read().splitlines()
    except Exception:
        return '', [], [], [], [], [], []
    name = ''
    if lines:
        parts = lines[0].strip().split()
        if len(parts) >= 2 and parts[0][:4].isdigit():
            name = parts[1]
    d, o, h, lo, c, v = [], [], [], [], [], []
    for ln in lines[1:]:
        p = ln.split(',')
        if len(p) not in (7, 8):
            continue
        if not (p[0][:4].isdigit() and '-' in p[0]):
            continue
        try:
            d.append(p[0]); o.append(float(p[1])); h.append(float(p[2]))
            lo.append(float(p[3])); c.append(float(p[4])); v.append(float(p[5]))
        except ValueError:
            continue
    return name, d, o, h, lo, c, v


def load_index():
    """idx_data/000300.SH.csv → {date: close}"""
    closes = {}
    try:
        lines = open(IDX_FILE, encoding='utf-8', errors='ignore').read().splitlines()
    except Exception:
        return closes
    for ln in lines:
        p = ln.split(',')
        if len(p) >= 6 and p[0][:4].isdigit() and '-' in p[0]:
            try:
                closes[p[0]] = float(p[4])
            except ValueError:
                pass
    return closes


def update_index():
    """baostock 拉取沪深300 日线覆盖 idx_data/000300.SH.csv(同 update_index_daily 套路)"""
    import baostock as bs
    bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            'sh.000300', 'date,open,high,low,close,volume',
            start_date='2021-08-01', end_date='2026-12-31', frequency='d')
        rows = []
        while rs.error_code == '0' and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            print('!! baostock 沪深300 无返回, 沿用旧指数文件')
            return
        with open(IDX_FILE, 'w', encoding='utf-8') as f:
            f.write('date,open,high,low,close,volume\n')
            for r_ in rows:
                f.write(','.join(r_) + '\n')
        print(f'指数已更新: 至 {rows[-1][0]}')
    finally:
        bs.logout()


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def load_concepts():
    """cache_data/_stock_concept.csv → {code6: [概念, ...]}; 剔除非题材类概念"""
    if not os.path.exists(CONCEPT_FILE):
        print(f'!! 缺 {CONCEPT_FILE} → 概念维度跳过, 仅行业口径')
        return {}
    cp = pd.read_csv(CONCEPT_FILE, dtype=str)
    cp = cp.dropna(subset=['ts_code', 'concept'])
    cp = cp[~cp['concept'].str.contains('|'.join(re.escape(k) for k in CONCEPT_JUNK), na=False)]
    cp = cp[~cp['concept'].str.contains('|'.join(re.escape(k) for k in CONCEPT_BLACKLIST), na=False)]
    cp['c6'] = cp['ts_code'].str.split('.').str[0]
    mp = cp.groupby('c6')['concept'].apply(lambda s: sorted(set(s))).to_dict()
    print(f'概念成分 {CONCEPT_FILE.split(os.sep)[-1]}: {len(mp)} 只股票, '
          f'{cp["concept"].nunique()} 个概念(已剔除资金/风格/宽泛类)')
    return mp


def add_sec(b, key, sname, board, code6, name, npct, thr):
    """把一只涨停股计入某口径板块的当日聚合桶(key='ind' 行业 / 'con' 概念)"""
    b[key][sname] += 1
    b[key + '_max'][sname] = max(b[key + '_max'].get(sname, 0), board)
    if board == 1:
        b[key + '_fb'][sname] += 1                     # 当日首板(补涨)家数
    if board >= SECTOR_LEADER_MIN:                     # 该板块最高标(>=3板)
        cur = b[key + '_lead'].get(sname)
        if cur is None or board > cur[1]:
            b[key + '_lead'][sname] = (code6, board, name)
    if npct is not None:                               # 次日收盘溢价(落在次日行)
        rec = b[key + '_next'].setdefault(sname, {'prem': [], 'dt': 0, 'promo': 0})
        rec['prem'].append(npct)
        if npct <= -thr:
            rec['dt'] += 1
        if npct >= thr:
            rec['promo'] += 1


def build_sec_rows(T, b, prev, key, label):
    """当日某口径(行业 key='ind' / 概念 key='con')的板块级记录"""
    cur_cnt, cur_max, cur_fb = b[key], b[key + '_max'], b[key + '_fb']
    p_cnt = prev[key] if prev else Counter()
    p_next = prev[key + '_next'] if prev else {}
    p_lead = prev[key + '_lead'] if prev else {}
    rows = []
    for nm in sorted(set(cur_cnt) | set(p_cnt)):
        n_t, n_p = cur_cnt.get(nm, 0), p_cnt.get(nm, 0)
        rec = p_next.get(nm)
        prem_avg = pos = promo = ''
        dt_n = 0
        if rec and rec['prem']:
            m = len(rec['prem'])
            prem_avg = round(sum(rec['prem']) / m, 2)
            pos = round(sum(1 for x in rec['prem'] if x > 0) / m * 100, 1)   # 溢价覆盖率
            promo = round(rec['promo'] / m * 100, 1)
            dt_n = rec['dt']
        lead = p_lead.get(nm)
        rows.append({'date': T, label: nm, 'zt_n': n_t, 'zt_prev': n_p,
                     'prem_avg': prem_avg, 'pos_ratio': pos, 'dt_next': dt_n,
                     'promo_ratio': promo,
                     'max_board': cur_max.get(nm, 0),
                     'first_board_n': cur_fb.get(nm, 0),
                     'leader_name': lead[2] if lead else '',
                     'leader_board': lead[1] if lead else 0,
                     'leader_break': (0 if lead[0] in b['zt_code'] else 1) if lead else 0,
                     'leader_dt': (1 if lead[0] in b['dt_code'] else 0) if lead else 0})
    return rows


def sector_tag(x):
    """板块退潮标签(行业/概念同口径)"""
    if x['zt_prev'] >= SECTOR_MIN_ZT and x['pos_ratio'] != '':
        if x['leader_break'] and x['first_board_n'] == 0:
            return '题材结束(最高标断板+0首板)'
        bad = (x['pos_ratio'] < SECTOR_COV_LO or x['dt_next'] >= SECTOR_DT_HI
               or (x['dt_next'] >= 1 and x['pos_ratio'] < SECTOR_COV_DT) or x['leader_dt'])
        return '退潮预警' if bad else '健康(未退潮)'
    if x['zt_prev'] >= SECTOR_MIN_ZT:
        return '数据不足'
    return '样本不足(昨涨停<3家)'


def sector_line(x, label_key):
    """板块行文本"""
    pr = f'{x["prem_avg"]:+.2f}%' if x['prem_avg'] != '' else '—'
    cv = f'{x["pos_ratio"]:.0f}%' if x['pos_ratio'] != '' else '—'
    lead = ''
    if x['leader_board'] >= SECTOR_LEADER_MIN:
        lead = (f' 龙头{x["leader_name"]}({int(x["leader_board"])}板)'
                + ('断板' if x['leader_break'] else ('跌停' if x['leader_dt'] else '封板')))
    return (f'  {str(x[label_key]):<10} 今{int(x["zt_n"])}家/昨{int(x["zt_prev"])}家 '
            f'昨溢价{pr} 覆盖{cv} 昨涨今跌停{int(x["dt_next"])}家 首板{int(x["first_board_n"])}家'
            f'{lead} → {sector_tag(x)}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=DATA_DIR_DEFAULT, help='数据目录 stockdb_csv(默认)/cache_data')
    ap.add_argument('--start', default='2026-01-01', help='输出序列起始日期')
    ap.add_argument('--date', default=None, help='只看某日行 + 邻近日轨迹(仍写全序列)')
    ap.add_argument('--update-index', action='store_true', help='先经 baostock 更新沪深300 指数')
    args = ap.parse_args()
    data_dir = os.path.join(ROOT, args.dir)
    if args.update_index:
        update_index()

    ind = pd.read_csv(IND_FILE, dtype=str)
    ind['code6'] = ind['ts_code'].str.split('.').str[0]
    name_map = ind.set_index('code6')['name'].to_dict()
    ind_map = ind.set_index('code6')['industry'].to_dict()
    con_map = load_concepts()

    files = sorted([f for f in os.listdir(data_dir) if f.endswith('.csv')])
    print(f'数据源 {args.dir}: {len(files)} 只, 起算 {args.start}')
    if args.date:
        print(f'--date {args.date}: 仅展示该日附近轨迹, 序列照常写入')

    # days[date] = 聚合桶
    days = {}
    def bucket(dt):
        b = days.get(dt)
        if b is None:
            b = {'zt': 0, 'zha': 0, 'dt': 0, 'lb2': 0, 'yizi': 0, 'suoliang': 0,
                 'board': 0, 'active': 0, 'premium': [], 'promote': [], 'ind': Counter(),
                 'ind_max': {}, 'ind_next': {}, 'ind_fb': Counter(), 'ind_lead': {},
                 'con': Counter(), 'con_max': {}, 'con_next': {}, 'con_fb': Counter(),
                 'con_lead': {},
                 'zt_code': set(), 'dt_code': set()}
            days[dt] = b
        return b

    for fn in files:
        code6 = fn.split('#')[1].split('.')[0]
        name, d, o, h, lo, c, v = load_stock(os.path.join(data_dir, fn))
        if len(d) < 6:
            continue
        name = name_map.get(code6) or name           # 个股文件表头名称可能被截断, 优先用行业表全称
        if not name:
            continue
        nm = str(name).upper()
        if 'ST' in nm or '退' in nm:
            continue
        thr = limit_threshold(code6, name)
        board = 0
        for i in range(5, len(d)):            # 上市前 5 日不计
            pct = (c[i] / c[i - 1] - 1) * 100
            b = bucket(d[i])
            b['active'] += 1
            is_zt = pct >= thr
            if is_zt:
                board += 1
                b['zt'] += 1
                b['board'] = max(b['board'], board)
                if board >= 2:
                    b['lb2'] += 1
                if (o[i] / c[i - 1] - 1) * 100 >= thr:
                    b['yizi'] += 1
                if v[i] < v[i - 1]:
                    b['suoliang'] += 1
                ind_name = ind_map.get(code6, '未知') or '未知'
                b['zt_code'].add(code6)
                npct = (c[i + 1] / c[i] - 1) * 100 if i + 1 < len(d) else None
                if npct is not None:
                    b['premium'].append(npct)                         # 次日收盘溢价, 落在次日行
                    b['promote'].append(1 if npct >= thr else 0)
                # 板块口径 = 行业 ∪ 概念(题材): 概念横跨 CSRC 行业, 是退潮判定的真实粒度
                add_sec(b, 'ind', ind_name, board, code6, name, npct, thr)
                for cn in con_map.get(code6, ()):
                    add_sec(b, 'con', cn, board, code6, name, npct, thr)
            else:
                board = 0
                if (h[i] / c[i - 1] - 1) * 100 >= thr:
                    b['zha'] += 1                                       # 盘中触及涨停未封住
                if pct <= -thr:
                    b['dt'] += 1
                    b['dt_code'].add(code6)

    idx_close = load_index()
    # 完整交易日序列(按日期排序, 活跃股票数达标)
    full_dates = sorted(d for d, b in days.items() if b['active'] >= MIN_ACTIVE)

    rows = []
    ind_rows = []
    con_rows = []
    prev_date = None
    prev_stage, hold = None, 0
    STAGE_RANK = {'冰点': 0, '修复': 1, '发酵': 2, '高潮': 3, '退潮': -1}
    STAGE_LO = {'冰点': 0, '修复': T_ICE, '发酵': T_REPAIR, '高潮': T_FERM}
    for T in full_dates:
        b = days[T]
        seal = b['zt'] / (b['zt'] + b['zha']) * 100 if (b['zt'] + b['zha']) else float('nan')
        if prev_date is not None:
            parts = days[prev_date]['premium']
            promo = days[prev_date]['promote']
            premium = sum(parts) / len(parts) if parts else float('nan')
            promote = sum(promo) / len(promo) * 100 if promo else float('nan')
        else:
            premium = promote = float('nan')
        zt = b['zt']
        bb = b['board']
        # 温度分
        s_b = min(zt / BREADTH_FULL, 1.0) * 100
        s_s = clip((seal - SEAL_LO) / (SEAL_HI - SEAL_LO), 0, 1) * 100 if seal == seal else 0
        s_p = clip((premium - PREM_LO) / (PREM_HI - PREM_LO), 0, 1) * 100 if premium == premium else 0
        s_g = clip((promote - PROM_LO) / (PROM_HI - PROM_LO), 0, 1) * 100 if promote == promote else 0
        s_h = min(bb / BOARD_FULL, 1.0) * 100
        temp = W_B * s_b + W_S * s_s + W_P * s_p + W_G * s_g + W_H * s_h \
               - min(b['dt'] / DT_REF, 1.0) * DT_PENALTY
        temp = round(temp)
        # 阶段: 退潮(溢价)/暴跌冰点 立即生效; 其余档位加滞回(边界±4分)与最短持有3日,
        # 避免温度分在阈值附近抖动导致阶段日频翻转。高标分歧只作并列标签, 不覆盖阶段
        # (旧口径把 max_board>=4 且封板率<62% 改判为"分歧", 吞掉了 111 天冰点/修复,
        #  其中温度 3~86 分都叫分歧, 语义失效且压掉"冰点/修复→转强第一天"窗口)
        if premium == premium and premium <= RETREAT_PREM:
            stage = '退潮'
        elif temp <= T_ICE_HARD:
            stage = '冰点'
        else:
            cand = ('高潮' if temp >= T_FERM else '发酵' if temp >= T_REPAIR
                    else '修复' if temp >= T_ICE else '冰点')
            if prev_stage is None or prev_stage == '退潮' or cand == prev_stage:
                stage = cand
            elif (prev_stage in ('冰点', '高潮')
                  and abs(STAGE_RANK[cand] - STAGE_RANK[prev_stage]) >= 2):
                stage = cand          # 极端档跨两档跳变(如冰点->发酵) 立即改判, 避免"冰点63分"错标
            elif hold < STAGE_MIN_HOLD:
                stage = prev_stage
            else:
                up = STAGE_RANK[cand] > STAGE_RANK[prev_stage]
                ok = (temp >= STAGE_LO[cand] + STAGE_HYST if up
                      else temp <= STAGE_LO[prev_stage] - STAGE_HYST)
                stage = cand if ok else prev_stage
        hold = hold + 1 if stage == prev_stage else 1
        prev_stage = stage
        diverge = 1 if (bb >= DIVERGE_BOARD and seal == seal and seal < DIVERGE_SEAL) else 0
        ind_c = b['ind']
        top1 = ind_c.most_common(1)[0] if ind_c else ('', 0)
        top3_n = sum(n for _, n in ind_c.most_common(3))
        rows.append({
            'date': T, 'zt': zt, 'zha': b['zha'], 'seal': round(seal, 1),
            'premium': round(premium, 2) if premium == premium else '',
            'promote': round(promote, 1) if promote == promote else '',
            'max_board': bb, 'lb2': b['lb2'], 'dt': b['dt'],
            'yizi': b['yizi'], 'suoliang': b['suoliang'],
            'top1_ind': top1[0], 'top1_n': top1[1],
            'top3_share': round(top3_n / zt * 100, 1) if zt else '',
            'temp': temp, 'stage': stage, 'diverge': diverge,
            'hs300': idx_close.get(T, float('nan')),
            'hs300_ma20': float('nan'),
            'active': b['active'],
        })
        # —— 行业级/概念级聚合: 昨涨停股今日表现(溢价/覆盖率/跌停) ——
        prev = days[prev_date] if prev_date else None
        ind_rows.extend(build_sec_rows(T, b, prev, 'ind', 'industry'))
        con_rows.extend(build_sec_rows(T, b, prev, 'con', 'concept'))
        prev_date = T

    if not rows:
        print('无有效交易日数据'); return
    df = pd.DataFrame(rows)
    # HS300 MA20(交易日序列滚动)
    s = df['hs300'].astype(float)
    df['hs300_ma20'] = s.rolling(20, min_periods=20).mean().round(1)
    # 温度相对分位: 当日温度在最近 120 个交易日(含当日, 至少 60 日历史)中的百分位
    # 0 = 近半年最冷 / 100 = 最热; 仅作参考列, 不参与阶段/门控/仓位判定
    def _temp_pct(w):
        if len(w) < 2:
            return float('nan')
        return round(float((w[:-1] < w[-1]).mean()) * 100, 1)
    df['temp_pct'] = df['temp'].rolling(120, min_periods=60).apply(_temp_pct, raw=True)
    # 仅输出 --start 之后的序列
    out_df = df[df['date'] >= args.start].reset_index(drop=True)
    out_df.to_csv(OUT, index=False, encoding='utf-8-sig')
    # 行业级序列(仅输出窗口内的日期)
    ind_df = pd.DataFrame(ind_rows)
    ind_df = ind_df[ind_df['date'] >= args.start].reset_index(drop=True) if not ind_df.empty else ind_df
    ind_df.to_csv(INDUSTRY_OUT, index=False, encoding='utf-8-sig')
    con_df = pd.DataFrame(con_rows)
    con_df = con_df[con_df['date'] >= args.start].reset_index(drop=True) if not con_df.empty else con_df
    con_df.to_csv(CONCEPT_OUT, index=False, encoding='utf-8-sig')

    # —— 控制台报告 ——
    focus = args.date if args.date else out_df.iloc[-1]['date']
    if focus not in out_df['date'].values:
        print(f'!! {focus} 不在输出序列内(可能停牌日/非交易日), 展示最新交易日')
        focus = out_df.iloc[-1]['date']
    i_f = out_df.index[out_df['date'] == focus][0]
    r = out_df.iloc[i_f]
    print()
    print('=' * 66)
    print(f' 情绪快照 {r["date"]}  (数据源 {args.dir}, 最新交易日 {out_df.iloc[-1]["date"]})')
    print('=' * 66)
    print(f'阶段 {r["stage"]} | 温度 {r["temp"]} 分 | 涨停 {r["zt"]} 家 | 炸板 {r["zha"]} 家 | '
          f'封板率 {r["seal"]}%')
    print(f'昨涨停溢价 {"—" if r["premium"] == "" else f"{r["premium"]}%"} | 晋级率 '
          f'{"—" if r["promote"] == "" else f"{r["promote"]}%"} | 最高板 {r["max_board"]} | '
          f'连板 {r["lb2"]} 家 | 跌停 {r["dt"]} 家')
    print(f'一字板 {r["yizi"]} 家 | 缩量板 {r["suoliang"]} 家 | 题材 top1 '
          f'{r["top1_ind"] or "—"} {r["top1_n"] or 0} 家 | top3 占比 '
          f'{"—" if r["top3_share"] == "" else f"{r["top3_share"]}%"}')
    hstxt = (f'HS300 {r["hs300"]:.0f} (MA20 {r["hs300_ma20"]:.0f})\n'
             if r['hs300'] == r['hs300'] and r['hs300_ma20'] == r['hs300_ma20']
             else 'HS300 指数数据缺失/未更新 → 大盘判定跳过, 可加 --update-index 更新\n')
    print(hstxt)
    print('-' * 66)
    if not ind_df.empty:
        fr = ind_df[(ind_df['date'] == focus)
                    & ((ind_df['zt_n'] >= 3) | (ind_df['zt_prev'] >= 3))]
        fr = fr.sort_values(['zt_n', 'zt_prev', 'industry'],
                            ascending=[False, False, True]).head(6)
        if len(fr):
            warn_inds = []
            print(f'[板块退潮] {focus} 涨停行业(昨日涨停股今日表现):')
            for _, x in fr.iterrows():
                print(sector_line(x, 'industry'))
                if sector_tag(x).startswith(('题材结束', '退潮预警')):
                    warn_inds.append(x['industry'])
            if warn_inds:
                print('        → 板块退潮信号: ' + '、'.join(warn_inds) +
                      ' 昨日涨停股今日无溢价覆盖/出跌停, 该板块不接力')
            else:
                print('        → 板块溢价覆盖健康, 无板块级退潮信号')
        else:
            print('[板块退潮] 今日无成规模涨停行业(≥3 家), 无板块级信号')
    # —— 概念级: 题材横跨行业, 行业健康不代表题材健康(宇晶 10-29 案例) ——
    if not con_df.empty:
        fc = con_df[(con_df['date'] == focus) & (con_df['zt_prev'] >= SECTOR_MIN_ZT)]
        fc = fc.sort_values(['zt_prev', 'zt_n', 'concept'],
                            ascending=[False, False, True]).head(8)
        if len(fc):
            warn_c, ok_c = [], []
            print(f'[题材退潮] {focus} 昨日有涨停梯队的题材(概念口径):')
            for _, x in fc.iterrows():
                print(sector_line(x, 'concept'))
                tg = sector_tag(x)
                if tg.startswith(('题材结束', '退潮预警')):
                    warn_c.append(x['concept'])
                elif tg.startswith('健康'):
                    ok_c.append(x['concept'])
            if warn_c:
                print('        → 题材退潮信号: ' + '、'.join(warn_c) +
                      ' → 个股只要属于其中任一题材, 板块分按该题材取差(行业健康不算数)')
            if ok_c:
                print('        → 题材未退潮: ' + '、'.join(ok_c))
    print('-' * 66)
    lo_w = max(0, i_f - 21)
    w = out_df.iloc[lo_w:i_f + 1]
    print(f'近 {len(w)} 个交易日轨迹(温度/阶段/涨停/炸板/封板率/溢价/晋级/最高板/跌停):')
    print(f'{"日期":<12}{"阶段":<5}{"温度":>5}{"涨停":>5}{"炸板":>5}{"封板率":>8}'
          f'{"溢价":>8}{"晋级":>8}{"最高板":>7}{"跌停":>5}')
    for _, x in w.iterrows():
        pr = f'{x["premium"]}%' if x['premium'] != '' else '—'
        pg = f'{x["promote"]}%' if x['promote'] != '' else '—'
        mark = ' ←' if x['date'] == focus else ''
        print(f'{x["date"]:<12}{x["stage"]:<5}{x["temp"]:>5}{x["zt"]:>5}{x["zha"]:>5}'
              f'{str(x["seal"]) + "%":>8}{pr:>8}{pg:>8}{x["max_board"]:>7}{x["dt"]:>5}{mark}')
    print('-' * 66)
    print(f'序列已写入 {OUT} ({len(out_df)} 行); 行业级 {INDUSTRY_OUT} ({len(ind_df)} 行); '
          f'概念级 {CONCEPT_OUT} ({len(con_df)} 行)')


if __name__ == '__main__':
    main()