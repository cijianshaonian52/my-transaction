import backtrader as bt
import datetime
import math
import os
import sys
import argparse
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# 命令行参数：股票代码（位置参数，可省略）+ --timing 开启大盘择时（默认关闭）。
# 必须在 import market_timing 之前解析并设置环境变量（其首次调用时懒加载配置）
_parser = argparse.ArgumentParser(description='三信号策略（回踩/上穿/影线）单票回测')
_parser.add_argument('code', nargs='?', default='000017.SZ',
                     help='股票代码，如 600487.SH（默认 000017.SZ）')
_parser.add_argument('--timing', action='store_true',
                     help='开启大盘择时（HS300 MA20 exit + 涨停顺延；默认关闭）')
_parser.add_argument('--no-timing', action='store_true', help=argparse.SUPPRESS)  # 旧参数兼容（现为默认行为）
_args = _parser.parse_args()   # 严格解析：拼错/多写参数立即报 usage，避免被误当成股票代码
# 显式参数优先于环境变量；都不给则库默认 off
if _args.no_timing:
    os.environ['MARKET_TIMING'] = 'off'
if _args.timing:
    os.environ['MARKET_TIMING'] = '000300.SH:20:exit'

# 大盘择时（指数 MA 过滤，默认关闭；--timing 开 HS300 MA20 exit，环境变量 MARKET_TIMING 可覆盖）
import market_timing as mt

def _parse_stock_code(raw):
    """支持 603002 / 603002.SH / SH#603002 三种写法，返回 (显示代码, 6位数字, 交易所)"""
    s = raw.strip().upper()
    if '#' in s:
        exch, num = s.split('#')
    elif '.' in s:
        num, exch = s.split('.')
    else:
        num, exch = s, ''
    if not exch:
        # 6/9 开头沪市（主板/科创板/B股），0/2/3 开头深市
        exch = 'SH' if num[:1] in ('6', '9') else 'SZ'
    return f'{num}.{exch}', num, exch


# 股票代码（命令行参数或默认值）
STOCK_CODE, CODE_NUM, CODE_EXCH = _parse_stock_code(_args.code)
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR = os.path.dirname(_SRC_DIR)   # 项目根（src 上一级）：stockdb_csv / cache_data / output 都在根下


def _find_data_file():
    """按 stockdb_csv(qfq前复权，首选) → 根目录裸代码 → cache_data(tushare不复权) 顺序查找"""
    candidates = [
        os.path.join(_BASE_DIR, 'stockdb_csv', f'{CODE_EXCH}#{CODE_NUM}.csv'),
        os.path.join(_BASE_DIR, f'{CODE_NUM}.csv'),
        os.path.join(_BASE_DIR, f'{STOCK_CODE}.csv'),
        os.path.join(_BASE_DIR, 'cache_data', f'{STOCK_CODE}.csv'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


DATA_PATH = _find_data_file()
# 仅当作为主脚本运行时校验（被 batch_test/risk_metrics exec 加载时 globals 无 __name__，跳过）
if globals().get('__name__') == '__main__' and DATA_PATH is None:
    sys.exit(
        f'[错误] 找不到股票数据文件：{STOCK_CODE}.csv\n'
        f'       已查找：stockdb_csv\\{CODE_EXCH}#{CODE_NUM}.csv、项目根目录、cache_data\\。\n'
        f'       请先用 export_stockdb_csv.py 导出行情数据（或 python export_stockdb_csv.py --update 增量更新）。\n'
        f'       大盘择时默认关闭，需要时加 --timing（整体连写、中间无空格），例如：\n'
        f'         .venv\\Scripts\\python.exe src\\main.py 000017.SZ --timing'
    )
IS_STOCKDB_CSV = (DATA_PATH is not None and '#' in os.path.basename(DATA_PATH)
                  and os.path.basename(DATA_PATH).split('#')[0] in ('SH', 'SZ', 'BJ'))
_OUTPUT_DIR = os.path.join(_BASE_DIR, 'output')
os.makedirs(_OUTPUT_DIR, exist_ok=True)
OUTPUT_PNG = os.path.join(_OUTPUT_DIR, f'backtest_result_{STOCK_CODE}.png')
OUTPUT_HTML = os.path.join(_OUTPUT_DIR, f'backtest_result_{STOCK_CODE}.html')

# 回测区间（END_DATE_EXCL 为排他上界，实际展示/交易含 2026-09-09）
START_DATE = datetime.datetime(2024, 9, 9)
END_DATE_EXCL = datetime.datetime(2026, 9, 10)
RANGE_TEXT = '2024-09-09 ~ 2026-09-09'

# 资金与费用（A股口径）
INIT_CASH = 1000000.0  # 初始资金（100万）
LOT = 100              # 买入数量必须是 100 股（1手）的整数倍
COMM_RATE = 0.0005     # 手续费（佣金）：万五，双边收取
STAMP_RATE = 0.0005    # 印花税：万五，仅卖出时收取（买入不收）
MIN_COMM = 5.0         # 手续费单笔最低 5 元（成交金额不足 1 万时按 5 元收）
LIMIT_UP_PCT = 0.10    # 深主板涨停幅度 10%
LOW_OPEN_PCT = 0.01    # 影线持仓：开盘相对昨收低开幅度达到该阈值（1%）才开盘卖出
STOP_LOSS_PCT = 0.05   # 影线持仓：相对买入价跌幅达到5%强制平仓兜底


def limit_up_price(prev_close):
    """涨停价：昨收 ×1.1，四舍五入到分（交易所口径）。"""
    return math.floor(prev_close * (1 + LIMIT_UP_PCT) * 100 + 0.5) / 100


def calc_fee(side, amount):
    """按成交金额计算费用，返回 (手续费, 印花税)。
    买入：手续费 max(金额×万五, 5)，无印花税；
    卖出：手续费 max(金额×万五, 5) + 印花税 金额×万五。"""
    comm = max(amount * COMM_RATE, MIN_COMM)
    stamp = amount * STAMP_RATE if side == 'sell' else 0.0
    return comm, stamp


class AStockCommission(bt.CommInfoBase):
    """A股费用方案：股票式、按金额比例；佣金万五最低5元（双边），印花税万五仅卖出。"""
    params = (
        ('stocklike', True),
        ('commtype', bt.CommInfoBase.COMM_PERC),
        ('percabs', True),
        ('commission', COMM_RATE),
    )

    def _getcommission(self, size, price, pseudoexec):
        amount = abs(size) * price
        comm, stamp = calc_fee('buy' if size > 0 else 'sell', amount)
        return comm + stamp


class FullLotSizer(bt.Sizer):
    """满仓买入：用全部可用现金（含买入手续费反推），数量向下取整到 100 股。"""
    params = (('lot', LOT),)

    def _getsizing(self, comminfo, cash, data, isbuy):
        if not isbuy:
            # 卖出数量 = 当前持仓（self.close() 平仓时使用）
            return abs(self.broker.getposition(data).size)
        price = data.close[0]  # coc 模式：信号日收盘价即成交价
        lot = self.p.lot
        qty = int(cash / (price * (1 + COMM_RATE)) // lot) * lot
        # 低金额触发最低5元佣金时可能使预估超支，按 1 手步进回退
        while qty > 0:
            comm, _ = calc_fee('buy', qty * price)
            if qty * price + comm <= cash + 1e-6:
                break
            qty -= lot
        return qty


# 三信号策略（影线/回踩/上穿均启用）：
# A 回踩：当日开盘在20日线上方、收盘跌破20日线（盘中跌破）+ 当日未跳空低开
#         + MA10/MA20 涨速为正；且今日K线不被昨日完全覆盖（大阴吞噬不买）
# B 上穿：当日开盘在20日线下方、收盘站上20日线（盘中上穿）+ MA20 斜率为负
#         + MA10 斜率为正
# C 影线（十字星）：下影线 >= 实体6.0倍
#         + MA5 斜率 < MA10 斜率 且 MA5 斜率 < MA20 斜率（MA5斜率最小）
#           或 MA5/MA10/MA20 中任意两个斜率为负（二者满足其一即可）
#         + 当日最低点同时低于昨日开盘价与昨日收盘价（盘中跌破昨日实体后收回）
#         + 今日实体上沿不低于昨日实体下沿（重叠差值>=-0.15%）
# 买入：以信号日(T)收盘价买入（cheat-on-close）
# 卖出-影线：持仓期间任一交易日低开达到阈值（开 <= 昨收×(1-1%)）-> 当日开盘价直接卖出；
#            当日开盘价或收盘价相对买入价跌幅 >= 5% -> 强制平仓（开盘价/收盘价卖出）；
#            1) 盘中当日最高价相对当日开盘价涨幅 >= 5% -> 当日收盘价卖出；
#            2) 当日为阴线（收<开）且下跌实体 >= 前一日实体3倍，
#               且收盘价低于昨日开盘价与昨日收盘价 -> 当日收盘价卖出；
#            其余情况继续持有。
#            卖出当日若涨停（收 >= 昨收*1.1 的涨停价），顺延至次日收盘卖；
#            此后逐日：当日仍涨停且高开（开>昨收）则继续持有，
#            直到某日未涨停或未高开，当日收盘价卖出
#            （顺延期间若低开，仍按上方开盘价直接卖出）
class PullbackStrategy(bt.Strategy):
    params = (('p10', 10), ('p20', 20),)

    def __init__(self):
        self.sma5 = bt.ind.SMA(period=5)
        self.sma10 = bt.ind.SMA(period=self.p.p10)
        self.sma20 = bt.ind.SMA(period=self.p.p20)
        self.entry_signal = ''    # 当前持仓的开仓信号
        self.entry_size = 0       # 开仓数量（next_open 时持仓尚未入账，close() 无效，需显式数量）
        self.entry_price = 0.0    # 买入价（用于止损兜底）
        self.limit_wait = False  # 影线卖出当日涨停，顺延等待中
        self.harami_wait = False  # 上穿后 T+1 包孕，持有等待 T+2 决策
        self.timing_wait = False  # 大盘破位当日涨停，顺延中（B版：之后开盘未封板即走）
        # 本地状态机（不能只靠 self.position：cheat_on_open 下卖单成交通知
        # 在下一 bar 的 next_open 之后才送达，重复下单会导致反手做空）
        # flat=空仓 / in=持仓且无退出单 / out_pending=退出单已提交
        self.state = 'flat'

    def start(self):
        # 记录每笔成交：(方向, 日期, 成交价, 数量, 信号)，供图表标注与明细配对
        self.fills = []

    def notify_order(self, order):
        if order.status == order.Completed:
            fill_date = bt.num2date(order.executed.dt).date()
            side = 'buy' if order.isbuy() else 'sell'
            signal = order.info.get('signal', '')
            self.fills.append((side, fill_date,
                               order.executed.price, order.executed.size, signal))
            if side == 'sell':
                self.entry_signal = ''
                self.limit_wait = False
                self.harami_wait = False
                self.timing_wait = False

    def _bar_dt(self):
        return bt.num2date(self.data.datetime[0]).date()

    def _is_limit_up(self):
        # 当日收盘封涨停：收盘价 >= 昨收对应涨停价（容差 0.1 分）
        limit = limit_up_price(self.data.close[-1])
        return self.data.close[0] >= limit - 0.001

    def _is_high_open(self):
        # 高开：当日开盘价 > 昨日收盘价
        return self.data.open[0] > self.data.close[-1]

    def _exit_at_open(self, signal=''):
        # next_open 中下单：coc=False 强制按当日开盘价成交；
        # 此时 coc 买单尚未入账，close() 无效，必须显式卖出数量
        self.sell(size=self.entry_size, coc=False, signal=signal)
        self.state = 'out_pending'

    def _exit_at_close(self):
        # next 中下单：coc 按当日收盘价成交
        self.close()
        self.state = 'out_pending'

    def next_open(self):
        # 开盘时刻决策（cheat-on-open：此刻下 coc=False 市价单以当日开盘价成交）
        # 注意：此时 coc 买单尚未记入 broker 持仓（同一 broker.next 先买单后本单），
        # 故只依据本地状态机 state 判断，不能用 self.position
        if self.state != 'in':
            return
        if self.timing_wait:
            # 大盘破位涨停顺延（B版）：开盘未封涨停即开盘价卖出；
            # 开盘继续封板则持有（开盘涨停时个股自身的低开规则也不可能触发）
            if not mt.is_limit_up(self.data.open[0], self.data.close[-1], STOCK_CODE):
                self._exit_at_open('大盘破位补离')
            return
        if self.entry_signal == '影线':
            # 持仓期间每个交易日开盘检查：低开达到 1% 阈值
            # （开 <= 昨收×0.99）-> 开盘价直接卖出，
            # 含涨停顺延期间（大幅低开先于收盘规则离场）；微幅低开/平开/高开继续持有，
            # 当日再按盘中触价/大阴线/涨停顺延规则处理
            # 另：相对买入价跌幅达 5% 强制平仓兜底
            if (self.data.open[0] <= self.data.close[-1] * (1 - LOW_OPEN_PCT)
                    or self.data.open[0] <= self.entry_price * (1 - STOP_LOSS_PCT)):
                self._exit_at_open()
            return
        if self.entry_signal != '上穿':
            return
        if self.harami_wait:
            # T+2 开盘：未高于昨日(T+1)开盘价（开 <= T+1开）-> 开盘价卖；
            # 高于昨日开盘价则继续持有，等 T+2 收盘卖
            if self.data.open[0] <= self.data.open[-1]:
                self._exit_at_open()
        else:
            # T+1 开盘：高开（开 > 买入日T收盘价）-> 开盘直接卖出
            if self.data.open[0] > self.data.close[-1]:
                self._exit_at_open()

    def next(self):
        # ===== 退出单已提交：等持仓消失后恢复空仓状态（防止重复下单）=====
        if self.state == 'out_pending':
            if self.position:
                return          # 成交尚未落地，不做任何操作
            self.state = 'flat'  # 已平仓，落到下面扫描新信号

        # ===== 持仓中：按信号执行分层卖出 =====
        if self.state == 'in' and self.position:
            # 大盘择时：破位当日收盘离场；当日收盘涨停则顺延（B版：
            # 之后每日开盘未封板即开盘卖，继续封板继续拿；指数重回线上取消离场）
            if self.timing_wait and mt.is_on(self._bar_dt()):
                self.timing_wait = False
            if not self.timing_wait and mt.should_exit(self._bar_dt()):
                if mt.lu_exempt_enabled() and mt.is_limit_up(
                        self.data.close[0], self.data.close[-1], STOCK_CODE):
                    self.timing_wait = True
                else:
                    self.close(signal='大盘破位')
                    self.state = 'out_pending'
                    return
            if self.entry_signal == '影线':
                if self.limit_wait:
                    # 涨停顺延中：当日仍涨停且高开则继续持有，
                    # 直到未涨停或未高开 -> 当日收盘价卖出
                    if self._is_limit_up() and self._is_high_open():
                        return
                    self._exit_at_close()
                    return
                # 卖出触发：1) 盘中最高价相对当日开盘涨幅 >= 5%
                #           2) 当日阴线（收<开）实体 >= 前一日实体的 3 倍，
                #              且收盘价 < 昨日开盘价 且 收盘价 < 昨日收盘价
                #           3) 收盘价相对买入价跌幅 >= 5% -> 强制平仓兜底
                body_today = self.data.open[0] - self.data.close[0]  # 阴线为正
                body_prev = abs(self.data.close[-1] - self.data.open[-1])
                big_bear = (body_today > 0 and body_today >= 3 * body_prev
                           and self.data.close[0] < self.data.open[-1]
                           and self.data.close[0] < self.data.close[-1])
                stop_loss = self.data.close[0] <= self.entry_price * (1 - STOP_LOSS_PCT)
                trigger = (self.data.high[0] >= self.data.open[0] * 1.05
                           or big_bear or stop_loss)
                if trigger:
                    # 当日涨停则顺延：次日起逐日收盘检查，直到未涨停或未高开
                    if self._is_limit_up():
                        self.limit_wait = True
                    else:
                        self._exit_at_close()
                return

            if self.entry_signal == '上穿' and self.harami_wait:
                # T+2 仍持仓（T+2 开盘高于昨日开盘、未在开盘卖）-> T+2 收盘价卖
                self._exit_at_close()
                return

            if self.entry_signal == '上穿':
                # T+1 收盘：判断今日实体是否被买入日 T(=[-1]) 实体包住（包孕线）
                t_hi = max(self.data.open[-1], self.data.close[-1])
                t_lo = min(self.data.open[-1], self.data.close[-1])
                d_hi = max(self.data.open[0], self.data.close[0])
                d_lo = min(self.data.open[0], self.data.close[0])
                if d_hi <= t_hi and d_lo >= t_lo:
                    self.harami_wait = True   # 包孕：持有到 T+2
                else:
                    self._exit_at_close()     # 不包孕：T+1 收盘价卖
            else:
                # 回踩：T+1 收盘价卖
                self._exit_at_close()
            return

        # ===== 空仓：扫描买入信号 =====
        ma10_up = self.sma10[0] > self.sma10[-1]    # 10日线涨速为正
        ma20_up = self.sma20[0] > self.sma20[-1]    # 20日线涨速为正
        ma20_down = self.sma20[0] < self.sma20[-1]  # 20日线斜率为负

        # 信号 A：当日开盘在线上、盘中跌破线（收在线下）+ 未跳空低开 + 双均线涨速为正
        # EPS 容差：收盘价恰好等于均线（浮点噪声 ~1e-15）不算穿越
        eps = 1e-9 * self.sma20[0]
        # 今日K线完全包住昨日K线（今高>=昨高 且 今低<=昨低）则不买（大阴吞噬）
        engulfs = (self.data.high[0] >= self.data.high[-1]
                   and self.data.low[0] <= self.data.low[-1])
        cond_a = ((self.data.open[0] >= self.sma20[0] - eps
                   and self.data.close[0] < self.sma20[0] - eps)
                  and self.data.open[0] >= self.data.close[-1]
                  and ma10_up and ma20_up
                  and not engulfs)
        # 信号 B：当日开盘在线下、盘中上穿（收在线上）+ 20日线斜率为负 + 10日线涨速为正
        cond_b = ((self.data.open[0] <= self.sma20[0] + eps
                   and self.data.close[0] > self.sma20[0] + eps)
                  and ma20_down and ma10_up)
        # 信号 C：下影线 >= 实体1.5倍 + 20日线斜率为负
        #         + MA5 斜率 < MA10 斜率 且 MA5 斜率 < MA20 斜率
        #         + MA5/MA10/MA20 中任意两个斜率为负
        #         + 当日最低点同时低于昨日开盘价与昨日收盘价
        #         + 今日实体上沿不低于昨日实体下沿（重叠差值>=0%）
        body = abs(self.data.close[0] - self.data.open[0])
        lower_shadow = min(self.data.open[0], self.data.close[0]) - self.data.low[0]
        dips_below_prev = (self.data.low[0] < self.data.open[-1]
                           and self.data.low[0] < self.data.close[-1])
        # 斜率用 eps 容差：均线值与前一日数学相等（斜率为0）时不计为负，
        # 避免 SMA 累加浮点噪声（~1e-15）把"走平"误判成下行
        # MA5/MA10/MA20 中任意两个斜率为负
        ma5_down = self.sma5[0] < self.sma5[-1] - eps
        ma10_down = self.sma10[0] < self.sma10[-1] - eps
        ma20_down_c = self.sma20[0] < self.sma20[-1] - eps
        slopes_down = sum([ma5_down, ma10_down, ma20_down_c]) >= 2
        # MA5 斜率 < MA10 斜率 且 MA5 斜率 < MA20 斜率
        ma5_slope = self.sma5[0] - self.sma5[-1]
        ma10_slope = self.sma10[0] - self.sma10[-1]
        ma20_slope = self.sma20[0] - self.sma20[-1]
        ma5_slope_smallest = ma5_slope < ma10_slope and ma5_slope < ma20_slope
        # 今日实体上沿 >= 昨日实体下沿×(1-0.15%)（允许微幅跳空0.15%）
        _prev_lo = min(self.data.open[-1], self.data.close[-1])
        overlap_ok = max(self.data.open[0], self.data.close[0]) >= _prev_lo * (1 - 0.0015)
        cond_c = (lower_shadow >= 6.0 * body
                  and (ma5_slope_smallest or slopes_down) and dips_below_prev
                  and overlap_ok)

        if cond_a and mt.can_open(self._bar_dt()):
            qty_a = int(self.getsizing(isbuy=True)
                        * mt.position_factor(self._bar_dt()) / 100) * 100
            if qty_a > 0:
                self.entry_signal = '回踩'
                self.entry_size = qty_a
                self.harami_wait = False
                self.state = 'in'
                # coc：当日收盘价成交
                self.buy(size=self.entry_size, signal='回踩')
        elif cond_b and mt.can_open(self._bar_dt()):
            qty_b = int(self.getsizing(isbuy=True)
                        * mt.position_factor(self._bar_dt()) / 100) * 100
            if qty_b > 0:
                self.entry_signal = '上穿'
                self.entry_size = qty_b
                self.harami_wait = False
                self.state = 'in'
                self.buy(size=self.entry_size, signal='上穿')
        elif cond_c and mt.can_open(self._bar_dt()):
            qty_c = int(self.getsizing(isbuy=True)
                        * mt.position_factor(self._bar_dt()) / 100) * 100
            if qty_c > 0:
                self.entry_signal = '影线'
                self.entry_size = qty_c
                self.limit_wait = False
                self.harami_wait = False
                self.entry_price = self.data.close[0]
                self.state = 'in'
                self.buy(size=self.entry_size, signal='影线')

# 创建 cerebro：cheat_on_open 启用 next_open（开盘价决策/成交）；
# stdstats=False 关掉默认统计面板（绘图改用 Plotly）
cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)

# 读 csv 并预处理：stockdb_csv 为 GBK 混合分隔（首行股票名、次行表头），tushare 为标准 csv
if IS_STOCKDB_CSV:
    raw = open(DATA_PATH, 'r', encoding='gbk').read()
    lines = raw.strip().split('\n')
    rows = []
    for line in lines[2:]:
        parts = [p for p in line.replace('\t', ',').replace(' ', ',').split(',') if p]
        if len(parts) >= 6:
            try:
                rows.append((pd.to_datetime(parts[0]), float(parts[1]), float(parts[2]),
                             float(parts[3]), float(parts[4]), float(parts[5])))
            except ValueError:
                continue
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    df = df.set_index('date').sort_index()
else:
    df = pd.read_csv(DATA_PATH)
    # 日期列：优先 trade_date，其次 date；tushare 的 trade_date 是整数 YYYYMMDD
    date_col = 'trade_date' if 'trade_date' in df.columns else 'date'
    df[date_col] = pd.to_datetime(df[date_col].astype(str))
    df = df.sort_values(date_col).set_index(date_col)
    # 成交量列：统一为 volume
    if 'vol' in df.columns:
        df = df.rename(columns={'vol': 'volume'})

data = bt.feeds.PandasData(
    dataname=df,
    fromdate=START_DATE,
    todate=END_DATE_EXCL
)
cerebro.adddata(data)

# 添加策略
cerebro.addstrategy(PullbackStrategy)

# 设置初始资金
cerebro.broker.setcash(INIT_CASH)

# 成交模式：coc 让 next 中的市价单以当日收盘价成交（信号日收盘买/收盘卖）；
# coo 让 next_open 中的市价单以当日开盘价成交（高开/低开时开盘卖出）
cerebro.broker.set_coc(True)
cerebro.broker.set_coo(True)
# 关闭提交前保证金预检：coc 买单与次日 coo 卖单在同一批次预检会因顺序误判 MARGIN；
# 满仓整手 sizer 已把买入费用计入反推，队列 FIFO 先买后卖净额结算不会透支
cerebro.broker.set_checksubmit(False)

# 仓位管理：满仓买入，数量为 100 股整数倍
cerebro.addsizer(FullLotSizer)

# 添加分析器
cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade')
cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')

# A股费用：买入佣金万五（最低5元）；卖出佣金万五（最低5元）+ 印花税万五
cerebro.broker.addcommissioninfo(AStockCommission())

# 运行
print(f'股票: {STOCK_CODE}  大盘择时: {mt.describe()}'
      + ('（--timing 已开启）' if _args.timing
         else '（环境变量 MARKET_TIMING 已开启）' if mt.is_enabled()
         else '（--no-timing 已关闭）' if _args.no_timing
         else '（默认关闭，--timing 可开启）'))
print(f'初始资金: {cerebro.broker.getvalue():.2f}')
results = cerebro.run()
print(f'最终资金: {cerebro.broker.getvalue():.2f}')

# 获取分析结果
strat = results[0]
print(f'夏普比率: {strat.analyzers.sharpe.get_analysis()}')

# 交易详情
ta = strat.analyzers.trade.get_analysis()
print(f'\n=== 交易统计 ===')
n_closed = ta.total.closed if 'total' in ta and 'closed' in ta.total else 0
n_won = ta.won.total if 'won' in ta else 0
n_lost = ta.lost.total if 'lost' in ta else 0
print(f'总交易次数: {n_closed}')
print(f'盈利次数: {n_won}')
print(f'亏损次数: {n_lost}')
if n_won > 0:
    print(f'平均盈利: {ta.won.pnl.average:.2f}')
if n_lost > 0:
    print(f'平均亏损: {ta.lost.pnl.average:.2f}')

# 收益率：直接用期末/期初资金（TimeReturn 分析器在 cheat-on-close
# 时序下按估值时点复利，与实际资金口径不一致，不采用）
final_cash = cerebro.broker.getvalue()
total_ret = final_cash / INIT_CASH - 1
years = (END_DATE_EXCL - START_DATE).days / 365.25
annual_ret = (final_cash / INIT_CASH) ** (1 / years) - 1 if years > 0 else 0.0
print(f'\n=== 收益率 ===')
print(f'总收益率: {total_ret*100:.2f}%')
print(f'年化收益率: {annual_ret*100:.2f}%')

# ===== Plotly 绘图 =====
# 1) 均线在全量数据上计算（保证区间开头的 20 日线也有值），再截取回测区间
start_dt, end_dt = START_DATE, END_DATE_EXCL
df['ma5'] = df['close'].rolling(5).mean()
df['ma10'] = df['close'].rolling(10).mean()
df['ma20'] = df['close'].rolling(20).mean()
# 涨跌幅：相对前一交易日收盘价（在全量数据上算，保证区间首根K线也有值）
df['pct_chg'] = (df['close'] / df['close'].shift(1) - 1) * 100
seg = df[(df.index >= start_dt) & (df.index < end_dt)].copy()
# 类目轴标签：用字符串日期，使交易日在轴上连续排列（周末/节假日不占位、不留空）
x_labels = seg.index.strftime('%Y-%m-%d').tolist()

# 2) 买卖点 + 逐笔配对
buy_x, buy_y, sell_x, sell_y = [], [], [], []
date_to_row = {d: i for i, d in enumerate(seg.index)}
open_buy = None
# (信号, 买日期, 买价, 卖日期, 卖价, 数量, 手续费, 印花税, 费用合计, 盈亏, 盈亏率)
trades = []
for side, fill_date, price, size, signal in strat.fills:
    pos = date_to_row.get(pd.Timestamp(fill_date))
    if pos is not None:
        if side == 'buy':
            buy_x.append(x_labels[pos])
            buy_y.append(seg['low'].iloc[pos] * 0.98)
        else:
            sell_x.append(x_labels[pos])
            sell_y.append(seg['high'].iloc[pos] * 1.02)

    if side == 'buy':
        open_buy = (fill_date, price, abs(size), signal)
    elif open_buy is not None:
        bdate, bprice, qty, sig = open_buy
        # 买入：佣金万五最低5元、无印花税；卖出：佣金万五最低5元 + 印花税万五
        comm_b, _stamp_b = calc_fee('buy', bprice * qty)
        comm_s, stamp_s = calc_fee('sell', price * qty)
        comm = comm_b + comm_s
        stamp = stamp_s
        fee_total = comm + stamp
        pnl = (price - bprice) * qty - fee_total
        # 盈亏率分母=实际投入（买入金额+买入佣金）
        pnl_rate = pnl / (bprice * qty + comm_b) * 100
        trades.append((sig, bdate, bprice, fill_date, price, qty,
                       comm, stamp, fee_total, pnl, pnl_rate))
        open_buy = None

# 3) 成交量颜色跟随涨跌（红涨绿跌）
vol_colors = np.where(seg['close'] >= seg['open'], '#ef5350', '#00c853')

# 4) 主图 + 成交量两个子图（go.Table 是 domain 型组件，不能放进 make_subplots，
#    需单独指定 domain，否则会默认铺满整个画布叠在 K 线上）
fig = make_subplots(
    rows=2, cols=1, shared_xaxes=True,
    vertical_spacing=0.04,
)
# 手动分配纵向空间：主图 0.49~0.96 / 成交量 0.335~0.455 / 说明 0.316 / 表格 0~0.298
fig.update_layout(
    yaxis=dict(domain=[0.49, 0.96]),
    yaxis2=dict(domain=[0.335, 0.455]),
)

# 蜡烛图（hover 显示中文 OHLC 与相对昨收的涨跌幅）
fig.add_trace(go.Candlestick(
    x=x_labels,
    open=seg['open'], high=seg['high'], low=seg['low'], close=seg['close'],
    increasing_line_color='#ef5350', increasing_fillcolor='#ef5350',
    decreasing_line_color='#00c853', decreasing_fillcolor='#00c853',
    name='K线',
    customdata=seg[['pct_chg']].to_numpy(),
    hovertemplate=(
        '开盘价：%{open:.2f}<br>'
        '收盘价：%{close:.2f}<br>'
        '最高价：%{high:.2f}<br>'
        '最低价：%{low:.2f}<br>'
        '涨跌幅：%{customdata[0]:+.2f}%'
        '<extra></extra>'
    ),
), row=1, col=1)

# 三条均线
fig.add_trace(go.Scatter(x=x_labels, y=seg['ma5'], mode='lines',
                         line=dict(color='#ffffff', width=1.4), name='MA5'), row=1, col=1)
fig.add_trace(go.Scatter(x=x_labels, y=seg['ma10'], mode='lines',
                         line=dict(color='#ffeb3b', width=1.4), name='MA10'), row=1, col=1)
fig.add_trace(go.Scatter(x=x_labels, y=seg['ma20'], mode='lines',
                         line=dict(color='#ce93d8', width=1.4), name='MA20'), row=1, col=1)

# 买卖箭头：用 annotation 画真正带箭杆的箭头（买入红↑ / 卖出绿↓），
# 不用 marker 三角、也不用 text 散点（text 模式图例里会显示成 Aa）
for d, p in zip(buy_x, buy_y):
    fig.add_annotation(x=d, y=p, row=1, col=1, text='', showarrow=True,
                       arrowhead=2, arrowsize=1.3, arrowwidth=2,
                       arrowcolor='#ef5350', ax=0, ay=16)
for d, p in zip(sell_x, sell_y):
    fig.add_annotation(x=d, y=p, row=1, col=1, text='', showarrow=True,
                       arrowhead=2, arrowsize=1.3, arrowwidth=2,
                       arrowcolor='#00c853', ax=0, ay=-16)
# 图例补充（annotation 不进图例，用 paper 坐标手写两个图例项；放在K线区内避免遮挡标题）
fig.add_annotation(xref='paper', yref='paper', x=0.875, y=0.952,
                   text='↑ 买入', showarrow=False, xanchor='right', yanchor='top',
                   bgcolor='rgba(0,0,0,0.45)',
                   font=dict(size=13, color='#ef5350'))
fig.add_annotation(xref='paper', yref='paper', x=0.985, y=0.952,
                   text='↓ 卖出', showarrow=False, xanchor='right', yanchor='top',
                   bgcolor='rgba(0,0,0,0.45)',
                   font=dict(size=13, color='#00c853'))

# 成交量
fig.add_trace(go.Bar(
    x=x_labels, y=seg['volume'],
    marker_color=vol_colors, name='成交量', showlegend=False,
), row=2, col=1)

# 交易明细表（费用拆分 + 盈亏；附合计行与初始/最终资金行）
total_qty = sum(t[5] for t in trades)
total_comm = sum(t[6] for t in trades)
total_stamp = sum(t[7] for t in trades)
total_fee = sum(t[8] for t in trades)
total_pnl = sum(t[9] for t in trades)
final_value = cerebro.broker.getvalue()
total_rate = (final_value / INIT_CASH - 1) * 100
pnl_color = '#ef5350' if total_pnl >= 0 else '#00c853'

# 每笔交易持仓期间的最大盈利率 / 回撤率（均相对买入价，毛口径不计费用）：
#   最大盈利率 = (持仓期最高价 - 买入价) / 买入价
#   回撤率     = (持仓期最低价 - 买入价) / 买入价（≤0，即最大浮亏）
# 买入按当日收盘价成交，买入当日盘中高低价持仓未经历，故从次根K线起算，含卖出当日
_df_index = df.index
trade_exc = []
for t in trades:
    i0 = _df_index.get_loc(pd.Timestamp(t[1]))
    i1 = _df_index.get_loc(pd.Timestamp(t[3]))
    win = df.iloc[i0 + 1:i1 + 1]
    trade_exc.append(((win['high'].max() / t[2] - 1) * 100,
                      (win['low'].min() / t[2] - 1) * 100))
avg_max_pnl = (sum(e[0] for e in trade_exc) / len(trade_exc)) if trade_exc else 0.0
avg_max_dd = (sum(e[1] for e in trade_exc) / len(trade_exc)) if trade_exc else 0.0

# 逐笔追踪资金（买入后的余额/仓位占用/总资金）
_running_cash = INIT_CASH
_trade_assets = []  # (余额, 仓位占用, 总资金) after each buy
for t in trades:
    bprice, qty = t[2], t[5]
    sprice = t[4]
    comm_b, _ = calc_fee('buy', bprice * qty)
    comm_s, stamp_s = calc_fee('sell', sprice * qty)
    cash_after_buy = _running_cash - (bprice * qty + comm_b)
    pos_value = bprice * qty
    total_assets = cash_after_buy + pos_value
    _trade_assets.append((cash_after_buy, pos_value, total_assets))
    _running_cash = cash_after_buy + (sprice * qty - comm_s - stamp_s)
# 如果末笔未平仓，_running_cash 未扣该笔买入成本，需扣买入成本+佣金再加持仓市值
_last_pos_value = 0
if open_buy is not None:
    _bdate, _bprice, _bqty, _ = open_buy
    _comm_b, _ = calc_fee('buy', _bprice * _bqty)
    _running_cash -= (_bprice * _bqty + _comm_b)
    _last_pos_value = _bqty * seg['close'].iloc[-1]
assert abs(_running_cash + _last_pos_value - final_value) < 0.01, \
    f'现金流校验失败: {_running_cash + _last_pos_value} vs {final_value}'

def fdate(d):
    return pd.Timestamp(d).strftime('%Y-%m-%d')

table_cols = [
    [str(i + 1) for i in range(len(trades))],                      # 0 序号
    [t[0] for t in trades],                                         # 1 信号
    [fdate(t[1]) for t in trades],                                  # 2 买入日期
    [f'{t[2]:.2f}' for t in trades],                                # 3 买入价格
    [fdate(t[3]) for t in trades],                                  # 4 卖出日期
    [f'{t[4]:.2f}' for t in trades],                                # 5 卖出价格
    [f'{t[5]:.0f}' for t in trades],                                # 6 数量(股)
    [f'{t[6]:,.2f}' for t in trades],                               # 7 手续费(元)
    [f'{t[7]:.2f}' for t in trades],                                # 8 印花税(元)
    [f'{t[8]:,.2f}' for t in trades],                               # 9 费用合计
    [f'{t[9]:+,.2f}' for t in trades],                              # 10 盈亏(元)
    [f'{t[10]:+.2f}%' for t in trades],                             # 11 盈亏率
    [f'{e[0]:+.2f}%' for e in trade_exc],                           # 12 最大盈利率
    [f'{e[1]:+.2f}%' for e in trade_exc],                           # 13 回撤率
    [f'{a[0]:,.2f}' for a in _trade_assets],                        # 14 余额(元)
    [f'{a[1]:,.2f}' for a in _trade_assets],                        # 15 仓位占用(元)
    [f'{a[2]:,.2f}' for a in _trade_assets],                        # 16 总资金(元)
]
# 合计行（最大盈利率/回撤率取全部交易均值）
summary_row = ['', '', '', '', '', '合计',
               f'{total_qty:,.0f}', f'{total_comm:,.2f}',
               f'{total_stamp:,.2f}', f'{total_fee:,.2f}',
               f'{total_pnl:+,.2f}', f'{total_rate:+.2f}%',
               f'{avg_max_pnl:+.2f}%', f'{avg_max_dd:+.2f}%',
               f'{final_value:,.2f}', '0.00', f'{final_value:,.2f}']
# 初始/最终资金行
capital_row = ['资金概况', '', '初始资金(元)', f'{INIT_CASH:,.2f}',
               '最终资金(元)', f'{final_value:,.2f}', '总收益率',
               f'{total_rate:+.2f}%', '', '', '', '',
               '', '', '', '', '']
for j, val in enumerate(summary_row):
    table_cols[j].append(val)
for j, val in enumerate(capital_row):
    table_cols[j].append(val)

# 每个单元格的字色：数据行盈亏/盈亏率盈红亏绿，其余白；
# 合计行费用/数量黄色、盈亏盈红亏绿；资金行统一黄色
cell_colors = []
for j in range(17):
    colors = []
    for k, t in enumerate(trades):
        if j in (10, 11):
            colors.append('#ef5350' if t[9] >= 0 else '#00c853')
        elif j == 12:
            colors.append('#ef5350' if trade_exc[k][0] >= 0 else '#00c853')
        elif j == 13:
            colors.append('#ef5350' if trade_exc[k][1] >= 0 else '#00c853')
        else:
            colors.append('white')
    if j in (10, 11):
        colors.append(pnl_color)                              # 合计行
    elif j == 12:
        colors.append('#ef5350' if avg_max_pnl >= 0 else '#00c853')
    elif j == 13:
        colors.append('#ef5350' if avg_max_dd >= 0 else '#00c853')
    else:
        colors.append('#ffeb3b')
    colors.append('#ffeb3b')                                  # 资金行
    cell_colors.append(colors)

n = len(trades)
# 表格行高随行数自适应：表格域约 0.27*画布高，扣表头后均分给 n+2 个汇总行，
# 限制在 15~21px，避免笔数增多时末行/合计行被裁切
_cell_h = min(21, max(15, int((0.286 * 1900 - 90) / (n + 2))))
row_fill = (['#121616' if i % 2 == 0 else '#0a0e0e' for i in range(n)]
            + ['#1d1d1d', '#16202a'])

fig.add_trace(go.Table(
    domain=dict(x=[0.02, 0.98], y=[0.012, 0.298]),
    header=dict(
        values=['序号', '信号', '买入日期', '买入价格', '卖出日期', '卖出价格',
                '数量(股)', '手续费(元)', '印花税(元)', '费用合计(元)',
                '盈亏(元)', '盈亏率', '最大盈利率', '回撤率',
                '余额(元)', '仓位占用(元)', '总资金(元)'],
        fill_color='#263238', font=dict(color='white', size=13),
        align='center', height=28,
    ),
    cells=dict(
        values=table_cols,
        fill_color=[row_fill] * 17,
        font=dict(color=cell_colors, size=12),
        align='center', height=_cell_h,
    ),
    columnwidth=[0.035, 0.05, 0.09, 0.06, 0.09, 0.06, 0.055,
                 0.07, 0.065, 0.07, 0.08, 0.065, 0.07, 0.065,
                 0.075, 0.075, 0.075],
))

# 费用规则说明（表格上方空隙内）
fig.add_annotation(xref='paper', yref='paper', x=0.02, y=0.316,
                   text='满仓买入（100股整数倍）｜买入：手续费万五，不足5元按5元，无印花税；'
                        '卖出：手续费万五（最低5元）+ 印花税万五'
                        '｜最大盈利率/回撤率为持仓期最高/最低价相对买入价（毛口径），合计行为均值',
                   showarrow=False, xanchor='left',
                   font=dict(size=12, color='#b0bec5'))

# 5) 黑底样式
fig.update_layout(
    title=dict(text=f'{STOCK_CODE} 三信号策略（回踩/上穿/长下影十字星，下影≥实体6.0倍·MA5斜率最小或任二斜率负·最低跌破昨日开收·实体上沿≥昨日实体下沿(容差0.15%)；影线低开≥1%开盘卖·买入价跌幅≥5%止损兜底·盘中涨5%止盈·阴线实体≥前日3倍且收盘跌破昨开收止损，涨停顺延，{RANGE_TEXT}）',
               font=dict(size=18)),
    template='plotly_dark',
    paper_bgcolor='black', plot_bgcolor='black',
    font=dict(family='Microsoft YaHei, SimHei, sans-serif', color='white'),
    legend=dict(orientation='h', yanchor='top', y=0.955,
                xanchor='left', x=0.01, bgcolor='rgba(0,0,0,0.55)'),
    xaxis_rangeslider_visible=False,
    width=1800, height=1900,
    bargap=0,
    margin=dict(l=70, r=40, t=90, b=20),
)

# 类目轴：每个交易日紧挨排列，周末/节假日不占位；刻度取每月首个交易日
months = seg.index.to_series().dt.month
first_idx = [i for i in range(len(months))
             if i == 0 or months.iloc[i] != months.iloc[i - 1]]
# 标签较多，每隔一个月显示一个，避免首尾重叠
first_idx = first_idx[::2]
tickvals = [x_labels[i] for i in first_idx]
ticktext = [seg.index[i].strftime('%Y-%m') for i in first_idx]
fig.update_xaxes(
    type='category', tickvals=tickvals, ticktext=ticktext,
    showgrid=False, linecolor='#555555', tickangle=0,
)
fig.update_yaxes(gridcolor='#333333', linecolor='#555555',
                 title_text='价格', row=1, col=1)
fig.update_yaxes(gridcolor='#333333', linecolor='#555555',
                 title_text='成交量', row=2, col=1)

# 十字悬停
fig.update_layout(hovermode='x unified')

fig.write_image(OUTPUT_PNG, scale=1)
# 同时保存一份可交互 HTML（自适应容器：1800×1900 等比缩放，窄屏不裁切、不挤压文字）
import plotly.io as pio
fig.update_layout(width=None, height=None, autosize=True)
_inner = pio.to_html(fig, include_plotlyjs=True, full_html=False,
                     config={'responsive': True},
                     default_width='100%', default_height='100%')
_html = (
    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<style>html,body{margin:0;padding:0;background:#000;}'
    '.wrap{width:100%;max-width:1800px;margin:0 auto;aspect-ratio:1800/1900;}'
    '.wrap .plotly-graph-div{width:100%!important;height:100%!important;}'
    '</style></head><body><div class="wrap">' + _inner + '</div></body></html>'
)
with open(OUTPUT_HTML, 'w', encoding='utf-8') as _f:
    _f.write(_html)
print(f'\n图表已保存: {os.path.basename(OUTPUT_PNG)}（另存交互版 {os.path.basename(OUTPUT_HTML)}）')
