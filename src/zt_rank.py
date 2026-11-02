# -*- coding: utf-8 -*-
"""涨停候选打分排序(zt_rank) —— v2 实测刻度口径

背景: 原打分(v1)含 4板-25 / 5板-15 / 缩量-8 等惩罚, 实测符号全反(4板5板次日更强、
      缩量板次日更强), 且执行分把一字板判 0 分拉反了竞价因子; 结果在可买池内
      Top5 与 Bottom5 次日收益无差异(IC≈0, 240 天样本内)。

v2 只用实测有正向区分度的四项, 取消全部惩罚项。口径与刻度均来自分箱统计:

  候选池   当日涨停(收盘涨幅≥阈值); 非一字且开盘 < 9.5% 标记"可买", 一字/高开≥9.5%
           标记"买不进"但仍参与打分并展示(仅参考, 由使用者自行判断, 不再直接剔除)
  剔除     最强题材(板块强度最高者)已退潮/结束 → 不入池; 其余活跃题材退潮只写
           weak_themes 警告。原口径"任一活跃题材差即剔除"会误杀澳洋健康这类边缘
           概念(冷链物流/三胎)退潮、真实驱动题材(养老)健康的票: 被误杀组次日
           内 +1.19% / 外 +1.42%, 接近健康组(+1.36/+2.27), 明显优于真正退潮组
           (+0.73/+1.28), 故只按最强题材剔除
  概念黑名单 本轮新增 26 个非题材/宽泛标签(累计 47 个): 持仓牌照(证金/基金重仓/
           参股银行/券商/保险/信托/国家大基金持股等 13 个) + 宽泛/政策/生态(碳中和/
           工业互联网/AI智能体/无人机/数据要素/雄安/抖音/特斯拉等 13 个)。双窗口可买 Top5 前后: 内
           +2.03→+2.07%/日, 外 +2.37→+2.39%/日, 最近一月不变; 文化传媒/腾讯/AI应用
           因最近一月 5 个换票日全部变差(累计 +28.19%→+19.49%), 暂留观察
  板块强度 最强题材今涨停 <3 / 3-4 / 5-7 / 8家+ → 0 / 10 / 18 / 25
           该题材最高板 3 / 4 / 5+ 再 +2 / +3 / +5
  板数     1 / 2 / 3 / 4 / 5 / 6+ → 0 / 10 / 14 / 14 / 16 / 10
           (v2: 4板20 / 5+22; 收紧依据 4板内样本回落+1.02%、6板以上两窗口衰减)
  开盘涨幅 <-3 / -3~0 / 0~3 / 3~6 / 6~9.5% → 6 / 0 / 2 / 5 / 20
           (v2: <-3 给12, 但该档实测 +1.33% 低于 -3~0 的 +1.44%)
  缩量板   高开(开盘≥0)缩量 +10 / 低开缩量 +5 (v2 一律+10; 缩量低开两窗口仅微优于放量低开)
  阶段门控 仅 修复/发酵/高潮 启用横截面排序; 冰点/退潮只出名单并提示关闭
  v3(默认开启, --no-v3 关闭) 在 v2 基础上追加"题材高连板全断"退潮过滤: 候选最强
           题材内昨日连板>=3 的股票今日全部未封板 → 剔除。全断组候选次日
           +0.72%(内)/+1.66%(外), 全封组 +1.53%/+1.79%; 过滤代价 内 -0.06pp / 外 -0.09pp,
           按风险偏好默认开启
  分歧日切换(默认开启, --no-dv-switch 关闭) 高标分歧(diverge=1, 最高板>=4且封板率<62%)
           且阶段门控开启时, 若入选候选里存在行业级新题材首板(昨日0家涨停/今日>=2家
           且全为1板, 且近60个交易日该行业未出现单日>=5家涨停的"高潮"), 只在该新题材
           池内排序, 其余入选票转观察。门控开启分歧日 Top5 内 1.09→1.57% / 外 1.87→2.23%;
           全窗口 +0.07/+0.08pp。新题材个股组 内 +2.35%(胜率72%) / 外 +3.52%(67%),
           优于仅"昨0家"定义(2.12%/2.03%)。全历史从未高潮过过于严格(内样本仅2天),
           故用近60日回看; 直接给新题材加分实测样本内变差, 故用切池不用加分

验证(相邻两窗口, 相邻不重叠):
  样本内 2025-11~2026-10 198 天: Top5 +1.67% vs Bottom5 +0.45%, 差 +1.22pp, 胜率63%
  样本外 2024-07~2025-10 259 天: Top5 +2.32% vs Bottom5 +1.45%, 差 +0.87pp, 胜率61%
  v3 口径(新刻度+全断过滤): 内 Top5 +1.69% 差 +1.18pp / 外 +2.15% 差 +0.69pp
        --no-v3 回到 v2 的 1.22/0.87; 单改分解 板数 内+0.05pp/外-0.02pp, 缩量 内+0.02/外-0.07,
        低开 内0/外-0.07 → 刻度收紧主要服务风控语义(不追4板、缩量低开不抬分), 非收益增益
  日内打乱分数-收益配对 500 次: 两窗口真实差值均超过全部置换(排序区分度 p<0.002)
  阶段门控(冰点/退潮不做) 启用 内187/198 外247/259 天: 增益 +0.13pp(p=0.030) / +0.08pp(p=0.070)
        → 门控只作"极冷日不做"的纪律, 收益来源是四项排序本身
  对照 v1 原始口径同窗口: -0.84pp(43%) / -1.20pp(35%)

用法:
  .venv\\Scripts\\python.exe src\\zt_rank.py                      # 最新交易日(v3 默认开启)
  .venv\\Scripts\\python.exe src\\zt_rank.py --date 2026-10-29
  .venv\\Scripts\\python.exe src\\zt_rank.py --no-v3              # 回到 v2 口径
  .venv\\Scripts\\python.exe src\\zt_rank.py --top 10 --all      # 详细列出全部入选
输出: output/zt_rank_YYYYMMDD.csv(全部候选, status 区分 入选/剔除原因, buyable 标记可买/买不进,
      weak_themes 记录非最强题材的退潮警告)
"""
import os, sys, io, glob, argparse, re
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, 'stockdb_csv')
IND_FILE = os.path.join(ROOT, 'cache_data', '_stock_industry.csv')
CONCEPT_FILE = os.path.join(ROOT, 'cache_data', '_stock_concept.csv')
IND_SENTI = os.path.join(ROOT, 'output', 'sentiment_industry.csv')
CON_SENTI = os.path.join(ROOT, 'output', 'sentiment_concept.csv')
DAILY = os.path.join(ROOT, 'output', 'sentiment_daily.csv')

GATE_STAGES = ('修复', '发酵', '高潮')   # 阶段门控: 仅这些阶段启用横截面排序
SECTOR_MIN_ZT = 3                        # 活跃题材门槛: 今或昨涨停 >= 3 家
HB_MIN_BOARD = 3                         # v3 高连板门槛: 昨日连板 >= 该值算题材高标
THEME_CLIMAX_LOOKBACK = 60               # "未高潮"回看: 近 N 个交易日
THEME_CLIMAX_ZT = 5                      # 同行业单日涨停 >= 该家数 视为题材高潮
GAP_BUY_MAX = 9.5                        # 开盘涨幅 >= 该值视为买不进
SEC_COLS = ('zt_n', 'zt_prev', 'pos_ratio', 'dt_next', 'first_board_n',
            'leader_break', 'leader_dt', 'max_board')
# 与 sentiment_snapshot 的 CONCEPT_JUNK / CONCEPT_BLACKLIST 一致
CONCEPT_JUNK = ('股通', '融资', '融券', '罗素', 'MSCI', '标普', '富时', '转融', 'ST板块')
CONCEPT_BLACKLIST = (
    '华为', '新能源', '国企改革', 'DeepSeek', '专精特新', '贬值', '预增',
    '预亏', '股权转让', '并购重组', '高股息', '破净', '创投', '一带一路',
    '西部大开发', '粤港澳', '乡村振兴', '海峡两岸', '京津冀',
    # 资金属性/持仓/牌照: 不是题材, 涨停只是持仓/参股巧合
    '证金持股', '基金重仓', '参股银行', '参股券商', '参股保险', '信托概念',
    '国家大基金持股', '村镇银行', '金控平台', '互联网保险', '互联网券商',
    '参股外资金融', 'T+0基金', '基金三方销售', '科技保险',
    # 宽泛政策/生态标签: 作为最强题材时双窗口均跑输可买基准
    '碳中和', '工业互联网', 'AI智能体', '无人机', '智能医疗', '智能电网',
    '数据要素', '虚拟现实', '网红经济', '统一大市场', '雄安新区',
    '抖音概念', '特斯拉概念',
)


def limit_threshold(code6, name):
    """涨停判定阈值(0.2pct 容差), 同 sentiment_snapshot / scan_signals 口径"""
    if code6.startswith(('300', '301', '688', '689')):
        return 19.8
    if code6.startswith(('43', '83', '87', '920')):
        return 29.8
    if name and 'ST' in str(name).upper():
        return 4.8
    return 9.8


def load_meta():
    """股票名 / CSRC 行业 / 个股→题材 映射"""
    ind = pd.read_csv(IND_FILE, dtype=str)
    ind['c6'] = ind['ts_code'].str.split('.').str[0]
    name = ind.set_index('c6')['name'].to_dict()
    industry = ind.set_index('c6')['industry'].to_dict()
    cp = pd.read_csv(CONCEPT_FILE, dtype=str).dropna(subset=['ts_code', 'concept'])
    cp = cp[~cp['concept'].str.contains('|'.join(re.escape(k) for k in CONCEPT_JUNK), na=False)]
    cp = cp[~cp['concept'].str.contains('|'.join(re.escape(k) for k in CONCEPT_BLACKLIST), na=False)]
    cp['c6'] = cp['ts_code'].str.split('.').str[0]
    concepts = cp.groupby('c6')['concept'].apply(lambda s: sorted(set(s))).to_dict()
    return name, industry, concepts


def load_sector(date):
    """指定日期的行业级 + 概念级板块快照行"""
    def rows(fp, label):
        d = pd.read_csv(fp, dtype={'date': str})
        d = d[d['date'] == date]
        for c in SEC_COLS:
            d[c] = pd.to_numeric(d[c], errors='coerce')
        return {r[label]: r for _, r in d.iterrows()}

    return rows(IND_SENTI, 'industry'), rows(CON_SENTI, 'concept')


def scan_limit_ups(name):
    """扫描全市场日线 → 全部涨停记录 [{code,name,date,board,gap,yizi,suoliang,ret1}]"""
    out = []
    for fp in glob.glob(os.path.join(DATA_DIR, '*.csv')):
        code = os.path.basename(fp)[3:9]
        nm = name.get(code, '')
        if not nm or 'ST' in nm.upper() or '退' in nm:
            continue
        rr = [l.split(',') for l in open(fp, encoding='gbk', errors='ignore').read()
              .splitlines()[1:] if len(l.split(',')) in (7, 8) and l.split(',')[0][:4].isdigit()]
        if len(rr) < 6:
            continue
        t = limit_threshold(code, nm)
        streak = 0
        for i in range(5, len(rr)):
            try:
                c1, c0 = float(rr[i][4]), float(rr[i - 1][4])
                o, v1, v0 = float(rr[i][1]), float(rr[i][5]), float(rr[i - 1][5])
            except ValueError:
                streak = 0
                continue
            if (c1 / c0 - 1) * 100 < t:
                streak = 0
                continue
            streak += 1
            ret1 = ((float(rr[i + 1][4]) / c1 - 1) * 100) if i + 1 < len(rr) else None
            out.append(dict(code=code, name=nm, date=rr[i][0], board=streak,
                            gap=(o / c0 - 1) * 100, yizi=(o / c0 - 1) * 100 >= t,
                            suoliang=v1 < v0, ret1=ret1))
    return out


def sector_ok(s):
    """题材是否健康(未退潮/未结束), 严格按规则: 昨涨停过半有溢价 + 龙头/补涨无跌停
    龙头断板本身不算退潮, 只有断板且当日 0 首板才算题材结束"""
    zt_prev = int(s['zt_prev'])
    pos = s['pos_ratio'] if pd.notna(s['pos_ratio']) else None
    ended = bool(s['leader_break']) and int(s['first_board_n']) == 0
    bad = ended or bool(s['leader_dt']) or (
        zt_prev >= SECTOR_MIN_ZT and pos is not None
        and (pos < 50 or s['dt_next'] >= 2 or (s['dt_next'] >= 1 and pos < 60)))
    return (not bad), ended


def theme_members(nm, industry, concepts):
    """题材名 → 成分股集合(先按行业名匹配, 否则按概念)"""
    s = {c for c, ind in industry.items() if ind == nm}
    if s:
        return s
    return {c for c, cs in concepts.items() if nm in cs}


def theme_strength(x):
    """题材强度(0~30): 今涨停家数分档 + 该题材最高板加成"""
    zn = int(x['zt_n'])
    base = 25 if zn >= 8 else 18 if zn >= 5 else 10 if zn >= 3 else 0
    mb = int(x['max_board']) if pd.notna(x['max_board']) else 0
    return base + (5 if mb >= 5 else 3 if mb == 4 else 2 if mb == 3 else 0), zn, mb


def score(rec, industry, sec_rows, con_rows, concepts, v3=True):
    """v2/v3 打分 → (总分, 分项dict, 状态)"""
    cands = []
    ind = industry.get(rec['code'], '未知') or '未知'
    si = sec_rows.get(ind)
    if si is not None:
        cands.append((ind, si))
    for cn in concepts.get(rec['code'], ()):
        x = con_rows.get(cn)
        if x is not None and (int(x['zt_n']) >= SECTOR_MIN_ZT or int(x['zt_prev']) >= SECTOR_MIN_ZT):
            cands.append((cn, x))

    if not cands:
        return None, dict(best_theme='无行业/题材数据', theme_zt_n=0, theme_max_board=0,
                          sec=0, board_sc=0, gap_sc=0, suoliang_sc=0), '无板块数据'
    scored = []
    for nm, x in cands:
        v, zn, mb = theme_strength(x)
        ok, _ = sector_ok(x)
        scored.append((v, nm, zn, mb, ok))
    top_v = max(t[0] for t in scored)
    tops = [t for t in scored if t[0] == top_v]
    best = next((t for t in tops if t[4]), tops[0])   # 同等强度优先取健康题材
    weak = [t[1] for t in scored if not t[4] and t[1] != best[1]]
    sec_sc, best_nm, zn, mb, best_ok = best

    b = rec['board']
    g = rec['gap']
    if v3:
        bsc = 10 if b >= 6 else 16 if b == 5 else 14 if b in (3, 4) else 10 if b == 2 else 0
        gsc = 20 if g >= 6 else 5 if g >= 3 else 2 if g >= 0 else 0 if g >= -3 else 6
        ssc = (10 if g >= 0 else 5) if rec['suoliang'] else 0
    else:
        bsc = 22 if b >= 5 else 20 if b == 4 else 14 if b == 3 else 10 if b == 2 else 0
        gsc = 20 if g >= 6 else 5 if g >= 3 else 2 if g >= 0 else 0 if g >= -3 else 12
        ssc = 10 if rec['suoliang'] else 0
    total = sec_sc + bsc + gsc + ssc

    buyable = not (rec['yizi'] or g >= GAP_BUY_MAX)
    status = '入选' if best_ok else '剔除·题材退潮/结束'
    detail = dict(best_theme=best_nm, theme_zt_n=zn, theme_max_board=mb,
                  sec=sec_sc, board_sc=bsc, gap_sc=gsc, suoliang_sc=ssc,
                  buyable=buyable, weak_themes='/'.join(weak))
    return total, detail, status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='交易日 YYYY-MM-DD, 默认最新')
    ap.add_argument('--top', type=int, default=8, help='控制台显示前 N 名, 默认 8')
    ap.add_argument('--all', action='store_true', help='详细列出全部入选(默认只列前 N)')
    ap.add_argument('--v3', action='store_true', default=True,
                    help='v3: 题材高连板全断退潮过滤(默认开启)')
    ap.add_argument('--no-v3', dest='v3', action='store_false',
                    help='关闭 v3 过滤, 回到 v2 口径')
    ap.add_argument('--dv-switch', dest='dv_switch', action='store_true', default=True,
                    help='分歧日切换池: 有新题材首板时只在新题材内排序(默认开启)')
    ap.add_argument('--no-dv-switch', dest='dv_switch', action='store_false',
                    help='关闭分歧日切换池')
    args = ap.parse_args()

    name, industry, concepts = load_meta()
    recs_all = scan_limit_ups(name)
    dates = sorted({r['date'] for r in recs_all})
    if not dates:
        print('!! stockdb_csv 无涨停记录'); return
    T = args.date or dates[-1]
    board_by_date = {}
    for r in recs_all:
        board_by_date.setdefault(r['date'], {})[r['code']] = r['board']
    ind_cnt = {}
    for r in recs_all:
        k = (r['date'], industry.get(r['code'], '未知') or '未知')
        ind_cnt[k] = ind_cnt.get(k, 0) + 1
    recs = [r for r in recs_all if r['date'] == T]
    if not recs:
        print(f'!! {T} 无涨停候选'); return

    sec_rows, con_rows = load_sector(T)
    if not sec_rows:
        print(f'!! 缺 {T} 的板块快照 → 先跑 sentiment_snapshot.py --start ... 覆盖该日期'); return

    sd = pd.read_csv(DAILY, dtype={'date': str})
    row = sd[sd['date'] == T]
    stage = row.iloc[-1]['stage'] if len(row) else '—'
    gate_on = stage in GATE_STAGES
    row_last = row.iloc[-1] if len(row) else None
    diverge = int(row_last['diverge']) if row_last is not None and pd.notna(row_last['diverge']) else 0
    cal = sorted(sd['date'])
    prev = cal[cal.index(T) - 1] if T in cal and cal.index(T) > 0 else None

    hb_cache, v3_cut = {}, 0

    def hb_all_broken(nm):
        """v3: 该题材昨日连板>=3 的股票今日是否全部未封板"""
        if nm not in hb_cache:
            pv = board_by_date.get(prev, {}) if prev else {}
            td = board_by_date.get(T, {})
            hb = [c for c in theme_members(nm, industry, concepts)
                  if pv.get(c, 0) >= HB_MIN_BOARD]
            hb_cache[nm] = bool(hb) and not any(c in td for c in hb)
        return hb_cache[nm]

    rows = []
    for r in recs:
        s, d, status = score(r, industry, sec_rows, con_rows, concepts, args.v3)
        if s is None:
            continue
        if args.v3 and status == '入选' and hb_all_broken(d['best_theme']):
            status = '剔除·题材高连板全断'
            v3_cut += 1
        rows.append(dict(date=T, code=r['code'], name=r['name'],
                         industry=industry.get(r['code'], '未知') or '未知',
                         board=r['board'], gap=round(r['gap'], 2),
                         suoliang='缩量' if r['suoliang'] else '放量',
                         yizi='一字' if r['yizi'] else '',
                         score=s, theme=d['best_theme'], theme_zt_n=d['theme_zt_n'],
                         theme_max_board=d['theme_max_board'],
                         sec_sc=d['sec'], board_sc=d['board_sc'],
                         gap_sc=d['gap_sc'], suoliang_sc=d['suoliang_sc'],
                         status=status,
                         buyable='可买' if d['buyable'] else '买不进',
                         weak_themes=d['weak_themes'],
                         next_ret=r['ret1'] if r['ret1'] is not None else float('nan')))

    t_idx = cal.index(T) if T in cal else None
    nt_cache = {}

    def theme_no_climax(nm):
        """该行业近 N 个交易日未出现单日 >= THEME_CLIMAX_ZT 家涨停(即近期未高潮)"""
        if nm not in nt_cache:
            if t_idx is None:
                nt_cache[nm] = True
            else:
                lo = max(0, t_idx - THEME_CLIMAX_LOOKBACK)
                nt_cache[nm] = max((ind_cnt.get((d, nm), 0) for d in cal[lo:t_idx]),
                                   default=0) < THEME_CLIMAX_ZT
        return nt_cache[nm]

    def new_theme_ind(nm):
        """行业级新题材: 昨0家/今>=2家全首板, 且近60日未高潮(单日>=5家涨停)"""
        x = sec_rows.get(nm)
        return (x is not None and int(x['zt_prev']) == 0 and int(x['zt_n']) >= 2
                and pd.notna(x['max_board']) and int(x['max_board']) == 1
                and theme_no_climax(nm))

    dv_pool, dv_sw = [], 0
    if args.dv_switch and diverge and gate_on:
        dv_pool = [x for x in rows if x['status'] == '入选' and x['buyable'] == '可买'
                   and new_theme_ind(x['industry'])]
        if dv_pool:
            for x in rows:
                if (x['status'] == '入选' and x['buyable'] == '可买'
                        and not new_theme_ind(x['industry'])):
                    x['status'] = '观察·分歧日切新题材'
                    dv_sw += 1
    df = pd.DataFrame(rows).sort_values('score', ascending=False).reset_index(drop=True)
    df.insert(1, 'rank', df.index + 1)

    op = os.path.join(ROOT, 'output', f'zt_rank_{T.replace("-", "")}.csv')
    df.to_csv(op, index=False, encoding='utf-8-sig')

    sel = df[df['status'] == '入选']
    buy = sel[sel['buyable'] == '可买']
    nob = sel[sel['buyable'] != '可买']
    print(f'{T} 涨停候选 {len(df)} 只 | 入选 {len(sel)} 只(可买 {len(buy)} / 买不进 {len(nob)})'
          f' | 剔除 {len(df) - len(sel)} 只 | 情绪阶段 {stage}')
    if args.v3:
        print(f'v3 高连板全断过滤: 剔除 {v3_cut} 只(题材昨日>=3板今日全部未封)')
    if args.dv_switch and diverge and gate_on:
        if dv_pool:
            print(f'分歧日切换池: 行业级新题材可买入选 {len(dv_pool)} 只, '
                  f'其余 {dv_sw} 只转观察(昨0家/今>=2家全首板/近'
                  f'{THEME_CLIMAX_LOOKBACK}日无>={THEME_CLIMAX_ZT}家涨停)')
        else:
            print('分歧日切换池: 无行业级新题材可买首板, 维持原排序')
    if not gate_on:
        print(f'!! 阶段门控关闭: {stage} 不属于 {" / ".join(GATE_STAGES)}(冰点/退潮不做横截面打板排序)')
        print('   以下名单仅供观察, 打板只在转强第一天第一时间窗口')
    print('=' * 96)
    print(f'{"#":>2} {"代码":<7}{"名称":<9}{"板":>2} {"开盘":>7} {"缩量":>4} {"分":>5} {"可买":<6} 题材(今涨停/最高板)')
    show_n = len(sel) if args.all else args.top
    for i, (_, x) in enumerate(sel.head(show_n).iterrows(), 1):
        rt = f'  次日{x["next_ret"]:+.2f}%' if pd.notna(x['next_ret']) else ''
        print(f'{i:>2} {x["code"]:<7}{x["name"]:<9}{x["board"]:>2} '
              f'{x["gap"]:>+6.1f}% {x["suoliang"]:>4} {x["score"]:>5.0f} '
              f'{x["buyable"]:<6} '
              f'{x["theme"]}({x["theme_zt_n"]}/{x["theme_max_board"]}){rt}')
        print(f'     板块{x["sec_sc"]} + 板数{x["board_sc"]} + 开盘{x["gap_sc"]} + 缩量{x["suoliang_sc"]}')
        if x['weak_themes']:
            print(f'     其他题材退潮(仅警告): {x["weak_themes"]}')
    rest = df[df['status'] != '入选']
    if len(rest):
        print('-' * 96)
        print('剔除:')
        for _, x in rest.iterrows():
            print(f'   {x["code"]} {x["name"]:<8} {x["board"]}板 开盘{x["gap"]:+.1f}%  '
                  f'{x["score"]:>4.0f}分 {x["buyable"]:<6} {x["status"]}')
    if buy['next_ret'].notna().any():
        ok = buy[buy['next_ret'].notna()]
        print(f'\n次日验证(可买入选): {len(ok)} 只均值 {ok["next_ret"].mean():+.2f}% '
              f'(涨{int((ok["next_ret"] > 0).sum())}/跌{int((ok["next_ret"] < 0).sum())})')
    if nob['next_ret'].notna().any():
        nb = nob[nob['next_ret'].notna()]
        print(f'次日验证(买不进, 仅参考不可成交): {len(nb)} 只均值 {nb["next_ret"].mean():+.2f}% '
              f'(涨{int((nb["next_ret"] > 0).sum())}/跌{int((nb["next_ret"] < 0).sum())})')
    print(f'\n已写入 {op}')


if __name__ == '__main__':
    main()
