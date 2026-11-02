# -*- coding: utf-8 -*-
"""扫描今日买入/卖出信号 + 回测胜率/收益，写入 Excel

用法:
    .venv\\Scripts\\python.exe scan_signals.py                    # 扫描 stockdb_csv 目录
    .venv\\Scripts\\python.exe scan_signals.py --dir cache_data   # 扫描 cache_data 目录
    .venv\\Scripts\\python.exe scan_signals.py --date 2026-09-09   # 指定日期
    .venv\\Scripts\\python.exe scan_signals.py --sort return       # 按收益排序
"""
import os, sys, glob, argparse, numpy as np, pandas as pd

# 路径：脚本在 src/ 下，stockdb_csv / cache_data / output 在项目根（src 上一级）
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SRC_DIR)

# ── 加载策略文件（只取策略类定义部分）──
strat_file = os.path.join(SRC_DIR, 'strategy_normal.py')
_orig_argv = sys.argv
sys.argv = [strat_file, '600206.SH']
ns = {'__file__': os.path.abspath(strat_file), 'sys': sys}
src = open(strat_file, encoding='utf-8').read()
for marker in ['# 创建 cerebro', '# ====.*引擎', '# 引擎']:
    if marker in src:
        src = src.split(marker)[0]
        break
exec(src, ns)
sys.argv = _orig_argv

INIT_CASH = ns['INIT_CASH']
COMMISSION = ns.get('COMMISSION', 0.0005)
MIN_COMM = ns.get('MIN_COMM', 5.0)
STAMP = ns.get('STAMP', 0.0005)
AStockCommission = ns['AStockCommission']
FullLotSizer = ns['FullLotSizer']
StrategyClass = ns.get('MyStrategy', ns.get('PullbackStrategy'))

# 大盘择时（指数 MA 过滤，详见 market_timing.py；默认关闭，--timing 开启）
import market_timing as mt

# ── 股票名称 → 行业分类（名称关键词匹配）──
def _load_stock_names():
    """从 _stock_list.csv 加载 ts_code→name 映射（cache_data 文件无名称）"""
    p = os.path.join(PROJECT_ROOT, 'cache_data', '_stock_list.csv')
    if not os.path.exists(p):
        return {}
    try:
        sl = pd.read_csv(p)
        col_code = 'ts_code' if 'ts_code' in sl.columns else sl.columns[0]
        col_name = 'name' if 'name' in sl.columns else sl.columns[2]
        return dict(zip(sl[col_code], sl[col_name]))
    except Exception:
        return {}

_STOCK_NAMES = _load_stock_names()


def _load_csrc_industry():
    """从 baostock 行业文件加载 ts_code→industry 和 ts_code→name 映射（兜底用）"""
    p = os.path.join(PROJECT_ROOT, 'cache_data', '_stock_industry.csv')
    if not os.path.exists(p):
        return {}, {}
    try:
        si = pd.read_csv(p)
        col_code = 'ts_code' if 'ts_code' in si.columns else si.columns[0]
        col_ind = 'industry' if 'industry' in si.columns else si.columns[2]
        col_name = 'name' if 'name' in si.columns else si.columns[1]
        return (dict(zip(si[col_code], si[col_ind])),
                dict(zip(si[col_code], si[col_name])))
    except Exception:
        return {}, {}

_CSRC_INDUSTRY, _CSRC_NAMES = _load_csrc_industry()


def _load_sw_industry():
    """申万行业映射（首板抱团雷达专用）：ts_code → (sw_l1, sw_l2, sw_l3)
    l2 缺失时按 l3→l1 顺序兜底。"""
    p = os.path.join(PROJECT_ROOT, 'cache_data', '_stock_sw_industry.csv')
    if not os.path.exists(p):
        return {}
    try:
        sw = pd.read_csv(p, dtype=str)
        for c in ('sw_l1', 'sw_l2', 'sw_l3'):
            if c in sw.columns:
                sw[c] = sw[c].replace({'': None, 'nan': None, 'None': None})
        out = {}
        for _, r in sw.iterrows():
            l2 = r.get('sw_l2') or r.get('sw_l3') or r.get('sw_l1') or ''
            out[r['ts_code']] = (r.get('sw_l1') or '', l2, r.get('sw_l3') or '')
        return out
    except Exception:
        return {}

_SW_INDUSTRY = _load_sw_industry()

# 首板抱团雷达：首板阈值 + 双环境皆负的 4 行业黑名单（全市场实证 2021-10~2026-10）
BURST_MIN_N = 5
BURST_BLACKLIST = {'煤炭开采', '电力', '工业金属', '化学制药'}

# CSRC 证监会行业名 → 简称（兜底分类时统一口径）
_CSRC_SIMPLIFY = {
    '房地产业': '房地产', '批发业': '商贸零售', '零售业': '商贸零售',
    '商务服务业': '商贸零售', '互联网和相关服务': '科技IT',
    '软件和信息技术服务业': '科技IT', '电信、广播电视和卫星传输服务': '通信',
    '土木工程建筑业': '建筑', '房屋建筑业': '建筑', '建筑安装业': '建筑',
    '化学原料和化学制品制造业': '化工', '化学纤维制造业': '化工',
    '医药制造业': '医药医疗', '专用设备制造业': '机械', '通用设备制造业': '机械',
    '电气机械和器材制造业': '电气设备', '计算机、通信和其他电子设备制造业': '电子半导体',
    '铁路、船舶、航空航天和其他运输设备制造业': '交通运输设备',
    '汽车制造业': '汽车', '文教、工美、体育和娱乐用品制造业': '轻工制造',
    '非金属矿物制品业': '建材', '黑色金属冶炼和压延加工业': '钢铁',
    '有色金属冶炼和压延加工业': '有色金属', '金属制品业': '金属制品',
    '酒、饮料和精制茶制造业': '食品饮料', '农副食品加工业': '农业',
    '食品制造业': '食品饮料', '纺织业': '纺织服装', '纺织服装、服饰业': '纺织服装',
    '印刷和记录媒介复制业': '包装印刷', '造纸和纸制品业': '造纸',
    '公共设施管理业': '旅游', '农业': '农业', '林业': '农业', '畜牧业': '农业',
    '渔业': '农业', '电力、热力生产和供应业': '电力', '燃气生产和供应业': '燃气',
    '煤炭开采和洗选业': '煤炭', '石油和天然气开采业': '石油石化',
    '生态保护和环境治理业': '环保', '水的生产和供应业': '环保',
    '水上运输业': '交通运输', '航空运输业': '交通运输', '铁路运输业': '交通运输',
    '道路运输业': '交通运输', '装卸搬运和运输代理业': '交通运输',
    '仓储业': '交通运输', '邮政业': '交通运输',
    '货币金融服务': '银行', '资本市场服务': '非银金融', '保险业': '非银金融',
    '其他金融业': '非银金融', '租赁业': '非银金融',
}

# 分类规则：按优先级排列，多字关键词避免误匹配
_INDUSTRY_RULES = [
    # 金融
    ('银行', '银行'), ('证券', '非银金融'), ('保险', '非银金融'),
    ('期货', '非银金融'), ('信托', '非银金融'), ('金融', '非银金融'),
    # 医药
    ('医药', '医药医疗'), ('药业', '医药医疗'), ('制药', '医药医疗'),
    ('生物', '医药医疗'), ('医疗', '医药医疗'), ('诊断', '医药医疗'),
    ('基因', '医药医疗'), ('眼科', '医药医疗'), ('药房', '医药医疗'),
    ('健康', '医药医疗'), ('药', '医药医疗'),
    # 化工
    ('化工', '化工'), ('化学', '化工'), ('石化', '化工'), ('氨纶', '化工'),
    # 食品饮料
    ('食品', '食品饮料'), ('饮料', '食品饮料'), ('酒业', '食品饮料'),
    ('乳业', '食品饮料'), ('味业', '食品饮料'), ('调味', '食品饮料'),
    ('茶业', '食品饮料'), ('茶', '食品饮料'),
    # 房地产
    ('房地产', '房地产'), ('置业', '房地产'), ('地产', '房地产'),
    # 建材
    ('水泥', '建材'), ('玻璃', '建材'), ('陶瓷', '建材'), ('建材', '建材'),
    # 钢铁
    ('钢铁', '钢铁'), ('钢', '钢铁'),
    # 有色/黄金
    ('黄金', '黄金珠宝'), ('珠宝', '黄金珠宝'), ('金矿', '黄金珠宝'),
    ('铝业', '有色金属'), ('铜业', '有色金属'), ('锌业', '有色金属'),
    ('锂业', '有色金属'), ('钨业', '有色金属'), ('稀土', '有色金属'),
    # 能源
    ('煤业', '煤炭'), ('煤炭', '煤炭'), ('燃气', '燃气'),
    ('石油', '石油石化'), ('石化', '石油石化'),
    ('电力', '电力'), ('核电', '电力'), ('风电', '电力'),
    ('光伏', '电力'), ('太阳能', '电力'), ('能源', '电力'),
    ('股份', None),  # 占位：排除"股份"
    # 电气
    ('电气', '电气设备'), ('电缆', '电气设备'), ('电源', '电气设备'),
    # 电子
    ('半导体', '电子半导体'), ('芯片', '电子半导体'), ('集成电路', '电子半导体'),
    # 科技
    ('软件', '科技IT'), ('信息', '科技IT'), ('数据', '科技IT'),
    ('网络', '科技IT'), ('智能', '科技IT'), ('科技', '科技IT'),
    ('计算机', '科技IT'), ('数字', '科技IT'),
    # 通信
    ('通信', '通信'), ('电信', '通信'),
    # 传媒
    ('传媒', '传媒游戏'), ('影视', '传媒游戏'), ('游戏', '传媒游戏'),
    ('文化', '传媒游戏'), ('出版', '传媒游戏'),
    # 汽车
    ('汽车', '汽车'), ('汽配', '汽车'),
    # 机械
    ('机械', '机械'), ('重工', '机械'), ('设备', '机械'),
    # 建筑
    ('建筑', '建筑'), ('建工', '建筑'), ('建设', '建筑'),
    ('园林', '建筑'), ('装饰', '建筑'),
    # 纺织
    ('纺织', '纺织服装'), ('服装', '纺织服装'), ('服饰', '纺织服装'),
    ('家纺', '纺织服装'),
    # 农业
    ('农业', '农业'), ('种业', '农业'), ('牧业', '农业'), ('渔业', '农业'),
    ('饲料', '农业'),
    # 交通运输
    ('航空', '交通运输'), ('机场', '交通运输'), ('港口', '交通运输'),
    ('航运', '交通运输'), ('高速', '交通运输'), ('物流', '交通运输'),
    ('运输', '交通运输'), ('仓储', '交通运输'), ('铁路', '交通运输'),
    ('外运', '交通运输'), ('客运', '交通运输'),
    # 旅游
    ('旅游', '旅游'), ('景区', '旅游'), ('酒店', '旅游'),
    # 军工
    ('军工', '军工'), ('航天', '军工'), ('防务', '军工'),
    # 环保
    ('环保', '环保'), ('水务', '环保'), ('环境', '环保'),
    # 包装印刷
    ('包装', '包装印刷'), ('印刷', '包装印刷'),
    # 家居
    ('家居', '家居'), ('家具', '家居'),
    # 商贸
    ('百货', '商贸零售'), ('商业', '商贸零售'), ('零售', '商贸零售'),
    ('贸易', '商贸零售'), ('批发', '商贸零售'),
]


def classify_industry(name, ts_code=''):
    """根据股票名称关键词推断行业分类；未命中时用 baostock 证监会行业兜底"""
    if not name:
        # 名称缺失（cache_data 无名称且 _stock_list.csv 无该股）→ 直接走 CSRC 兜底
        if ts_code and ts_code in _CSRC_INDUSTRY:
            raw = _CSRC_INDUSTRY[ts_code]
            if raw:
                for i, ch in enumerate(raw):
                    if '\u4e00' <= ch <= '\u9fff':
                        cn = raw[i:]
                        return _CSRC_SIMPLIFY.get(cn, cn)
        return ''
    # 去掉 ST/*ST 前缀后递归分类
    clean = name.replace('*ST', '').replace('ST', '').strip()
    if clean != name:
        r = classify_industry(clean)
        if r and r != '其他':
            return r
    for kw, cat in _INDUSTRY_RULES:
        if cat is None:
            continue
        if kw in clean:
            return cat
    # 关键词未命中 → baostock 证监会行业兜底（去代码前缀 + 简称映射）
    if ts_code and ts_code in _CSRC_INDUSTRY:
        raw = _CSRC_INDUSTRY[ts_code]
        if raw:
            # "C27医药制造业" → "医药制造业"；"K70房地产业" → "房地产业"
            for i, ch in enumerate(raw):
                if '\u4e00' <= ch <= '\u9fff':
                    cn = raw[i:]
                    return _CSRC_SIMPLIFY.get(cn, cn)
            return raw
    return '其他'


# ── 参数 ──
parser = argparse.ArgumentParser(description='扫描今日买卖信号 + 回测胜率/收益')
parser.add_argument('--dir', default=None,
                    help='数据目录（normal/main 均默认 stockdb_csv 前复权；main 可用 --dir cache_data 回到 main.py 不复权对账口径）')
parser.add_argument('--strategy', default='normal', choices=['normal', 'main'],
                    help='策略：normal=strategy_normal MA5三信号(默认)；main=main.py 影线策略')
parser.add_argument('--date', default=None, help='指定日期 YYYY-MM-DD（默认最新交易日）')
parser.add_argument('--sort', default='signal', choices=['signal', 'return', 'win_rate'],
                    help='排序方式（默认按信号类型，return 按回测收益，win_rate 按胜率）')
parser.add_argument('--timing', nargs='?', const='exit', default=None,
                    choices=['off', 'gate', 'exit', 'half'],
                    help='大盘择时（默认关闭）：不带值=exit破位离场(HS300 MA20+涨停顺延)；'
                         '可选 gate线下不新开 / half线下半仓 / off显式关闭')
parser.add_argument('--timing-ma', type=int, default=None, help='择时指数 MA 周期（默认20）')
parser.add_argument('--no-lu-exempt', dest='lu_exempt', action='store_false', default=None,
                    help='关闭破位涨停顺延（exit 模式默认开启）')
parser.add_argument('--min-ret', type=float, default=50.0,
                    help='优质股筛选：回测收益%%下限（默认50）')
parser.add_argument('--min-win', type=float, default=40.0,
                    help='优质股筛选：回测胜率%%下限（默认40）')
parser.add_argument('--rf', type=float, default=0.015,
                    help='夏普比率无风险年利率（默认0.015，口径同 risk_metrics.py）')
parser.add_argument('--vol-target', type=float, default=None,
                    help='波动率目标仓位（年化%%）：30=目标30%%波动率，高波动票按比例减仓，低波动票满仓')
parser.add_argument('--export', action='store_true',
                    help='先增量更新 stockdb_csv/（调用 export_stockdb_csv.py --update），再扫描该目录；'
                         '未显式指定 --dir 时自动切换到 stockdb_csv')
args = parser.parse_args()
# --export: 先增量刷新 stockdb_csv，再扫描（导完即分析）
if args.export:
    import subprocess
    _exp = os.path.join(PROJECT_ROOT, 'export_stockdb_csv.py')
    print('══ 步骤1/2: 增量更新 stockdb_csv ══')
    _r = subprocess.run([sys.executable, _exp, '--update'], cwd=PROJECT_ROOT)
    if _r.returncode != 0:
        print('导出失败，终止扫描'); sys.exit(1)
    if args.dir is None:
        args.dir = 'stockdb_csv'
    print('══ 步骤2/2: 扫描信号 ══')
# 数据目录：normal/main 均默认 stockdb_csv（前复权，全市场，更新到最新交易日）；
# main 与 main.py 逐笔对账的不复权历史口径可用 --dir cache_data 复现
if args.dir is None:
    args.dir = 'stockdb_csv'
# --timing 不给值时沿用库默认（2026-09-16 起为 off）；给了值或调 MA/顺延开关才显式配置
if args.timing or args.timing_ma or args.lu_exempt is False:
    mt.configure(mode=args.timing, ma=args.timing_ma, limit_exempt=args.lu_exempt)

DATA_DIR = os.path.join(PROJECT_ROOT, args.dir)
OUTPUT_DIR = os.path.join(PROJECT_ROOT, 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Vol Targeting 全局开关（多进程 pickle 传递给 process_file）
VOL_TARGET = getattr(args, 'vol_target', None)

# 环境雷达亮灯阈值：HS300 在 MA20 线上且全市场 bias20<-15% 个股数 ≥300。
# 5年逐日校准（2021-10~2026-09，3199只）：仅 2024-02-06/07/08/19/20 五天触发、零误报；
# 阴跌型广度（2026-05/06 大量<-10%但<-15%不足300）不亮。
RADAR_D15_TH = 300

# 回测区间：今年
YEAR = 2026
YEAR_START = pd.Timestamp(f'{YEAR}-01-01')
YEAR_END = pd.Timestamp(f'{YEAR}-12-31')

_EPS = 1e-12

def sharpe_daily(daily_returns, rf=None):
    """日简单收益序列 → 年化夏普（口径同 risk_metrics.py）：
    (mean日收益−rf/252)/样本std(ddof=1)×√252；样本<2或全程零波动返回 None。"""
    if rf is None:
        rf = args.rf
    r = np.asarray(daily_returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return None
    sd = r.std(ddof=1)
    if sd < _EPS:
        return None
    return round(float((r.mean() - rf / 252.0) / sd * np.sqrt(252)), 2)


def load_local_csv(filepath):
    """快速加载 stockdb_csv 逐股 CSV（GBK，混合分隔）"""
    try:
        import numpy as np
        raw = open(filepath, 'r', encoding='gbk').read()
        lines = raw.strip().split('\n')
        if len(lines) < 3:
            return None, ''
        name = lines[0].split()[1] if len(lines[0].split()) > 1 else ''

        dates = []
        o_arr, h_arr, l_arr, c_arr, v_arr = [], [], [], [], []
        for line in lines[2:]:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # 统一替换分隔符为逗号
            parts = line.replace('\t', ',').replace(' ', ',').split(',')
            parts = [p for p in parts if p]
            if len(parts) < 6:
                continue
            try:
                dates.append(parts[0])
                o_arr.append(float(parts[1]))
                h_arr.append(float(parts[2]))
                l_arr.append(float(parts[3]))
                c_arr.append(float(parts[4]))
                v_arr.append(float(parts[5]))
            except (ValueError, IndexError):
                continue

        if not dates:
            return None, ''

        idx = pd.to_datetime(dates, errors='coerce')
        df = pd.DataFrame({
            'open': np.array(o_arr),
            'high': np.array(h_arr),
            'low': np.array(l_arr),
            'close': np.array(c_arr),
            'volume': np.array(v_arr),
        }, index=idx)
        df = df[~df.index.isna()].sort_index()
        return df, name
    except Exception:
        return None, ''


def load_cache(filepath):
    try:
        df = pd.read_csv(filepath)
        date_col = 'trade_date' if 'trade_date' in df.columns else 'date'
        df[date_col] = pd.to_datetime(df[date_col].astype(str))
        df = df.sort_values(date_col).set_index(date_col)
        if 'vol' in df.columns:
            df = df.rename(columns={'vol': 'volume'})
        return df, ''
    except Exception:
        return None, ''


def check_signals(df, code=None, stock_name=''):
    """检查最新交易日的买卖信号"""
    if len(df) < 25:
        return None
    df = df.copy()
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma10'] = df['close'].rolling(10).mean()
    df['ma20'] = df['close'].rolling(20).mean()

    today = df.index[-1]
    if args.date:
        target = pd.Timestamp(args.date)
        if target not in df.index:
            return None
        today = target

    today_idx = df.index.get_loc(today)
    if today_idx < 20:
        return None

    # 模拟持仓状态：从数据开始到今天，按日追踪是否持仓
    # 买入信号触发 → 持仓；收盘<MA5 → 空仓；持仓期间不再发买入信号
    state = 'flat'  # 'flat' 空仓, 'in' 持仓, 'lu_pending' 破位涨停顺延中
    signals_today = []

    for i in range(20, today_idx + 1):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        o, h, l, c = row['open'], row['high'], row['low'], row['close']
        prev_c = prev['close']
        ma5, ma10, ma20 = row['ma5'], row['ma10'], row['ma20']
        prev_ma5, prev_ma10, prev_ma20 = prev['ma5'], prev['ma10'], prev['ma20']

        is_today = (i == today_idx)
        ma20_below = (ma20 < ma5) or (ma20 < ma10)

        # 开盘阶段：涨停顺延补离（开盘未封涨停 → 开盘卖出；封板继续拿）
        if state == 'lu_pending' and not mt.is_limit_up(o, prev_c, code, stock_name):
            state = 'flat'
            if is_today:
                signals_today.append(('卖出', '大盘破位开盘补离', o))

        # 收盘阶段：顺延期（开盘仍封涨停的情况）
        if state == 'lu_pending':
            if c < ma5:
                state = 'flat'
                if is_today:
                    signals_today.append(('卖出', '收盘跌破MA5', c))
            elif mt.is_on(row.name):
                state = 'in'  # 指数重回线上，取消离场
            # 否则继续顺延
            continue

        # 持仓中：检查卖出信号（收盘<MA5 或 大盘择时破位离场）
        if state == 'in':
            if c < ma5:
                state = 'flat'
                if is_today:
                    signals_today.append(('卖出', '收盘跌破MA5', c))
            elif mt.should_exit(row.name):
                if mt.lu_exempt_enabled() and mt.is_limit_up(c, prev_c, code, stock_name):
                    # 破位当日涨停：顺延，次日开盘未封板再卖
                    state = 'lu_pending'
                    if is_today:
                        signals_today.append(('卖出', '破位涨停·顺延次日开盘', c))
                else:
                    state = 'flat'
                    if is_today:
                        signals_today.append(('卖出', '大盘破位离场', c))
            continue

        # 空仓：检查买入信号（信号1「开盘上MA5」已移除，仅三个收盘买入信号）
        # 与引擎一致：按 阳线上穿MA5 → 开盘上MA5阳 → 收盘破MA5 优先级互斥取一个
        if state == 'flat':
            buy_signal = None
            bull_cross = (prev_c <= prev_ma5 and c > ma5 and c > o)
            open_above_ma5_bull = (o > ma5 and c > o)
            ma_order = (ma20 > ma10) or (ma10 > ma5)
            break_ma5 = (prev_c <= prev_ma5 and c > ma5)
            if ma20_below and bull_cross:
                buy_signal = '阳线上穿MA5'
            elif ma20_below and open_above_ma5_bull:
                buy_signal = '开盘上MA5阳'
            elif ma_order and break_ma5:
                buy_signal = '收盘破MA5'

            if buy_signal and mt.can_open(row.name):
                state = 'in'  # 触发买入后进入持仓
                if is_today:
                    signals_today.append(('买入', buy_signal, c))

    return signals_today


# ── main.py 影线策略（PullbackStrategy）支持 ──
_main_ns = None


def load_main_ns():
    """exec main.py 到创建 cerebro 之前，取影线策略常量/函数。
    参数（LOW_OPEN_PCT/STOP_LOSS_PCT/limit_up_price）直接取自 main，
    main 改参数这里自动跟随；多进程下每个子进程首次使用时加载一次。"""
    global _main_ns
    if _main_ns is not None:
        return _main_ns
    main_file = os.path.join(SRC_DIR, 'main.py')
    saved_argv = sys.argv
    sys.argv = [main_file, '600206.SH']   # 占位：main.py 顶部 argparse 严格解析
    try:
        src = open(main_file, encoding='utf-8').read()
        src = src[:src.index('# 创建 cerebro')]
        nns = {'__file__': os.path.abspath(main_file), 'sys': sys}
        exec(compile(src, main_file, 'exec'), nns)
    finally:
        sys.argv = saved_argv
    _main_ns = nns
    return _main_ns


def check_pullback(df, code=None, stock_name='', collect_all=False):
    """main.py PullbackStrategy 三信号（回踩A/上穿B/影线C）逐日状态机，返回目标日的信号列表。
    collect_all=True 时返回区间内全部事件 [(日期Timestamp, 方向, 原因, 价格)]，
    仅供与 backtrader 逐笔对账（process_file 不使用）。
    精确复刻 backtrader cheat_on_open + coc/coo 时序：
    - next(T) 的 coc 单按 T 收盘价成交、T+1 开盘后结算（main fills 记 T 日）；
    - next_open 的 coc=False 单按当日开盘价立即成交，卖出当根仍可重新买回；
    - 买入三信号 if/elif 优先级 A 回踩 > B 上穿 > C 影线（同一根K线只取一个）；
    - 卖出分层（按持仓信号）：
      影线C：开盘低开1%/买入价-5%开盘卖；收盘盘中触价5%/3倍大阴/收盘止损，封板走 limit_wait；
      上穿B：T+1 高开(开>T收)开盘卖；T+1 收盘判包孕（今日实体被T实体包住则持有），
             不包孕 T+1 收盘卖；包孕则 T+2 开盘未高于 T+1 开盘即开盘卖，否则 T+2 收盘卖；
      回踩A：T+1 收盘价卖；
    - 大盘破位离场对三类持仓统一生效，优先级高于信号自身卖出；
    - 卖出规则优先级：开盘补离/低开止损/上穿高开 > 大盘破位 > 各信号收盘规则；
    - 涨停顺延双状态：limit_wait（影线止盈日封板，10%硬编码）、
      timing_wait（破位日封板，mt.is_limit_up 含ST/双创幅度）。"""
    if len(df) < 25:
        return None
    import math
    mns = load_main_ns()
    LOW_OPEN_PCT = mns['LOW_OPEN_PCT']
    STOP_LOSS_PCT = mns['STOP_LOSS_PCT']
    limit_up_price = mns['limit_up_price']

    # 与 main.py 回测同起点（feed fromdate=START_DATE，SMA 从该段起点重新累积，
    # 路径/持仓才与 main 一致；不能用全历史，否则 2016 年起的旧信号路径不同）
    d = df[df.index >= pd.Timestamp(mns['START_DATE'])].copy()
    if len(d) < 25:
        return None
    close = d['close'].values
    opens = d['open'].values
    highs = d['high'].values
    lows = d['low'].values

    def fsum_ma(arr, period):
        out = [float('nan')] * len(arr)
        for k in range(period - 1, len(arr)):
            out[k] = math.fsum(arr[k - period + 1:k + 1]) / period
        return out

    ma5 = fsum_ma(close, 5)
    ma10 = fsum_ma(close, 10)
    ma20 = fsum_ma(close, 20)

    target = d.index[-1]
    if args.date:
        t = pd.Timestamp(args.date)
        if t not in d.index:
            return None
        target = t
    target_idx = d.index.get_loc(target)
    if target_idx < 20:
        return None

    # flat=空仓 / in=持仓；pending=上一根 coc 单待本根开盘后结算；
    # position=经纪侧已结算持仓；limit_wait/timing_wait=两类涨停顺延
    # entry_signal=持仓开仓信号（回踩/上穿/影线）；harami_wait=上穿 T+1 包孕持有到 T+2
    state = 'flat'
    pending = None
    position = False
    entry_price = 0.0
    entry_signal = ''
    limit_wait = False
    timing_wait = False
    harami_wait = False
    signals_today = []
    all_events = []

    def emit(side, reason, px, immediate=False):
        # immediate=True=next_open 的 coo 单（当根开盘即成交结算）；
        # 其余为 next 的 coc 单——backtrader 在 feed 最后一根下单后再无 broker 周期，
        # 不会成交/通知，故 collect_all 对账时剔除最后一根的 coc 事件
        if collect_all:
            if not immediate and i == target_idx:
                return
            all_events.append((bar_dt, side, reason, px))
        elif is_today:
            signals_today.append((side, reason, px))

    for i in range(20, target_idx + 1):
        o, h, l, c = opens[i], highs[i], lows[i], close[i]
        po, pc = opens[i - 1], close[i - 1]
        bar_dt = d.index[i]
        is_today = (i == target_idx)
        open_sold = False

        # ══ 开盘阶段（next_open）：state != 'in' 时直接 return ══
        if state == 'in':
            if timing_wait:
                # 破位顺延：开盘未封涨停 → 开盘价卖（coc=False 当根立即成交）
                if not mt.is_limit_up(o, pc, code, stock_name):
                    emit('卖出', '大盘破位开盘补离', o, immediate=True)
                    timing_wait = False
                    harami_wait = False
                    entry_signal = ''
                    state = 'out_pending'
                    position = False      # coo 单当根开盘已成交，持仓立即清零
                    open_sold = True
            elif entry_signal == '影线':
                open_stop = o <= entry_price * (1 - STOP_LOSS_PCT)
                low_open = o <= pc * (1 - LOW_OPEN_PCT)
                if low_open or open_stop:
                    emit('卖出', '开盘止损卖' if open_stop else '低开开盘卖', o,
                         immediate=True)
                    limit_wait = False
                    entry_signal = ''
                    state = 'out_pending'
                    position = False      # coo 单当根开盘已成交，持仓立即清零
                    open_sold = True
            elif entry_signal == '上穿':
                if harami_wait:
                    # T+2 开盘：未高于 T+1 开盘价（开 <= T+1开=po）→ 开盘价卖
                    if o <= po:
                        emit('卖出', '上穿T+2开盘卖', o, immediate=True)
                        harami_wait = False
                        entry_signal = ''
                        state = 'out_pending'
                        position = False
                        open_sold = True
                else:
                    # T+1 开盘：高开（开 > 买入日T收盘=pc）→ 开盘直接卖出
                    if o > pc:
                        emit('卖出', '上穿高开开盘卖', o, immediate=True)
                        entry_signal = ''
                        state = 'out_pending'
                        position = False
                        open_sold = True
            # 回踩持仓：开盘阶段无动作，等 T+1 收盘卖

        # ══ 结算上一根 coc 收盘单（next_open 之后、next 之前）══
        if pending == 'buy':
            position = not open_sold   # 同根开盘又卖出则净持仓 0
        elif pending == 'sell':
            position = False
        pending = None

        # ══ 收盘阶段（next）══
        if state == 'out_pending':
            if position:
                continue            # 卖单尚未结算落地，不动作
            state = 'flat'          # 已平仓，本根继续扫描新信号

        if state == 'in' and position:
            # 1) 大盘择时（should_exit 在线下持续为真；timing_wait 期间站回线上则取消）
            if timing_wait and mt.is_on(bar_dt):
                timing_wait = False
            if not timing_wait and mt.should_exit(bar_dt):
                if mt.lu_exempt_enabled() and mt.is_limit_up(c, pc, code, stock_name):
                    timing_wait = True
                    emit('卖出', '破位涨停·顺延次日开盘', c)
                else:
                    emit('卖出', '大盘破位', c)
                    pending = 'sell'
                    state = 'out_pending'
                    position = False
                    harami_wait = False
                    entry_signal = ''
                    continue

            if entry_signal == '影线':
                # 2) 影线卖出（timing_wait 当日 main 也会继续落到此块，按原样复刻）
                if limit_wait:
                    is_lu = c >= limit_up_price(pc) - 0.001
                    if is_lu and o > pc:
                        continue        # 仍涨停且高开，继续持有
                    emit('卖出', '涨停顺延结束收盘卖', c)
                    pending = 'sell'
                    state = 'out_pending'
                    position = False
                    limit_wait = False
                    entry_signal = ''
                    continue

                body_today = o - c                       # 阴线为正
                body_prev = abs(pc - po)
                big_bear = (body_today > 0 and body_today >= 3 * body_prev
                            and c < po and c < pc)
                stop_loss = c <= entry_price * (1 - STOP_LOSS_PCT)
                take_profit = h >= o * 1.05      # main.py：盘中最高价 ≥ 开盘价×1.05
                if take_profit or big_bear or stop_loss:
                    # main 自身涨停判定：固定 10%、昨收涨停价四舍五入到分
                    if c >= limit_up_price(pc) - 0.001:
                        limit_wait = True
                        emit('卖出', '止盈触发·涨停顺延', c)
                    else:
                        if stop_loss:
                            reason = '收盘止损卖'
                        elif big_bear:
                            reason = '大阴线卖'
                        else:
                            reason = '盘中触价止盈'
                        emit('卖出', reason, c)
                        pending = 'sell'
                        state = 'out_pending'
                        position = False
                        entry_signal = ''
                continue

            if entry_signal == '上穿' and harami_wait:
                # T+2 仍持仓（T+2 开盘高于 T+1 开盘、未在开盘卖）→ T+2 收盘价卖
                emit('卖出', '上穿T+2收盘卖', c)
                pending = 'sell'
                state = 'out_pending'
                position = False
                harami_wait = False
                entry_signal = ''
                continue

            if entry_signal == '上穿':
                # T+1 收盘：今日实体是否被买入日 T 实体包住（包孕线）
                # T 即上一根：开 po / 收 pc
                t_hi = max(po, pc)
                t_lo = min(po, pc)
                d_hi = max(o, c)
                d_lo = min(o, c)
                if d_hi <= t_hi and d_lo >= t_lo:
                    harami_wait = True       # 包孕：持有到 T+2（不卖）
                else:
                    emit('卖出', '上穿T+1收盘卖', c)
                    pending = 'sell'
                    state = 'out_pending'
                    position = False
                    entry_signal = ''
                continue

            # 回踩：T+1 收盘价卖
            emit('卖出', '回踩T+1收盘卖', c)
            pending = 'sell'
            state = 'out_pending'
            position = False
            entry_signal = ''
            continue

        # ══ 空仓：扫描买入信号（A 回踩 > B 上穿 > C 影线，同 main.py if/elif）══
        eps = 1e-9 * ma20[i]
        # A：当日开盘在线上、盘中跌破（收在线下）+ 未跳空低开 + MA10/MA20 涨速为正
        #    + 今日K线不被昨日完全包住（大阴吞噬不买）
        ma10_up = ma10[i] > ma10[i - 1]
        ma20_up = ma20[i] > ma20[i - 1]
        engulfs = (h >= highs[i - 1] and l <= lows[i - 1])
        cond_a = ((o >= ma20[i] - eps and c < ma20[i] - eps)
                  and o >= pc and ma10_up and ma20_up and not engulfs)
        # B：当日开盘在线下、盘中上穿（收在线上）+ MA20 斜率为负（无eps，同main）+ MA10 涨速为正
        ma20_down_raw = ma20[i] < ma20[i - 1]
        cond_b = ((o <= ma20[i] + eps and c > ma20[i] + eps)
                  and ma20_down_raw and ma10_up)
        # C：下影线 ≥ 实体6倍 + 斜率结构 + 最低跌破昨日开收 + 实体上沿重叠
        body = abs(c - o)
        lower_shadow = min(o, c) - l
        ma5_down = ma5[i] < ma5[i - 1] - eps
        ma10_down = ma10[i] < ma10[i - 1] - eps
        ma20_down = ma20[i] < ma20[i - 1] - eps
        slopes_down = sum([ma5_down, ma10_down, ma20_down]) >= 2
        ma5_slope = ma5[i] - ma5[i - 1]
        ma10_slope = ma10[i] - ma10[i - 1]
        ma20_slope = ma20[i] - ma20[i - 1]
        ma5_slope_smallest = ma5_slope < ma10_slope and ma5_slope < ma20_slope
        dips_below_prev = l < po and l < pc
        overlap_ok = max(o, c) >= min(po, pc) * (1 - 0.0015)
        cond_c = (lower_shadow >= 6.0 * body
                  and (ma5_slope_smallest or slopes_down)
                  and dips_below_prev and overlap_ok)
        if cond_a and mt.can_open(bar_dt):
            emit('买入', '回踩', c)
            entry_price = c
            entry_signal = '回踩'
            limit_wait = False
            timing_wait = False
            harami_wait = False
            pending = 'buy'
            state = 'in'
        elif cond_b and mt.can_open(bar_dt):
            emit('买入', '上穿', c)
            entry_price = c
            entry_signal = '上穿'
            limit_wait = False
            timing_wait = False
            harami_wait = False
            pending = 'buy'
            state = 'in'
        elif cond_c and mt.can_open(bar_dt):
            emit('买入', '影线', c)
            entry_price = c
            entry_signal = '影线'
            limit_wait = False
            timing_wait = False
            harami_wait = False
            pending = 'buy'
            state = 'in'

    return all_events if collect_all else signals_today


def run_backtest(df, return_trades=False, code=None, stock_name='', vol_target=None, fixed_scale=None, start_date=None, end_date=None):
    """纯 numpy 快速回测，精确复刻 strategy_normal 的 backtrader coc+coo 执行模型。

    实测 backtrader 撃合时序（cerebro cheat_on_open + broker coc=True + coo=True）：
    - coc=True 覆盖 coo：所有市价单均按【创建根收盘价】成交
      （bbroker._try_exec_market: exprice = order.created.pclose）
    - 信号1「开盘上MA5开盘价买入」已于2026-09移除：next_open 仅保留涨停顺延补离卖出
    - next(T) 创建的收盘单在 T+1 的 next_open 之后、next 之前结算：
      next_open(T+1) 看到旧持仓/现金（out_pending 时不动作），
      next(T+1) 起看到新状态，卖出结算后当根还可按收盘信号重新买入
    - 最后一根 next 创建的收盘单无后续 K 线，backtrader 不会成交（期末按收盘价估值）
    """
    trade_log = []
    try:
        import numpy as np
        seg = df[(df.index >= (start_date or YEAR_START) - pd.Timedelta(days=60)) & (df.index <= (end_date or YEAR_END))].copy()
        if len(seg) < 25:
            return (0.0, 0.0, 0, [], None) if return_trades else (0.0, 0.0, 0, None)

        close = seg['close'].values
        opens = seg['open'].values
        # 必须用 math.fsum 逐窗口求和，精确复刻 backtrader 的 Average/SMA
        # （basicops.Average: math.fsum(window)/period）；
        # pandas rolling.mean 的浮点误差会在 close≈MA 边界翻转大小比较
        import math

        def fsum_ma(arr, period):
            out = np.full(len(arr), np.nan)
            for k in range(period - 1, len(arr)):
                out[k] = math.fsum(arr[k - period + 1:k + 1]) / period
            return out

        ma5 = fsum_ma(close, 5)
        ma10 = fsum_ma(close, 10)
        ma20 = fsum_ma(close, 20)

        # ── Vol Targeting：预计算 20 日年化波动率（口径同 L1005）──
        # vol_target=30 → 目标年化波动 30%；个股 60% → 半仓，25% → 满仓(cap 1.0)
        # fixed_scale=0.5 → 固定半仓（与 vol_target 互斥，fixed_scale 优先）
        vol20 = None
        if vol_target is not None:
            rets = np.diff(close) / close[:-1]          # 日简单收益
            vol20 = np.full(len(close), np.nan)
            for k in range(20, len(close)):
                vol20[k] = np.std(rets[k - 20:k], ddof=1) * np.sqrt(252)

        cash = INIT_CASH
        position = 0
        entry_cost = 0.0   # 当前持仓买入总成本（含手续费）
        state = 'flat'     # flat / in / out_pending
        # 上一根 next 创建的收盘单，待本根收盘阶段结算：
        # ('buy'/'sell', qty, price, fee, buy_cost)，buy_cost 仅 sell 时用于统计胜负
        pending = None
        trades = 0
        wins = 0
        navs = []          # 每根K线收盘盯市净值（已结算仓位，与 broker.getvalue 同口径）

        def calc_qty(avail_cash, ref_price):
            """复刻 FullLotSizer：用参考价反推整手股数"""
            qty = int(avail_cash / (ref_price * (1 + COMMISSION)) // 100) * 100
            while qty > 0:
                fee = max(qty * ref_price * COMMISSION, MIN_COMM)
                if qty * ref_price + fee <= avail_cash + 1e-6:
                    break
                qty -= 100
            return qty

        # backtrader：next() 在 SMA20 首次有效的那根（i=19）即开始，
        # 但 next_open() 要到下一根（i=20）才首次被调用（就绪根不会排队 open）
        for i in range(19, len(seg)):
            o, c = opens[i], close[i]
            pc = close[i - 1]
            m5, m10, m20 = ma5[i], ma10[i], ma20[i]
            pm5, pm10, pm20 = ma5[i - 1], ma10[i - 1], ma20[i - 1]

            # ══ 开盘阶段（next_open）：仅处理涨停顺延补离卖出；pending(T-1收盘单) 尚未结算 ══
            if i >= 20:
                if state == 'out_pending':
                    if position == 0:
                        state = 'flat'
                # 涨停顺延补离：开盘未封涨停即以开盘价立即卖出（cheat_on_open），
                # 卖出后交给收盘阶段恢复空仓，本根仍可按收盘信号买回；
                # 开盘继续封板则持有到收盘阶段再判
                if state == 'lu_pending' and position > 0 and pending is None:
                    if not mt.is_limit_up(o, pc, code, stock_name):
                        fee = max(position * o * COMMISSION, MIN_COMM) + position * o * STAMP
                        cash += position * o - fee
                        trades += 1
                        if position * o - fee > entry_cost:
                            wins += 1
                        if return_trades:
                            trade_log.append((seg.index[i].strftime('%Y-%m-%d'),
                                              'sell', o, -position, '大盘破位补离'))
                        position = 0
                        state = 'flat'

            # ══ 收盘阶段（next）：结算上一根收盘单 ══
            if pending is not None:
                side, qty, price, fee, bcost = pending
                if side == 'buy':
                    # backtrader 佣金方案参数误写为 stock=True（正确名 stocklike），
                    # 被忽略后 _stocklike=False、margin 兜底为 1.0 元/股，实际成为
                    # 期货式账户：买入扣款 = 股数×1.0 + 佣金（与股价无关）。
                    # 股价<1元时 sizer 按全价算出的股数会因保证金不足被 Margin 拒单，
                    # 订单作废且 state 永久卡在 'in'（backtrader 实测行为）
                    if (cash - float(qty)) - fee < 0.0:
                        state = 'in'
                        if return_trades and trade_log:
                            trade_log.pop()
                    else:
                        cash -= qty * price + fee
                        position = qty
                        entry_cost = qty * price + fee
                else:  # sell
                    cash += qty * price - fee
                    position = 0
                    trades += 1
                    if qty * price - fee > bcost:
                        wins += 1
                pending = None

            if state == 'out_pending':
                # 卖出刚结算，position 已为 0 → 恢复空仓，同根可再按收盘信号买
                if position != 0:
                    navs.append(cash + position * c)
                    continue
                state = 'flat'

            if state == 'in':
                if position > 0:
                    # 收盘 < MA5 → 收盘价卖出（coc，次根收盘阶段结算）
                    sell_reason = ''
                    if c < m5:
                        pass
                    elif mt.should_exit(seg.index[i]):
                        # 大盘择时：指数收盘跌破 MA，持仓当日收盘离场
                        # （收盘买单次根才结算，结算当日即可离场，与 MA5 卖出规则一致）
                        if mt.lu_exempt_enabled() and mt.is_limit_up(c, pc, code, stock_name):
                            # 破位当日个股涨停：顺延，之后开盘未封板即走
                            state = 'lu_pending'
                        else:
                            sell_reason = '大盘破位'
                    if state != 'lu_pending' and (c < m5 or sell_reason):
                        fee = max(position * c * COMMISSION, MIN_COMM) + position * c * STAMP
                        pending = ('sell', position, c, fee, entry_cost)
                        state = 'out_pending'
                        if return_trades:
                            trade_log.append((seg.index[i].strftime('%Y-%m-%d'),
                                              'sell', c, -position, sell_reason))
            elif state == 'lu_pending':
                # 开盘仍封板而持有到收盘：跌破MA5则收盘卖；指数重回线上取消离场
                if position > 0:
                    if c < m5:
                        fee = max(position * c * COMMISSION, MIN_COMM) + position * c * STAMP
                        pending = ('sell', position, c, fee, entry_cost)
                        state = 'out_pending'
                        if return_trades:
                            trade_log.append((seg.index[i].strftime('%Y-%m-%d'),
                                              'sell', c, -position, ''))
                    elif mt.is_on(seg.index[i]):
                        state = 'in'
            else:
                # 空仓：信号2/3/4，收盘价买入（coc，次根收盘阶段结算）
                ma20_below = (m20 < m5) or (m20 < m10)
                bull_cross = (pc <= pm5 and c > m5 and c > o)
                open_above_ma5_bull = (o > m5 and c > o)
                ma_order = (m20 > m10) or (m10 > m5)
                break_ma5 = (pc <= pm5 and c > m5)

                sig_name = None
                if ma20_below and bull_cross:
                    sig_name = '阳线上穿MA5'
                elif ma20_below and open_above_ma5_bull:
                    sig_name = '开盘上MA5阳'
                elif ma_order and break_ma5:
                    sig_name = '收盘破MA5'
                if sig_name and mt.can_open(seg.index[i]):
                    f = mt.position_factor(seg.index[i])
                    # Vol Targeting 仓位缩放（不改信号，只改每笔买入量）
                    vol_scale = 1.0
                    if fixed_scale is not None:
                        vol_scale = fixed_scale
                    elif vol_target is not None and vol20 is not None:
                        v = vol20[i]
                        if np.isfinite(v) and v > 0:
                            vol_scale = min(vol_target / 100.0 / v, 1.0)
                    qty = int(calc_qty(cash, c) * f * vol_scale / 100) * 100
                    if qty > 0:
                        fee = max(qty * c * COMMISSION, MIN_COMM)
                        pending = ('buy', qty, c, fee, 0.0)
                        state = 'in'
                        if return_trades:
                            trade_log.append((seg.index[i].strftime('%Y-%m-%d'),
                                              'buy', c, qty, sig_name))

            # 当日收盘盯市净值（当根新建 pending 单尚未结算，按下一根结算前
            # 的已结算状态记账——与状态机其余部分口径一致）
            navs.append(cash + position * c)

        # 期末：最后一根 pending 收盘单在 backtrader 中无后续K线撮合，作废；
        # 持仓按最后收盘价估值（与 broker.getvalue 口径一致）
        if pending is not None and return_trades and trade_log:
            trade_log.pop()  # 撤掉未成交单在创建时记录的日志
        last_c = close[-1]
        total_value = cash + position * last_c
        ret = (total_value / INIT_CASH - 1) * 100
        win_rate = (wins / trades * 100) if trades > 0 else 0
        nav_arr = np.asarray(navs, dtype=float)
        nav_rets = nav_arr[1:] / nav_arr[:-1] - 1.0 if len(nav_arr) >= 2 else np.array([])
        bt_sharpe = sharpe_daily(nav_rets)
        if return_trades:
            return ret, win_rate, trades, trade_log, bt_sharpe
        return ret, win_rate, trades, bt_sharpe
    except Exception:
        return (0.0, 0.0, 0, [], None) if return_trades else (0.0, 0.0, 0, None)


def process_file(f):
    """处理单个文件（用于多进程）"""
    try:
        if '#' in os.path.basename(f):
            result = load_local_csv(f)
        else:
            result = load_cache(f)
        if result[0] is None:
            return None
        df, name = result

        basename = os.path.basename(f).replace('.csv', '')
        if '#' in basename:
            parts = basename.split('#')
            ts_code = f'{parts[1]}.{parts[0]}'
        else:
            ts_code = basename

        # cache_data 文件无名称 → 从 _stock_list.csv 或 baostock 行业文件补
        if not name and ts_code in _STOCK_NAMES:
            name = _STOCK_NAMES[ts_code]
        if not name and ts_code in _CSRC_NAMES:
            name = _CSRC_NAMES[ts_code]
        industry = classify_industry(name, ts_code)

        # 信号日量比/成交额/乖离（5年实证口径；仅展示，不参与信号）：
        # 量比 = 信号日成交量 / 前20日均量（单位无关）。
        # 成交额（亿元）：tushare cache 自带 amount 列（千元，÷1e5）优先；
        # 无则量×价估算——stockdb_csv volume 单位为股，tushare vol 单位为手（×100）。
        # 乖离% = (收盘-MA20)/MA20×100（环境雷达与深乖离标记用）。
        is_local_csv = '#' in os.path.basename(f)
        sig_date = pd.Timestamp(args.date) if args.date else df.index[-1]
        if sig_date not in df.index:
            sig_date = df.index[-1]
        vi = df.index.get_loc(sig_date)
        vol_today = float(df['volume'].iloc[vi])
        vol_ma20 = float(df['volume'].rolling(20).mean().iloc[vi])
        vol_ratio = round(vol_today / vol_ma20, 2) if vi >= 20 and vol_ma20 > 0 else None
        close_today = float(df['close'].iloc[vi])
        # 信号日涨幅% = (收盘-昨收)/昨收×100；qfq 序列日收益即真实涨跌幅
        pct_chg = round((close_today / float(df['close'].iloc[vi - 1]) - 1) * 100, 2) \
            if vi >= 1 and float(df['close'].iloc[vi - 1]) > 0 else None
        ma20_today = float(df['close'].rolling(20).mean().iloc[vi])
        bias20 = (close_today / ma20_today - 1.0) if vi >= 20 and ma20_today > 0 else None
        if 'amount' in df.columns and pd.notna(df['amount'].iloc[vi]) \
                and float(df['amount'].iloc[vi]) > 0:
            amount_yi = round(float(df['amount'].iloc[vi]) / 1e5, 2)
        else:
            amount_yi = round(vol_today * (1.0 if is_local_csv else 100.0)
                              * close_today / 1e8, 2)

        # 20日波动率%/波动率分位（仅展示，不参与信号），口径同 risk_metrics.py：
        # 20日波动率 = 近20个日简单收益（含信号日）的样本std(ddof=1)×√252 年化；
        # 波动率分位 = 当日20日波动率在该股自身历史（截至信号日，全样本约2021-08起）
        # 滚动20日波动率序列中的百分位（平均秩处理并列，0=最低，100=最高），
        # 历史滚动样本≥60个才输出。--date 回扫时只取截至该日数据，无未来函数。
        daily_ret = df['close'].pct_change()
        vol20_series = daily_ret.rolling(20).std(ddof=1) * np.sqrt(252)
        vol20_today = vol20_series.iloc[vi]
        vol20_pct = None
        if pd.notna(vol20_today):
            hist_vol = vol20_series.iloc[:vi + 1].dropna()
            if len(hist_vol) >= 60:
                vol20_pct = int(round(float(
                    hist_vol.rank(pct=True, method='average').iloc[-1]) * 100))
            vol20_today = round(float(vol20_today) * 100, 1)
        else:
            vol20_today = None
        # 个股夏普：信号日前近250个交易日（含当日）日简单收益的年化夏普
        # （买入持有口径，与策略无关；不足60个有效收益留空）
        stock_ret_win = daily_ret.iloc[max(0, vi - 249):vi + 1].dropna()
        stock_sharpe = sharpe_daily(stock_ret_win.values) if len(stock_ret_win) >= 60 else None

        # 近30/60/90个交易日涨停次数（含信号日），仅展示不参与信号。
        # 涨停幅度按板块：创业板(300/301)/科创板(688/689) 20%、北交所(43/83/87/920) 30%、
        # ST 5%、其余主板 10%；阈值留 0.2pct 容差（9.8/19.8/29.8/4.8）。
        # 序列前5个交易日不计入（双创新股前5日无涨跌幅限制，会误判）；qfq 日收益即真实涨跌幅。
        def _limit_threshold(code6, sname):
            if code6.startswith(('300', '301', '688', '689')):
                return 19.8
            if code6.startswith(('43', '83', '87', '920')):
                return 29.8
            if sname and 'ST' in str(sname).upper():
                return 4.8
            return 9.8
        _code6 = str(ts_code).split('.')[0]
        _lu_bool = (daily_ret.iloc[5:vi + 1] * 100 >= _limit_threshold(_code6, name))
        lu30 = int(_lu_bool.iloc[-30:].sum())
        lu60 = int(_lu_bool.iloc[-60:].sum())
        lu90 = int(_lu_bool.iloc[-90:].sum())

        # 首板抱团雷达：全市场横截面（无论是否有信号，每只都返回）
        # 首板 = 当日涨幅≥9.5% 且 昨未涨停（与历史验证口径一致，比板块阈值 9.8 略松，吸收接近封板）
        # 跟涨候选 = 同行业非首板、|pct|<9.5、amt>0.5亿、b20非空
        _sw = _SW_INDUSTRY.get(ts_code, ('', '', ''))
        _pct_today = (close_today / float(df['close'].iloc[vi - 1]) - 1.0) if vi >= 1 \
            and float(df['close'].iloc[vi - 1]) > 0 else None
        _burst_th = 9.5 if not (name and 'ST' in str(name).upper()) else 4.5
        _burst_bool = (daily_ret.iloc[5:vi + 1] * 100 >= _burst_th)
        _lu_today = bool(_burst_bool.iloc[-1]) if len(_burst_bool) >= 1 else False
        _lu_yday = bool(_burst_bool.iloc[-2]) if len(_burst_bool) >= 2 else False
        _lu1y = int(_burst_bool.iloc[-250:].sum())
        burst_row = {
            '代码': ts_code, '名称': name, 'sw_l2': _sw[1],
            '收盘价': round(close_today, 2),
            '涨幅%': round(_pct_today * 100, 2) if _pct_today is not None else None,
            '涨停今日': _lu_today, '涨停昨日': _lu_yday,
            '首板': _lu_today and not _lu_yday,
            '量比': vol_ratio, '成交额亿': amount_yi,
            '一年涨停数': _lu1y,
            '乖离20%': round(bias20 * 100, 2) if bias20 is not None else None,
        }

        if args.strategy == 'main':
            sigs = check_pullback(df, code=ts_code, stock_name=name)
        else:
            sigs = check_signals(df, code=ts_code, stock_name=name)
        if not sigs:
            return {'rows': [], 'bias20': bias20, 'burst': burst_row}

        # main 三信号策略只扫当日信号，不跑 numpy 回测（回测列留空）
        if args.strategy == 'main':
            latest_date = sig_date
            rows = [{
                '代码': ts_code,
                '名称': name,
                '行业': industry,
                '方向': side,
                '信号': signal_type,
                '阳线上穿✅': '', '阳线次数': None, '阳线胜率%': None,
                '开盘上阳✅': '', '开阳次数': None, '开阳胜率%': None,
                '收盘抄底✅': '', '抄底次数': None, '抄底胜率%': None,
                '收盘价': round(price, 2),
                '涨幅%': pct_chg,
                '涨停30': lu30, '涨停60': lu60, '涨停90': lu90,
                '量比': vol_ratio,
                '成交额(亿)': amount_yi,
                '20日波动率%': vol20_today,
                '波动率分位': vol20_pct,
                '个股夏普': stock_sharpe,
                '乖离%': round(bias20 * 100, 1) if bias20 is not None else None,
                '日期': latest_date.strftime('%Y-%m-%d'),
                '回测收益%': None, '回测夏普': None, '回测胜率%': None, '回测交易数': None,
            } for side, signal_type, price in sigs]
            return {'rows': rows, 'bias20': bias20, 'burst': burst_row}

        ret, win_rate, n_trades, tlog, bt_sharpe = run_backtest(df, code=ts_code, stock_name=name,
                                                                 return_trades=True,
                                                                 vol_target=VOL_TARGET)
        buy_names = [t[4] for t in tlog if t[1] == 'buy']
        n_bull_cross = buy_names.count('阳线上穿MA5')
        n_open_bull = buy_names.count('开盘上MA5阳')
        n_rebound = buy_names.count('收盘破MA5')

        # 分信号类型的胜率：按 tlog 逐笔配对买卖，含佣金/印花税
        from collections import Counter
        sig_wins, sig_tot = Counter(), Counter()
        bpx = bsig = 0.0; pos = 0
        for _, side, px, qty, sname in tlog:
            if side == 'buy':
                pos, bpx, bsig = qty, px, sname
            elif side == 'sell' and pos > 0:
                cost = pos * bpx * (1 + COMMISSION)
                proceeds = pos * px * (1 - COMMISSION - STAMP)
                sig_tot[bsig] += 1
                sig_wins[bsig] += int(proceeds >= cost)
                pos = 0

        def sig_winrate(nm):
            return round(sig_wins[nm] / sig_tot[nm] * 100, 1) if sig_tot[nm] else ''
        w_bull_cross = sig_winrate('阳线上穿MA5')
        w_open_bull = sig_winrate('开盘上MA5阳')
        w_rebound = sig_winrate('收盘破MA5')
        latest_date = sig_date

        rows = []
        for side, signal_type, price in sigs:
            rows.append({
                '代码': ts_code,
                '名称': name,
                '行业': industry,
                '方向': side,
                '信号': signal_type,
                '阳线上穿✅': '✅' if signal_type == '阳线上穿MA5' else '',
                '阳线次数': n_bull_cross,
                '阳线胜率%': w_bull_cross,
                '开盘上阳✅': '✅' if signal_type == '开盘上MA5阳' else '',
                '开阳次数': n_open_bull,
                '开阳胜率%': w_open_bull,
                '收盘抄底✅': '✅' if signal_type == '收盘破MA5' else '',
                '抄底次数': n_rebound,
                '抄底胜率%': w_rebound,
                '收盘价': round(price, 2),
                '涨幅%': pct_chg,
                '涨停30': lu30, '涨停60': lu60, '涨停90': lu90,
                '量比': vol_ratio,
                '成交额(亿)': amount_yi,
                '20日波动率%': vol20_today,
                '波动率分位': vol20_pct,
                '个股夏普': stock_sharpe,
                '乖离%': round(bias20 * 100, 1) if bias20 is not None else None,
                '日期': latest_date.strftime('%Y-%m-%d'),
                '回测收益%': round(ret, 2),
                '回测夏普': bt_sharpe,
                '回测胜率%': round(win_rate, 1),
                '回测交易数': n_trades
            })
        return {'rows': rows, 'bias20': bias20, 'burst': burst_row}
    except Exception:
        return None


def main():
    import multiprocessing as mp

    files = sorted(glob.glob(os.path.join(DATA_DIR, '*.csv')))

    print(f'扫描目录: {DATA_DIR}  共 {len(files)} 个文件')
    if args.date:
        print(f'指定日期: {args.date}')
    print(f'大盘择时: {mt.describe()}')
    if VOL_TARGET is not None:
        print(f'Vol Targeting: 目标年化波动 {VOL_TARGET}%（仓位=min({VOL_TARGET}%/个股20日波动率, 100%)）')
    print()

    # 多进程并行处理
    n_procs = min(mp.cpu_count(), 8)
    print(f'使用 {n_procs} 进程并行处理...')
    with mp.Pool(n_procs) as pool:
        results = pool.map(process_file, files, chunksize=20)

    buy_signals = []
    sell_signals = []
    biases = []   # 全市场信号日 bias20（含无信号个股，供环境雷达统计广度）
    burst_pool = []  # 首板抱团雷达：全市场横截面
    for r in results:
        if r:
            if r.get('bias20') is not None:
                biases.append(r['bias20'])
            if r.get('burst') is not None:
                burst_pool.append(r['burst'])
            for row in r['rows']:
                if row['方向'] == '买入':
                    buy_signals.append(row)
                else:
                    sell_signals.append(row)

    # 排序（影线模式无回测列，强制按信号类型排序）
    sort_key = {'return': '回测收益%', 'win_rate': '回测胜率%', 'signal': '信号'}
    if args.sort != 'signal' and args.strategy != 'main':
        buy_signals.sort(key=lambda x: x[sort_key[args.sort]], reverse=True)
        sell_signals.sort(key=lambda x: x[sort_key[args.sort]], reverse=True)
    else:
        buy_signals.sort(key=lambda x: x['信号'])
        sell_signals.sort(key=lambda x: x['信号'])

    # 优质股：当日有买入信号，且回测收益/胜率达标（默认收益≥50%、胜率≥40%）
    # 影线模式只扫信号不跑回测，无优质股筛选
    if args.strategy == 'main':
        elite = []
    else:
        elite = [r for r in buy_signals
                 if r['回测收益%'] >= args.min_ret and r['回测胜率%'] >= args.min_win]
        elite.sort(key=lambda x: (x['回测收益%'], x['回测胜率%']), reverse=True)

    # 写入 Excel：按扫描日期命名（signals_YYYYMMDD.xlsx；影线策略加 main_ 前缀；
    # 若被占用则加 _new）
    if args.date:
        scan_date = args.date.replace('-', '')
    elif buy_signals or sell_signals:
        scan_date = max(r['日期'] for r in buy_signals + sell_signals).replace('-', '')
    else:
        scan_date = str(max(mt.regime_map().keys()).date()).replace('-', '')
    prefix = 'signals_main_' if args.strategy == 'main' else 'signals_'
    out_file = os.path.join(OUTPUT_DIR, f'{prefix}{scan_date}.xlsx')
    if os.path.exists(out_file):
        try:
            os.remove(out_file)
        except PermissionError:
            out_file = out_file.replace('.xlsx', '_new.xlsx')
            print(f'  注意：原 Excel 文件被占用，改写入 {out_file}')

    # ── 环境雷达：指数线上 + 全市场深乖离广度（5年校准，仅崩盘V反月亮灯）──
    scan_dt = scan_date[:4] + '-' + scan_date[4:6] + '-' + scan_date[6:8]
    idx_on = mt.regime_map().get(pd.Timestamp(scan_dt), 1.0) >= 1.0
    n_bias10 = sum(b < -0.10 for b in biases)
    n_bias15 = sum(b < -0.15 for b in biases)
    radar_lit = idx_on and n_bias15 >= RADAR_D15_TH

    # ── 首板抱团雷达：同 sw_l2 行业当日首板数≥BURST_MIN_N → 该行业跟涨候选（不做硬过滤，仅展示）──
    burst_sheet_rows, burst_hot = [], []
    if burst_pool:
        bp = pd.DataFrame(burst_pool)
        bp = bp[bp['sw_l2'].astype(bool)]  # 行业未知的不参与
        # 当日各行业首板数（黑名单仍统计，仅候选剔除）
        first_cnt = bp[bp['首板']].groupby('sw_l2').size().rename('首板数')
        bp = bp.join(first_cnt, on='sw_l2')
        bp['首板数'] = bp['首板数'].fillna(0).astype(int)
        burst_hot = first_cnt[first_cnt >= BURST_MIN_N].sort_values(ascending=False)
        # 跟涨候选：非首板、|pct|<9.5、成交额>0.5亿、b20非空、剔除黑名单行业
        cand = bp[(~bp['首板']) & (bp['首板数'] >= BURST_MIN_N)
                  & bp['涨幅%'].notna() & (bp['涨幅%'].abs() < 9.5)
                  & (bp['成交额亿'] > 0.5) & bp['乖离20%'].notna()
                  & (~bp['sw_l2'].isin(BURST_BLACKLIST))].copy()
        cand['口袋'] = ((cand['量比'].notna()) & (cand['量比'] < 0.8)
                        & (cand['涨幅%'] > 4) & (cand['乖离20%'] < 0))
        cand['口袋命中'] = cand['口袋'].map({True: '✅', False: ''})
        cand = cand.sort_values(['sw_l2', '成交额亿'], ascending=[True, False])
        burst_sheet_rows = cand[['代码', '名称', 'sw_l2', '收盘价', '涨幅%',
                                 '成交额亿', '量比', '一年涨停数', '乖离20%',
                                 '首板数', '口袋命中']].rename(
            columns={'sw_l2': '行业', '首板数': '当日行业首板数'}).to_dict('records')
    # 亮灯时的可执行候选：当日买入信号中深乖离 + 成交额≥0.5亿（实证流动性门槛）
    radar_picks = [r for r in buy_signals
                   if r.get('乖离%') is not None and r['乖离%'] < -10
                   and r.get('成交额(亿)', 0) >= 0.5]

    with pd.ExcelWriter(out_file, engine='openpyxl') as writer:
        cols = ['代码','名称','行业','方向','信号',
                '阳线上穿✅','阳线次数','阳线胜率%',
                '开盘上阳✅','开阳次数','开阳胜率%',
                '收盘抄底✅','抄底次数','抄底胜率%',
                '收盘价','涨幅%','涨停30','涨停60','涨停90','量比','成交额(亿)','20日波动率%','波动率分位','个股夏普','乖离%','日期',
                '回测收益%','回测夏普','回测胜率%','回测交易数']
        df_buy = pd.DataFrame(buy_signals) if buy_signals else pd.DataFrame(columns=cols)
        df_sell = pd.DataFrame(sell_signals) if sell_signals else pd.DataFrame(columns=cols)

        df_buy.to_excel(writer, sheet_name='买入信号', index=False)
        df_sell.to_excel(writer, sheet_name='卖出信号', index=False)

        df_elite = pd.DataFrame(elite) if elite else pd.DataFrame(columns=cols)
        elite_sheet = f'优质股(收益{int(args.min_ret)}+胜率{int(args.min_win)})'
        df_elite.to_excel(writer, sheet_name=elite_sheet[:31], index=False)

        # 汇总 sheet：信号数量（当日 + 回测期间分类型）
        summary_rows = [
            {'项目': '扫描日期', '信号类型': '', '数量': scan_dt},
            {'项目': '扫描股票数', '信号类型': '', '数量': len(files)},
            {'项目': '当日买入信号（只）', '信号类型': '全部', '数量': len(buy_signals)},
        ]
        for sig, cnt in pd.Series([r['信号'] for r in buy_signals]).value_counts().items():
            summary_rows.append({'项目': '当日买入信号（只）', '信号类型': sig, '数量': int(cnt)})
        summary_rows.append({'项目': '当日卖出信号（只）', '信号类型': '全部', '数量': len(sell_signals)})
        for sig, cnt in pd.Series([r['信号'] for r in sell_signals]).value_counts().items():
            summary_rows.append({'项目': '当日卖出信号（只）', '信号类型': sig, '数量': int(cnt)})
        summary_rows.append({'项目': f'优质股数（收益≥{args.min_ret:g}% 胜率≥{args.min_win:g}%）',
                             '信号类型': '', '数量': len(elite)})
        # 信号名单中各股票（按代码去重）回测窗口内三类买入信号触发次数合计（仅 normal 模式有回测）
        if args.strategy != 'main':
            uniq = {r['代码']: r for r in (buy_signals + sell_signals)}
            if uniq:
                for col in ('阳线次数', '开阳次数', '抄底次数'):
                    summary_rows.append({'项目': '回测期买入信号次数合计（信号名单股）',
                                         '信号类型': col, '数量': int(sum(r[col] for r in uniq.values()))})

        # 行业分布（买入信号按名称关键词分类）
        if buy_signals:
            for ind, cnt in pd.Series([r.get('行业', '') for r in buy_signals]).value_counts().items():
                summary_rows.append({'项目': '买入信号行业分布', '信号类型': ind, '数量': int(cnt)})

        # 环境雷达（深乖离×线上：5年校准仅崩盘V反月亮灯，详见 SKILL.md 结论库）
        summary_rows += [
            {'项目': '环境雷达·HS300相对MA20', '信号类型': '',
             '数量': '线上' if idx_on else '线下'},
            {'项目': '环境雷达·乖离<-10%个股数', '信号类型': f'有效样本{len(biases)}只',
             '数量': n_bias10},
            {'项目': f'环境雷达·乖离<-15%个股数（亮灯阈值{RADAR_D15_TH}）',
             '信号类型': '线上才判定', '数量': n_bias15},
        ]
        if radar_lit:
            summary_rows.append({'项目': '⚡V反雷达亮灯', '信号类型': '线上深乖离扎堆',
                                 '数量': f'候选买入信号 {len(radar_picks)} 只（乖离<-10%且成交额≥0.5亿）'})

        # 首板抱团（汇总 sheet 也留痕，便于事后核对扫描结果）
        summary_rows.append({'项目': f'首板抱团·首板数≥{BURST_MIN_N}行业数', '信号类型': '剔除黑名单前',
                             '数量': len(burst_hot)})
        if len(burst_hot):
            for ind, n in burst_hot.head(10).items():
                summary_rows.append({'项目': '首板抱团·热行业', '信号类型': ind, '数量': int(n)})
        summary_rows.append({'项目': '首板抱团·跟涨候选数', '信号类型': '剔黑名单后',
                             '数量': len(burst_sheet_rows)})
        summary_rows.append({'项目': '首板抱团·门控状态', '信号类型': 'HS300 MA20',
                             '数量': '线上（可人工复核）' if idx_on else '线下（仅观察勿动手）'})

        pd.DataFrame(summary_rows).to_excel(writer, sheet_name='汇总', index=False)

        # 亮灯时把可执行候选单列一个 sheet（平时不建）
        if radar_lit:
            df_radar = pd.DataFrame(radar_picks) if radar_picks else pd.DataFrame(columns=cols)
            df_radar.to_excel(writer, sheet_name='V反雷达候选', index=False)

        # 首板抱团雷达：有热行业或候选就建 sheet（只展示不交易，等 MA20 线上人工复核）
        if len(burst_hot) or burst_sheet_rows:
            bcols = ['代码', '名称', '行业', '收盘价', '涨幅%', '成交额亿', '量比',
                     '一年涨停数', '乖离20%', '当日行业首板数', '口袋命中']
            df_burst = pd.DataFrame(burst_sheet_rows) if burst_sheet_rows else pd.DataFrame(columns=bcols)
            df_burst.to_excel(writer, sheet_name='首板抱团雷达', index=False)
            # 行业汇总 sheet（含黑名单行业，便于对照）
            hot_rows = [{'行业': ind, '当日首板数': int(n),
                         '黑名单': '⚫' if ind in BURST_BLACKLIST else ''}
                        for ind, n in burst_hot.items()]
            pd.DataFrame(hot_rows).to_excel(writer, sheet_name='首板行业分布', index=False)

    # ── Excel 后处理：冻结 + 筛选 + 涨幅%正红负绿（首板抱团雷达 & 首板行业分布）──
    try:
        from openpyxl import load_workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
        wb = load_workbook(out_file)
        red, green = Font(color='FF0000'), Font(color='008000')
        for sname in ('首板抱团雷达', '首板行业分布'):
            if sname not in wb.sheetnames:
                continue
            ws = wb[sname]
            ws.freeze_panes = 'C2'
            ws.auto_filter.ref = ws.dimensions
            if sname == '首板抱团雷达':
                hdr = next((c.value for c in ws[1]), None)
                if hdr:
                    # 列名首行
                    headers = [c.value for c in ws[1]]
                    for cn in ('涨幅%', '乖离20%'):
                        if cn in headers:
                            cc = headers.index(cn) + 1
                            for r in range(2, ws.max_row + 1):
                                c = ws.cell(row=r, column=cc)
                                if c.value is not None:
                                    try:
                                        c.font = red if float(c.value) > 0 else green
                                    except Exception:
                                        pass
                for i in range(1, ws.max_column + 1):
                    col = ws.cell(row=1, column=i).value or ''
                    ws.column_dimensions[get_column_letter(i)].width = max(10, len(str(col)) * 2 + 2)
        wb.save(out_file)
    except Exception as e:
        print(f'  Excel 后处理（冻结/筛选/颜色）失败: {e}')

    # 控制台输出
    def _print_radar():
        print(f'\n环境雷达 [{scan_dt}]: HS300 {"线上" if idx_on else "线下"} | '
              f'乖离<-10% {n_bias10} 只, <-15% {n_bias15} 只（有效样本 {len(biases)}）')
        if radar_lit:
            print('⚡ V反雷达亮灯：线上深乖离扎堆（5年仅2024-02触发，当月抄底均+12%/胜率98%）')
            print(f'   今日候选买入信号 {len(radar_picks)} 只（乖离<-10% 且 成交额≥0.5亿），'
                  f'详见 Excel「V反雷达候选」sheet')

    def _print_burst():
        if not burst_pool:
            return
        print(f'\n首板抱团雷达 [{scan_dt}]: HS300 {"线上" if idx_on else "线下"}（MA20门控，仅展示）')
        if not len(burst_hot):
            print(f'   当日无 sw_l2 行业首板数≥{BURST_MIN_N}，无需跟涨')
            return
        hot_str = ' | '.join(f'{ind} {int(n)}' for ind, n in burst_hot.head(8).items())
        print(f'   热行业(首板数): {hot_str}')
        black_hit = [i for i in burst_hot.index if i in BURST_BLACKLIST]
        if black_hit:
            print(f'   黑名单触发（仅观察）: {"/".join(black_hit)}')
        if not idx_on:
            print('   ⛔ 指数线下：本期不操作，等重回 MA20 上方再人工复核')
        elif burst_sheet_rows:
            print(f'   指数线上：{len(burst_sheet_rows)} 只跟涨候选，人工复核题材持续性后择强')
            pocket = [r for r in burst_sheet_rows if r['口袋命中']]
            if pocket:
                print(f'   口袋命中（量比<0.8 & 涨>4% & b20<0，5Y +1.02/58%）: '
                      f'{" ".join(r["代码"] for r in pocket[:10])}')

    if args.strategy == 'main':
        # 影线模式：无回测列，简表；卖出在前（实盘持仓每日先看要不要卖）
        def _print_simple(title, rows):
            print(f'\n===== {title}（{len(rows)} 只）=====')
            if rows:
                print(f'{"代码":<11}{"名称":<9}{"行业":<10}{"信号":<20}{"价格":>8}{"涨幅%":>7}{"量比":>7}'
                      f'{"额亿":>7}{"波20%":>7}{"分位":>5}{"个夏":>6}{"乖离%":>7}')
                print('-' * 108)
                for r in rows:
                    pc = f'{r["涨幅%"]:+.2f}' if r.get('涨幅%') is not None else '-'
                    vr = f'{r["量比"]:.2f}' if r.get('量比') is not None else '-'
                    bi = f'{r["乖离%"]:.1f}' if r.get('乖离%') is not None else '-'
                    v20 = f'{r["20日波动率%"]:.1f}' if r.get("20日波动率%") is not None else '-'
                    vpct = f'{r["波动率分位"]}' if r.get('波动率分位') is not None else '-'
                    sh = f'{r["个股夏普"]:.2f}' if r.get('个股夏普') is not None else '-'
                    print(f'{r["代码"]:<11}{r["名称"]:<9}{r.get("行业",""):<10}{r["信号"]:<20}'
                          f'{r["收盘价"]:>8.2f}{pc:>7}{vr:>7}{r.get("成交额(亿)", 0):>7.2f}'
                          f'{v20:>7}{vpct:>5}{sh:>6}{bi:>7}')
                if len(rows) > 30:
                    print(f'... 共 {len(rows)} 只，详见 Excel')

        _print_simple('三信号策略·卖出信号', sell_signals)
        _print_simple('三信号策略·买入信号', buy_signals)
        _print_radar()
        _print_burst()
        print(f'\n汇总: 卖出 {len(sell_signals)} 只 | 买入 {len(buy_signals)} 只')
        print(f'Excel 已保存: {out_file}')
        return

    def _nw(r, mc, wc):
        return f'{r[mc]}/{r[wc]:g}' if r[wc] != '' else f'{r[mc]}/-'

    print(f'\n===== 买入信号（{len(buy_signals)} 只）=====')
    if buy_signals:
        print(f'{"代码":<11}{"名称":<9}{"行业":<10}{"信号":<13}{"收盘":>7}{"收益%":>8}{"夏普":>7}{"胜率%":>7}'
              f'{"阳线":>9}{"开阳":>9}{"抄底":>9}')
        print('-' * 107)
        for r in buy_signals[:20]:
            sh = f'{r["回测夏普"]:+.2f}' if r.get('回测夏普') is not None else '-'
            print(f'{r["代码"]:<11}{r["名称"]:<9}{r.get("行业",""):<10}{r["信号"]:<13}{r["收盘价"]:>7}'
                  f'{r["回测收益%"]:>+8.1f}{sh:>7}{r["回测胜率%"]:>7.1f}'
                  f'{("✅"+_nw(r,"阳线次数","阳线胜率%")) if r["阳线上穿✅"] else _nw(r,"阳线次数","阳线胜率%"):>9}'
                  f'{("✅"+_nw(r,"开阳次数","开阳胜率%")) if r["开盘上阳✅"] else _nw(r,"开阳次数","开阳胜率%"):>9}'
                  f'{("✅"+_nw(r,"抄底次数","抄底胜率%")) if r["收盘抄底✅"] else _nw(r,"抄底次数","抄底胜率%"):>9}')
        if len(buy_signals) > 20:
            print(f'... 共 {len(buy_signals)} 只，详见 Excel')

    print(f'\n===== 卖出信号（{len(sell_signals)} 只）=====')
    if sell_signals:
        print(f'{"代码":<11}{"名称":<9}{"行业":<10}{"信号":<13}{"收盘":>7}{"收益%":>8}{"夏普":>7}{"胜率%":>7}'
              f'{"阳线":>9}{"开阳":>9}{"抄底":>9}')
        print('-' * 107)
        for r in sell_signals[:20]:
            sh = f'{r["回测夏普"]:+.2f}' if r.get('回测夏普') is not None else '-'
            print(f'{r["代码"]:<11}{r["名称"]:<9}{r.get("行业",""):<10}{r["信号"]:<13}{r["收盘价"]:>7}'
                  f'{r["回测收益%"]:>+8.1f}{sh:>7}{r["回测胜率%"]:>7.1f}'
                  f'{_nw(r,"阳线次数","阳线胜率%"):>9}'
                  f'{_nw(r,"开阳次数","开阳胜率%"):>9}'
                  f'{_nw(r,"抄底次数","抄底胜率%"):>9}')
        if len(sell_signals) > 20:
            print(f'... 共 {len(sell_signals)} 只，详见 Excel')

    print(f'\n===== 优质股：收益≥{args.min_ret:g}% 且 胜率≥{args.min_win:g}%（{len(elite)} 只）=====')
    if elite:
        print(f'{"代码":<11}{"名称":<9}{"信号":<13}{"收盘":>7}{"收益%":>8}{"夏普":>7}{"胜率%":>7}'
              f'{"阳线":>9}{"开阳":>9}{"抄底":>9}')
        print('-' * 97)
        for r in elite[:30]:
            sh = f'{r["回测夏普"]:+.2f}' if r.get('回测夏普') is not None else '-'
            print(f'{r["代码"]:<11}{r["名称"]:<9}{r["信号"]:<13}{r["收盘价"]:>7}'
                  f'{r["回测收益%"]:>+8.1f}{sh:>7}{r["回测胜率%"]:>7.1f}'
                  f'{("✅"+_nw(r,"阳线次数","阳线胜率%")) if r["阳线上穿✅"] else _nw(r,"阳线次数","阳线胜率%"):>9}'
                  f'{("✅"+_nw(r,"开阳次数","开阳胜率%")) if r["开盘上阳✅"] else _nw(r,"开阳次数","开阳胜率%"):>9}'
                  f'{("✅"+_nw(r,"抄底次数","抄底胜率%")) if r["收盘抄底✅"] else _nw(r,"抄底次数","抄底胜率%"):>9}')
        if len(elite) > 30:
            print(f'... 共 {len(elite)} 只，详见 Excel「{elite_sheet[:31]}」sheet')

    _print_radar()
    _print_burst()
    print(f'\n汇总: 买入 {len(buy_signals)} 只 | 卖出 {len(sell_signals)} 只 | 优质股 {len(elite)} 只')
    print(f'Excel 已保存: {out_file}')

    # ── JSON 摘要（供工作台导入）──
    import json
    def _sig_brief(r):
        """精简单条信号为工作台 todo 项格式"""
        return {
            'code': r['代码'], 'name': r['名称'], 'signal': r['信号'],
            'price': r['收盘价'], 'pct_chg': r.get('涨幅%'),
            'lu30': r.get('涨停30'), 'lu60': r.get('涨停60'), 'lu90': r.get('涨停90'),
            'ret': r.get('回测收益%'), 'sharpe': r.get('回测夏普'),
            'win_rate': r.get('回测胜率%'), 'trades': r.get('回测交易数'),
            'vol20': r.get('20日波动率%'), 'vol_pct': r.get('波动率分位'),
            'bias': r.get('乖离%'), 'vol_ratio': r.get('量比'),
            'amount': r.get('成交额(亿)'),
            'industry': r.get('行业', ''),
        }
    summary_json = {
        'scan_date': scan_dt,
        'vol_target': VOL_TARGET,
        'timing': mt.describe(),
        'idx_on': idx_on,
        'radar_lit': radar_lit,
        'n_buy': len(buy_signals), 'n_sell': len(sell_signals), 'n_elite': len(elite),
        'n_bias15': n_bias15,
        'buy_signals': [_sig_brief(r) for r in buy_signals],
        'elite': [_sig_brief(r) for r in elite],
    }
    json_path = out_file.replace('.xlsx', '_summary.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(summary_json, f, ensure_ascii=False, indent=2)
    print(f'JSON 摘要已保存: {json_path}（可导入工作台）')

    # ── 同花顺导入用纯代码 txt（每行一个 6 位代码，Ctrl+F11 粘贴或板块设置导入）──
    date_tag = str(scan_dt).replace('-', '')
    def _write_codes(rows, label):
        codes = sorted({str(r['代码']).split('.')[0] for r in rows})  # 去后缀 + 去重
        p = os.path.join(OUTPUT_DIR, f'ths_{label}_{date_tag}.txt')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('\n'.join(codes))
        print(f'同花顺{label}代码已保存: {p}（{len(codes)} 只）')
        return p
    if buy_signals:
        _write_codes(buy_signals, '买入信号')
    if elite:
        _write_codes(elite, '优质股')


if __name__ == '__main__':
    main()
