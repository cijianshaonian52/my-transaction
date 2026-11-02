# -*- coding: utf-8 -*-
"""跳空放量策略回测

用法:
    .venv\\Scripts\\python.exe strategy_normal.py 600206.SH
    .venv\\Scripts\\python.exe strategy_normal.py 600206.SH --timing   # 开启大盘择时（默认关闭）

数据: cache_data/600206.SH.csv（tushare 格式）或根目录 600206.csv（stockdb_csv 格式）
"""
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

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(_SRC_DIR)   # 项目根（src 上一级）：stockdb_csv / cache_data / output 在根下

# 命令行参数：股票代码（位置参数，可省略）+ --timing 开启大盘择时（默认关闭）。
# 必须在 import market_timing 之前解析并设置环境变量（其首次调用时懒加载配置）
_parser = argparse.ArgumentParser(description='跳空放量策略单票回测')
_parser.add_argument('code', nargs='?', default='600206.SH',
                     help='股票代码，支持 603002 / 603002.SH / SH#603002（默认 600206.SH）')
_parser.add_argument('--timing', action='store_true',
                     help='开启大盘择时（HS300 MA20 exit + 涨停顺延；默认关闭）')
_parser.add_argument('--no-timing', action='store_true', help=argparse.SUPPRESS)  # 旧参数兼容（显式关闭）
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


# 股票代码（命令行参数或默认值），可省略交易所后缀
RAW_CODE = _args.code
STOCK_CODE, CODE_NUM, CODE_EXCH = _parse_stock_code(RAW_CODE)
STOCK_NAME = ''  # 数据加载段读后赋值；此默认值保证只 exec 本文件头部的场景可用


def _find_data_file():
    """按 stockdb_csv(qfq前复权，首选) → 脚本根目录 → cache_data(tushare不复权) 的顺序查找"""
    candidates = [
        os.path.join(BASE_DIR, 'stockdb_csv', f'{CODE_EXCH}#{CODE_NUM}.csv'),
        os.path.join(BASE_DIR, f'{CODE_NUM}.csv'),
        os.path.join(BASE_DIR, f'{STOCK_CODE}.csv'),
        os.path.join(BASE_DIR, 'cache_data', f'{STOCK_CODE}.csv'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


# 回测区间
START_DATE = datetime.datetime(2024, 9, 9)
END_DATE_EXCL = datetime.datetime(2026, 9, 10)
RANGE_TEXT = '2024-09-09 ~ 2026-09-10'

# 资金与费用（A股口径）
INIT_CASH = 100000.0  # 初始资金（10万）
LOT = 100
COMMISSION = 0.0005   # 佣金万五
MIN_COMM = 5.0         # 单笔最低5元
STAMP = 0.0005         # 印花税万五（仅卖出）
LIMIT_UP_PCT = 0.10    # 涨停10%


def limit_up_price(prev_close):
    return math.floor(prev_close * (1 + LIMIT_UP_PCT) * 100 + 0.5) / 100


def calc_fee(side, amount):
    """side: 'buy' or 'sell'"""
    comm = max(amount * COMMISSION, MIN_COMM)
    if side == 'buy':
        return comm, 0.0
    else:
        return comm, amount * STAMP


class AStockCommission(bt.CommInfoBase):
    params = dict(
        commission=COMMISSION,
        stock=True,
        commtype=bt.CommInfoBase.COMM_FIXED,
        percabs=True,
    )

    def _getcommission(self, size, price, pseudoexec):
        amount = abs(size * price)
        comm = max(amount * self.p.commission, MIN_COMM)
        if size > 0:  # 买入
            return comm
        else:  # 卖出
            return comm + amount * STAMP


class FullLotSizer(bt.Sizer):
    """满仓买入：用全部可用现金（含买入手续费反推），数量向下取整到 100 股。"""
    params = (('lot', LOT),)

    def _getsizing(self, comminfo, cash, data, isbuy):
        if not isbuy:
            return abs(self.broker.getposition(data).size)
        price = data.close[0]
        lot = self.p.lot
        qty = int(cash / (price * (1 + COMMISSION)) // lot) * lot
        while qty > 0:
            comm, _ = calc_fee('buy', qty * price)
            if qty * price + comm <= cash + 1e-6:
                break
            qty -= lot
        return qty


# ============================================================
# MA20<MA5或MA20<MA10 + 阳线上穿MA5以收盘价买入 或 开盘高于MA5且收盘阳线以收盘价买入 或 MA20>MA10或MA10>MA5且收盘突破5日线时以收盘价买入；阳线或阴线收盘低于MA5则卖出
# （信号1「开盘上MA5以开盘价买入」已于2026-09全市场实证后移除：5年收益代价约0.7~0.9pct且集中在趋势年，44%同日被收盘信号补位）
# ============================================================
class MyStrategy(bt.Strategy):
    params = (('p10', 10), ('p20', 20),)

    def __init__(self):
        self.sma5 = bt.ind.SMA(period=5)
        self.sma10 = bt.ind.SMA(period=self.p.p10)
        self.sma20 = bt.ind.SMA(period=self.p.p20)
        self.entry_signal = ''
        self.entry_size = 0
        self.state = 'flat'  # flat / in / out_pending / lu_pending
        self.bought_today = False  # 当天已买入，跳过卖出
        self.entry_dt = None  # 最近买入成交日（买入当日不触发大盘择时离场）
        self.fills = []

    def start(self):
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

    def _bar_dt(self):
        return bt.num2date(self.data.datetime[0]).date()

    def _timed_size(self):
        """满仓整手股数，再按大盘择时仓位系数（half 模式线下 0.5）取整"""
        qty = self.getsizing(isbuy=True)
        f = mt.position_factor(self._bar_dt())
        return int(qty * f / 100) * 100

    def next_open(self):
        self.bought_today = False

        # 退出单已提交：等持仓消失后恢复空仓
        if self.state == 'out_pending':
            if self.position:
                return
            self.state = 'flat'

        # 涨停顺延补离：开盘未封涨停即以开盘价卖出（coc=False 市价单按开盘成交，
        # 与 main.py 影线开盘卖出口径一致）；成交后 next() 恢复空仓，
        # 本根仍可按收盘信号买回；开盘继续封板则持有，交给 next() 判定
        if self.state == 'lu_pending' and self.position:
            if not mt.is_limit_up(self.data.open[0], self.data.close[-1],
                                  STOCK_CODE, STOCK_NAME):
                self.sell(size=self.position.size, coc=False, signal='大盘破位补离')
                self.state = 'out_pending'

    def next(self):
        # 退出单已提交：等持仓消失后恢复空仓
        if self.state == 'out_pending':
            if self.position:
                self.bought_today = False
                return
            self.state = 'flat'

        # 持仓中：收盘价<MA5 时卖出（当天买入不卖）；大盘择时破位当日收盘离场
        if self.state == 'in' and self.position:
            if self.bought_today:
                self.bought_today = False
                return
            if self.data.close[0] < self.sma5[0]:
                self.close()
                self.state = 'out_pending'
            elif mt.should_exit(self._bar_dt()):
                if mt.lu_exempt_enabled() and mt.is_limit_up(
                        self.data.close[0], self.data.close[-1],
                        STOCK_CODE, STOCK_NAME):
                    # 破位当日涨停：顺延，之后开盘未封板即走
                    self.state = 'lu_pending'
                else:
                    self.close(signal='大盘破位')
                    self.state = 'out_pending'
            return

        # 涨停顺延期（开盘仍封板而持有到收盘）：
        # 跌破MA5收盘卖；指数重回MA上方则取消离场；其余继续顺延
        if self.state == 'lu_pending' and self.position:
            if self.data.close[0] < self.sma5[0]:
                self.close()
                self.state = 'out_pending'
            elif mt.is_on(self._bar_dt()):
                self.state = 'in'
            return

        # 空仓：MA20<MA5或MA20<MA10，阳线上穿MA5 或 开盘高于MA5且收盘阳线 时以收盘价买入
        if self.state == 'flat':
            ma20_below = (self.sma20[0] < self.sma5[0]
                          or self.sma20[0] < self.sma10[0])
            bull_cross = (self.data.close[-1] <= self.sma5[-1]
                          and self.data.close[0] > self.sma5[0]
                          and self.data.close[0] > self.data.open[0])
            open_above_ma5_bull = (self.data.open[0] > self.sma5[0]
                                   and self.data.close[0] > self.data.open[0])
            break_ma5 = (self.data.close[-1] <= self.sma5[-1]
                         and self.data.close[0] > self.sma5[0])

            if not mt.can_open(self._bar_dt()):
                return
            if ma20_below and bull_cross:
                self.entry_signal = '阳线上穿MA5'
                self.entry_size = self._timed_size()
                if self.entry_size > 0:
                    self.state = 'in'
                    self.buy(size=self.entry_size, signal='阳线上穿MA5')
            elif ma20_below and open_above_ma5_bull:
                self.entry_signal = '开盘上MA5阳'
                self.entry_size = self._timed_size()
                if self.entry_size > 0:
                    self.state = 'in'
                    self.buy(size=self.entry_size, signal='开盘上MA5阳')
            elif (self.sma20[0] > self.sma10[0] or self.sma10[0] > self.sma5[0]) and break_ma5:
                self.entry_signal = '收盘破MA5'
                self.entry_size = self._timed_size()
                if self.entry_size > 0:
                    self.state = 'in'
                    self.buy(size=self.entry_size, signal='收盘破MA5')


# ============================================================
# 引擎（与 main.py 一致，勿改）
# ============================================================
# 以下文件查找/退出逻辑放在引擎标记之后：scan_signals、batch_test 只 exec
# 标记之前的代码，避免导入时因硬编码代码找不到数据而 sys.exit
DATA_PATH = _find_data_file()
if DATA_PATH is None:
    print(f'错误：未找到 {RAW_CODE} 的数据文件'
          f'（已查找 cache_data/、脚本目录、stockdb_csv/）')
    sys.exit(1)
IS_STOCKDB_CSV = os.path.basename(DATA_PATH).split('#')[0] in ('SH', 'SZ', 'BJ')
_OUTPUT_DIR = os.path.join(BASE_DIR, 'output')
os.makedirs(_OUTPUT_DIR, exist_ok=True)
OUTPUT_PNG = os.path.join(_OUTPUT_DIR, f'backtest_result_{STOCK_CODE}.png')
OUTPUT_HTML = os.path.join(_OUTPUT_DIR, f'backtest_result_{STOCK_CODE}.html')

cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
cerebro.broker.setcash(INIT_CASH)
cerebro.broker.set_coc(True)
cerebro.broker.set_coo(True)
cerebro.broker.set_checksubmit(False)
cerebro.broker.addcommissioninfo(AStockCommission())
cerebro.addstrategy(MyStrategy)
cerebro.addsizer(FullLotSizer)
cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade')

STOCK_NAME = ''
if IS_STOCKDB_CSV:
    # stockdb_csv 格式：GBK 编码，第1行含股票名，第2行表头，tab/空格混合分隔
    raw = open(DATA_PATH, 'r', encoding='gbk').read()
    lines = raw.strip().split('\n')
    head_parts = lines[0].split()
    STOCK_NAME = head_parts[1] if len(head_parts) > 1 else ''
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
    date_col = 'trade_date' if 'trade_date' in df.columns else 'date'
    df[date_col] = pd.to_datetime(df[date_col].astype(str))
    df = df.sort_values(date_col).set_index(date_col)
    if 'vol' in df.columns:
        df = df.rename(columns={'vol': 'volume'})

print(f'数据文件: {os.path.relpath(DATA_PATH, BASE_DIR)}'
      + (f'  ({STOCK_NAME})' if STOCK_NAME else ''))
print(f'大盘择时: {mt.describe()}（指数线下不开新仓'
      + ('，破位当日收盘离场' + ('，涨停顺延至次日开盘卖' if mt.lu_exempt_enabled() else '')
         if mt.config()[2] == 'exit' else '') + '）'
      + ('（--timing 已开启）' if _args.timing
         else '（环境变量 MARKET_TIMING 已开启）' if mt.is_enabled()
         else '（--no-timing 已关闭）' if _args.no_timing
         else '（默认关闭，--timing 可开启）'))

data = bt.feeds.PandasData(dataname=df, fromdate=START_DATE, todate=END_DATE_EXCL)
cerebro.adddata(data)

results = cerebro.run()
strat = results[0]

final_value = cerebro.broker.getvalue()
total_return = (final_value / INIT_CASH - 1) * 100
years = (END_DATE_EXCL - START_DATE).days / 365.25
annual_return = ((final_value / INIT_CASH) ** (1 / years) - 1) * 100

print(f'\n=== 收益率 ===')
print(f'总收益率: {total_return:.2f}%')
print(f'年化收益率: {annual_return:.2f}%')

ta = strat.analyzers.trade.get_analysis()
n_closed = ta.total.closed if 'total' in ta and 'closed' in ta.total else 0
n_won = ta.won.total if 'won' in ta else 0
n_lost = ta.lost.total if 'lost' in ta else 0
print(f'\n=== 交易统计 ===')
print(f'总交易次数: {n_closed}')
print(f'盈利次数: {n_won}')
print(f'亏损次数: {n_lost}')
if n_won > 0:
    print(f'平均盈利: {ta.won.pnl.average:.2f}')
if n_lost > 0:
    print(f'平均亏损: {ta.lost.pnl.average:.2f}')

# ============================================================
# 出图（与 main.py 一致的黑底 Plotly 方案 + 交易明细表）
# ============================================================
# 均线/涨跌幅在全量数据上计算（保证区间开头的 20 日线也有值），再截取回测区间。
# 注意：bt.feeds 的 todate 会被推到当天 23:59:59，实际【含】END_DATE_EXCL 当天，
# 图表窗口须与回测窗口一致（<=），否则期末持仓估值会差最后一根K线
df['ma5'] = df['close'].rolling(5).mean()
df['ma10'] = df['close'].rolling(10).mean()
df['ma20'] = df['close'].rolling(20).mean()
df['pct_chg'] = (df['close'] / df['close'].shift(1) - 1) * 100
seg = df[(df.index >= START_DATE) & (df.index <= END_DATE_EXCL)].copy()
# 类目轴标签：字符串日期，交易日连续排列（周末/节假日不占位）
x_labels = seg.index.strftime('%Y-%m-%d').tolist()

buy_x, buy_y, sell_x, sell_y = [], [], [], []
date_to_row = {d: i for i, d in enumerate(seg.index)}
open_buy = None
# (信号, 买日期, 买价, 卖日期, 卖价, 数量, 手续费, 印花税, 费用合计, 盈亏, 盈亏率)
trades = []
for side, fill_date, price, size, signal in strat.fills:
    pos = date_to_row.get(pd.Timestamp(fill_date))
    if pos is not None:
        if side == 'buy':
            buy_x.append(x_labels[pos]); buy_y.append(seg['low'].iloc[pos] * 0.98)
        else:
            sell_x.append(x_labels[pos]); sell_y.append(seg['high'].iloc[pos] * 1.02)
    if side == 'buy':
        open_buy = (fill_date, price, abs(size), signal)
    elif open_buy is not None:
        bdate, bprice, qty, sig = open_buy
        comm_b, _ = calc_fee('buy', bprice * qty)
        comm_s, stamp_s = calc_fee('sell', price * qty)
        comm = comm_b + comm_s
        stamp = stamp_s
        fee_total = comm + stamp
        pnl = (price - bprice) * qty - fee_total
        pnl_rate = pnl / (bprice * qty + comm_b) * 100
        trades.append((sig, bdate, bprice, fill_date, price, qty,
                       comm, stamp, fee_total, pnl, pnl_rate))
        open_buy = None

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

vol_colors = np.where(seg['close'] >= seg['open'], '#ef5350', '#00c853')

fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04)
fig.update_layout(
    yaxis=dict(domain=[0.49, 0.96]),
    yaxis2=dict(domain=[0.335, 0.455]),
)

# 蜡烛图（hover 显示中文 OHLC 与相对昨收的涨跌幅）
fig.add_trace(go.Candlestick(
    x=x_labels, open=seg['open'], high=seg['high'], low=seg['low'], close=seg['close'],
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

# MA线
for ma_str, color, name in [('ma5', '#ffffff', 'MA5'), ('ma10', '#ffeb3b', 'MA10'), ('ma20', '#ce93d8', 'MA20')]:
    fig.add_trace(go.Scatter(x=x_labels, y=seg[ma_str], mode='lines',
                            line=dict(color=color, width=1.4), name=name), row=1, col=1)

# 买卖箭头
for d, p in zip(buy_x, buy_y):
    fig.add_annotation(x=d, y=p, row=1, col=1, text='', showarrow=True,
                       arrowhead=2, arrowsize=1.3, arrowwidth=2,
                       arrowcolor='#ef5350', ax=0, ay=16)
for d, p in zip(sell_x, sell_y):
    fig.add_annotation(x=d, y=p, row=1, col=1, text='', showarrow=True,
                       arrowhead=2, arrowsize=1.3, arrowwidth=2,
                       arrowcolor='#00c853', ax=0, ay=-16)
fig.add_annotation(xref='paper', yref='paper', x=0.875, y=0.952,
                   text='↑ 买入', showarrow=False, xanchor='right', yanchor='top',
                   bgcolor='rgba(0,0,0,0.45)', font=dict(size=13, color='#ef5350'))
fig.add_annotation(xref='paper', yref='paper', x=0.985, y=0.952,
                   text='↓ 卖出', showarrow=False, xanchor='right', yanchor='top',
                   bgcolor='rgba(0,0,0,0.45)', font=dict(size=13, color='#00c853'))

# 成交量
fig.add_trace(go.Bar(x=x_labels, y=seg['volume'], marker_color=vol_colors,
                     name='成交量', showlegend=False), row=2, col=1)

# 交易明细表
total_qty = sum(t[5] for t in trades)
total_comm = sum(t[6] for t in trades)
total_stamp = sum(t[7] for t in trades)
total_fee = sum(t[8] for t in trades)
total_pnl = sum(t[9] for t in trades)
final_value = cerebro.broker.getvalue()
total_rate = (final_value / INIT_CASH - 1) * 100
pnl_color = '#ef5350' if total_pnl >= 0 else '#00c853'

# 逐笔追踪资金
_running_cash = INIT_CASH
_trade_assets = []
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
    [str(i + 1) for i in range(len(trades))],
    [t[0] for t in trades],
    [fdate(t[1]) for t in trades],
    [f'{t[2]:.2f}' for t in trades],
    [fdate(t[3]) for t in trades],
    [f'{t[4]:.2f}' for t in trades],
    [f'{t[5]:.0f}' for t in trades],
    [f'{t[6]:,.2f}' for t in trades],
    [f'{t[7]:.2f}' for t in trades],
    [f'{t[8]:,.2f}' for t in trades],
    [f'{t[9]:+,.2f}' for t in trades],
    [f'{t[10]:+.2f}%' for t in trades],
    [f'{e[0]:+.2f}%' for e in trade_exc],
    [f'{e[1]:+.2f}%' for e in trade_exc],
    [f'{a[0]:,.2f}' for a in _trade_assets],
    [f'{a[1]:,.2f}' for a in _trade_assets],
    [f'{a[2]:,.2f}' for a in _trade_assets],
]
# 最大盈利率/回撤率合计行取全部交易均值
avg_max_pnl = (sum(e[0] for e in trade_exc) / len(trade_exc)) if trade_exc else 0.0
avg_max_dd = (sum(e[1] for e in trade_exc) / len(trade_exc)) if trade_exc else 0.0
summary_row = ['', '', '', '', '', '合计',
               f'{total_qty:,.0f}', f'{total_comm:,.2f}',
               f'{total_stamp:,.2f}', f'{total_fee:,.2f}',
               f'{total_pnl:+,.2f}', f'{total_rate:+.2f}%',
               f'{avg_max_pnl:+.2f}%', f'{avg_max_dd:+.2f}%',
               f'{final_value:,.2f}', '0.00', f'{final_value:,.2f}']
capital_row = ['资金概况', '', '初始资金(元)', f'{INIT_CASH:,.2f}',
               '最终资金(元)', f'{final_value:,.2f}', '总收益率',
               f'{total_rate:+.2f}%', '', '', '', '',
               '', '', '', '', '']
for j, val in enumerate(summary_row):
    table_cols[j].append(val)
for j, val in enumerate(capital_row):
    table_cols[j].append(val)

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
        colors.append(pnl_color)
    elif j == 12:
        colors.append('#ef5350' if avg_max_pnl >= 0 else '#00c853')
    elif j == 13:
        colors.append('#ef5350' if avg_max_dd >= 0 else '#00c853')
    else:
        colors.append('#ffeb3b')
    colors.append('#ffeb3b')
    cell_colors.append(colors)

n = len(trades)
# 画布高度随交易笔数自适应：表格域占画布 0.286（y=0.012~0.298），
# 扣除表头 28px 后给每行预留 18px，反推所需画布高（下限 1900），
# 避免固定高度时笔数多、行高被压扁盖住文字
_canvas_h = max(1900, int((28 + (n + 2) * 18) / 0.286))
_cell_h = min(21, max(15, int((0.286 * _canvas_h - 28) / (n + 2))))
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

# 费用说明
fig.add_annotation(xref='paper', yref='paper', x=0.02, y=0.316,
                   text='满仓买入（100股整数倍）｜买入：手续费万五，不足5元按5元，无印花税；'
                        '卖出：手续费万五（最低5元）+ 印花税万五'
                        '｜最大盈利率/回撤率为持仓期最高/最低价相对买入价（毛口径），合计行为均值',
                   showarrow=False, xanchor='left',
                   font=dict(size=12, color='#b0bec5'))

# 样式
fig.update_layout(
    title=dict(text=f'{(STOCK_CODE + " " + STOCK_NAME).strip()} 跳空放量策略（{RANGE_TEXT}）'
                    + ('' if not mt.is_enabled() else f'｜择时 {mt.describe()}'),
               font=dict(size=18)),
    template='plotly_dark',
    paper_bgcolor='black', plot_bgcolor='black',
    font=dict(family='Microsoft YaHei, SimHei, sans-serif', color='white'),
    legend=dict(orientation='h', yanchor='top', y=0.955, xanchor='left', x=0.01,
                bgcolor='rgba(0,0,0,0.55)'),
    xaxis_rangeslider_visible=False,
    width=1800, height=_canvas_h,
    bargap=0,
    margin=dict(l=70, r=40, t=90, b=20),
)
months = seg.index.to_series().dt.month
first_idx = [i for i in range(len(months)) if i == 0 or months.iloc[i] != months.iloc[i - 1]]
first_idx = first_idx[::2]
tickvals = [x_labels[i] for i in first_idx]
ticktext = [seg.index[i].strftime('%Y-%m') for i in first_idx]
fig.update_xaxes(type='category', tickvals=tickvals, ticktext=ticktext,
                 showgrid=False, linecolor='#555555', tickangle=0)
fig.update_yaxes(gridcolor='#333333', linecolor='#555555', title_text='价格', row=1, col=1)
fig.update_yaxes(gridcolor='#333333', linecolor='#555555', title_text='成交量', row=2, col=1)

# 十字悬停（跨子图聚合）
fig.update_layout(hovermode='x unified')

fig.write_image(OUTPUT_PNG, scale=1)

# HTML 自适应：固定 1800×_canvas_h 在窄屏/高DPI缩放下会被裁切或挤压，
# 改为按 1800:_canvas_h 等比缩放的容器（整图像 PNG 一样整体缩放，宽屏居中封顶 1800）
import plotly.io as pio
fig.update_layout(width=None, height=None, autosize=True)
_inner = pio.to_html(fig, include_plotlyjs=True, full_html=False,
                     config={'responsive': True},
                     default_width='100%', default_height='100%')
_html = (
    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<style>html,body{margin:0;padding:0;background:#000;}'
    '.wrap{width:100%;max-width:1800px;margin:0 auto;'
    f'aspect-ratio:1800/{_canvas_h};}}'
    '.wrap .plotly-graph-div{width:100%!important;height:100%!important;}'
    '</style></head><body><div class="wrap">' + _inner + '</div></body></html>'
)
with open(OUTPUT_HTML, 'w', encoding='utf-8') as _f:
    _f.write(_html)
print(f'\n图表已保存: {os.path.basename(OUTPUT_PNG)}（另存交互版 {os.path.basename(OUTPUT_HTML)}）')
