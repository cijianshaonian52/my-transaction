# -*- coding: utf-8 -*-
"""大盘择时（沪深300 等指数 MA 过滤）

规则（全市场实证，2025-11~2026-09，3190 只）：
  指数收盘 < MA 时不开新仓；mode='exit' 时持仓在破位当日收盘离场。
  沪深300 MA20 exit：等权组合回撤 -9.1%→-4.9%，收益 +0.4%→+3.5%，胜率 30%→34%。

★默认关闭（2026-09-16 起）：所有回测/扫描脚本不带择时，需显式开启：
  命令行：各脚本加 --timing（= HS300 MA20 exit + 涨停顺延；scan 还支持 --timing gate/half/off）
  环境变量：set MARKET_TIMING=000300.SH:20:exit        （开启，实证推荐档）
            set MARKET_TIMING=000300.SH:20:exit:nolu   （关闭涨停顺延）
            set MARKET_TIMING=000300.SH:20:gate        （只挡新仓，不强制离场）
            set MARKET_TIMING=000300.SH:20:half        （线下半仓）
            set MARKET_TIMING=off                      （显式关闭，同默认）
  代码：import market_timing as mt; mt.configure(mode='exit')

exit 模式默认附带「涨停顺延」(B版，全市场实证 +0.27pct/回撤 -0.13pct)：
  破位当日个股收盘封涨停则不卖出；之后每日开盘，未封涨停即开盘卖出，
  继续封板继续持有；指数先站回 MA 则取消离场。
"""
import os
import pandas as pd

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_SRC_DIR)   # 项目根（src 上一级）：idx_data 在根下
IDX_DIR = os.path.join(PROJECT_ROOT, 'idx_data')

# 默认参数
DEFAULT_INDEX = '000300.SH'   # 沪深300（实证优于上证综指/中证500）
DEFAULT_MA = 20               # MA10 过敏感、MA60 过钝，20 为甜蜜点
DEFAULT_MODE = 'off'          # 2026-09-16 起默认关闭；脚本加 --timing 开 exit
DEFAULT_LU_EXEMPT = True      # exit 模式下破位日涨停顺延

_cache = {}  # (index, ma) -> {Timestamp: 1.0/0.0}
_config = None


def _parse_env():
    raw = os.environ.get('MARKET_TIMING', '').strip()
    if not raw:
        return DEFAULT_INDEX, DEFAULT_MA, DEFAULT_MODE, DEFAULT_LU_EXEMPT
    if raw.lower() in ('off', '0', 'none'):
        return DEFAULT_INDEX, DEFAULT_MA, 'off', DEFAULT_LU_EXEMPT
    parts = raw.split(':')
    idx = parts[0] if parts and parts[0] else DEFAULT_INDEX
    ma = int(parts[1]) if len(parts) > 1 and parts[1] else DEFAULT_MA
    mode = parts[2] if len(parts) > 2 and parts[2] else DEFAULT_MODE
    lu = DEFAULT_LU_EXEMPT
    if len(parts) > 3 and parts[3]:
        lu = parts[3].lower() not in ('nolu', '0', 'off', 'no')
    return idx, ma, mode, lu


def configure(index=None, ma=None, mode=None, limit_exempt=None):
    """显式配置；参数为 None 时保留当前值。mode: off/gate/exit/half"""
    global _config
    if _config is None:
        _config = _parse_env()
    idx0, ma0, mode0, lu0 = _config
    _config = (index or idx0,
               int(ma) if ma else ma0,
               mode or mode0,
               lu0 if limit_exempt is None else bool(limit_exempt))
    return _config


def config():
    if _config is None:
        configure()
    return _config


def is_enabled():
    return config()[2] != 'off'


def lu_exempt_enabled():
    """exit 模式且涨停顺延开关打开"""
    return is_enabled() and config()[2] == 'exit' and config()[3]


def limit_threshold(code=None, name=''):
    """个股涨跌停幅度：ST 5%；创业板/科创板 20%；北交所 30%；其余 10%"""
    if name and 'ST' in str(name).upper():
        return 0.05
    digits = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if digits:
        if digits[:3] in ('300', '301', '688', '689'):
            return 0.20
        if digits[:1] in ('4', '8'):
            return 0.30
    return 0.10


def is_limit_up(px, prev_close, code=None, name='', tol=0.003):
    """px（开盘/收盘价）是否封涨停（相对前收，容差 0.3pct 吸收四舍五入）"""
    if prev_close is None or prev_close <= 0 or px is None:
        return False
    return px / prev_close - 1 >= limit_threshold(code, name) - tol


def regime_map(index=None, ma=None):
    """返回 {交易日 Timestamp: 1.0(线上)/0.0(线下)}，惰性加载并缓存"""
    idx, m, _, _lu = config()
    index = index or idx
    ma = int(ma) if ma else m
    key = (index, ma)
    if key not in _cache:
        path = os.path.join(IDX_DIR, f'{index}.csv')
        d = pd.read_csv(path)
        cdate = 'trade_date' if 'trade_date' in d.columns else 'date'
        d[cdate] = pd.to_datetime(d[cdate].astype(str))
        d = d.sort_values(cdate).set_index(cdate)
        d['close'] = pd.to_numeric(d['close'], errors='coerce')
        above = (d['close'] > d['close'].rolling(ma).mean()).astype(float)
        _cache[key] = above.to_dict()
    return _cache[key]


def is_on(dt, index=None, ma=None):
    """dt（Timestamp 或 'YYYY-MM-DD'）指数是否在 MA 上方；指数无该日期记录时按可交易处理"""
    if not is_enabled():
        return True
    reg = regime_map(index, ma)
    if not isinstance(dt, pd.Timestamp):
        dt = pd.Timestamp(dt)
    return reg.get(dt, 1.0) >= 1


def can_open(dt):
    """当前是否允许开新仓"""
    return is_on(dt)


def should_exit(dt):
    """exit 模式：破位日持仓应离场"""
    return is_enabled() and config()[2] == 'exit' and not is_on(dt)


def position_factor(dt):
    """开仓资金比例：half 模式线下 0.5，其余 1.0/0.0"""
    mode = config()[2]
    if mode == 'off' or is_on(dt):
        return 1.0
    return 0.5 if mode == 'half' else 0.0


def describe():
    idx, ma, mode, lu = config()
    text = f'{idx} MA{ma} {mode if is_enabled() else "off"}'
    if lu_exempt_enabled():
        text += '+涨停顺延'
    return text
