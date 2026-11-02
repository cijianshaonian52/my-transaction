# -*- coding: utf-8 -*-
"""极端负乖离 + 大盘线上 抄底策略（回测 + K线图，图表样式同 main.py）

买入条件（唯一）：
  1) 收盘上穿 MA5：REF(收盘,1) <= REF(MA5,1) 且 收盘 > MA5（重站上5日线）；
  2) 极端负乖离：(收盘 - MA20) / MA20 < -15%；
  3) 大盘配合：沪深300 当日收盘在 MA20 上方（mt.is_on）；
  4) 除权/复牌跳空过滤：近20日无超过板块跌停板（主板10%/双创20%/北交30%）的隔夜跳空，
     避免前复权残留缺口或复牌暴跌造成的假乖离。
  以信号日收盘价买入（cheat-on-close），空仓才买，满仓整手。
卖出：持仓中（T+1，买入当日不卖）收盘 < MA5 → 当日收盘价卖出。
注：本文件只把"指数线上"作为买入门控，不做破位强制离场（与全市场回测的
    "深乖离<-15%且线上"变体口径一致）。

用法:
    .venv\\Scripts\\python.exe strategy_deep_bias.py 600206.SH
数据: cache_data/600206.SH.csv（tushare）→ 根目录 → stockdb_csv/SH#600206.csv（GBK）
"""
import backtrader as bt
import datetime
import math
import os
import sys
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# 大盘择时（仅用 is_on 判断沪深300 是否在 MA20 上方；库默认关闭，
# 需门控时设环境变量 MARKET_TIMING=000300.SH:20:gate 或 exit）
import market_timing as mt

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(_SRC_DIR)   # 项目根（src 上一级）：stockdb_csv / cache_data / output 在根下


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
        exch = 'SH' if num[:1] in ('6', '9') else 'SZ'
    return f'{num}.{exch}', num, exch


STOCK_CODE, CODE_NUM, CODE_EXCH = _parse_stock_code(
    sys.argv[1] if len(sys.argv) > 1 else '000017.SZ')


def _find_data_file():
    """stockdb_csv(qfq前复权，首选) → 根目录(裸代码/tushare) → cache_data(tushare不复权)"""
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


DATA_PATH = _find_data_file()
if DATA_PATH is None:
    raise FileNotFoundError(f'找不到 {STOCK_CODE} 的数据文件（cache_data/根目录/stockdb_csv）')

_OUTPUT_DIR = os.path.join(BASE_DIR, 'output')
os.makedirs(_OUTPUT_DIR, exist_ok=True)
OUTPUT_PNG = os.path.join(_OUTPUT_DIR, f'backtest_deepbias_{STOCK_CODE}.png')
OUTPUT_HTML = os.path.join(_OUTPUT_DIR, f'backtest_deepbias_{STOCK_CODE}.html')

# 回测区间（与 main.py 一致）
START_DATE = datetime.datetime(2024, 9, 9)
END_DATE_EXCL = datetime.datetime(2026, 9, 10)
RANGE_TEXT = '2024-09-09 ~ 2026-09-09'

# 资金与费用（A股口径，与 main.py 一致）
INIT_CASH = 1000000.0
LOT = 100
COMM_RATE = 0.0005
STAMP_RATE = 0.0005
MIN_COMM = 5.0

# 策略参数
BIAS_TH = -0.15   # 极端负乖离阈值：(收盘-MA20)/MA20 < -15%


def calc_fee(side, amount):
    comm = max(amount * COMM_RATE, MIN_COMM)
    stamp = amount * STAMP_RATE if side == 'sell' else 0.0
    return comm, stamp


class AStockCommission(bt.CommInfoBase):
    """A股费用方案：佣金万五最低5元（双边），印花税万五仅卖出。"""
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
    """满仓买入：数量向下取整到 100 股，最低5元佣金超支时逐手回退。"""
    params = (('lot', LOT),)

    def _getsizing(self, comminfo, cash, data, isbuy):
        if not isbuy:
            return abs(self.broker.getposition(data).size)
        price = data.close[0]
        lot = self.p.lot
        qty = int(cash / (price * (1 + COMM_RATE)) // lot) * lot
        while qty > 0:
            comm, _ = calc_fee('buy', qty * price)
            if qty * price + comm <= cash + 1e-6:
                break
            qty -= lot
        return qty


class DeepBiasStrategy(bt.Strategy):
    """极端负乖离 + 大盘线上：重站MA5 且 乖离MA20<-15% 且 HS300在MA20上方 → 收盘买；
    持仓中收盘跌破 MA5（T+1）→ 收盘卖。"""
    params = (('p5', 5), ('p10', 10), ('p20', 20), ('bias_th', BIAS_TH),)

    def __init__(self):
        self.sma5 = bt.ind.SMA(period=self.p.p5)
        self.sma10 = bt.ind.SMA(period=self.p.p10)
        self.sma20 = bt.ind.SMA(period=self.p.p20)
        self.state = 'flat'          # flat / in / out_pending
        self.bought_today = False    # T+1：买入当日不触发卖出
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

    def _bar_dt(self):
        return bt.num2date(self.data.datetime[0]).date()

    def next(self):
        # 退出单已提交：等持仓消失后恢复空仓
        if self.state == 'out_pending':
            if self.position:
                self.bought_today = False
                return
            self.state = 'flat'

        # 持仓中：收盘 < MA5 卖出（买入当日不卖）
        if self.state == 'in' and self.position:
            if self.bought_today:
                self.bought_today = False
                return
            if self.data.close[0] < self.sma5[0]:
                self.close(signal='收盘破MA5')
                self.state = 'out_pending'
            return

        # 空仓：扫描买入信号
        if self.state == 'flat':
            eps = 1e-9 * self.sma20[0]
            # 1) 收盘上穿 MA5（重站上5日线）
            recross = (self.data.close[-1] <= self.sma5[-1]
                       and self.data.close[0] > self.sma5[0])
            # 2) 极端负乖离
            bias = (self.data.close[0] - self.sma20[0]) / self.sma20[0]
            deep = bias <= self.p.bias_th + eps
            # 3) 沪深300 在 MA20 上方
            market_on = mt.is_on(self._bar_dt())
            # 4) 除权/复牌跳空过滤：近20日不得出现超过板块跌停板的隔夜跳空
            #    （前复权数据仍可能残留复权缺口或真实复牌暴跌，此类"超跌"不参与）
            clean = True
            if recross and deep:
                lim = mt.limit_threshold(STOCK_CODE)
                for k in range(1, 21):
                    if len(self.data) <= k + 1:
                        break
                    pc, px = self.data.close[-k-1], self.data.close[-k]
                    if pc > 0 and px / pc - 1 < -(lim + 0.005):
                        clean = False
                        break
            if recross and deep and market_on and clean:
                qty = self.getsizing(isbuy=True)
                if qty > 0:
                    self.state = 'in'
                    self.bought_today = True
                    self.buy(size=qty, signal='深乖离抄底')


# 创建 cerebro（coc：next 中市价单按当日收盘价成交）
cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)

# 读 csv：stockdb_csv 格式为 GBK、首行股票名、次行中文表头（空格分隔）、数据行逗号分隔；
# 直接跳过前两行按固定列名读取。其余为 tushare/标准格式（英文表头、逗号分隔）
if os.path.basename(DATA_PATH).split('#')[0] in ('SH', 'SZ', 'BJ'):
    df = pd.read_csv(DATA_PATH, encoding='gbk', skiprows=2, header=None,
                     names=['date', 'open', 'high', 'low', 'close',
                            'volume', 'amount', 'turnover'])
else:
    df = pd.read_csv(DATA_PATH)
date_col = 'trade_date' if 'trade_date' in df.columns else 'date'
df[date_col] = pd.to_datetime(df[date_col].astype(str), errors='coerce')
df = df.dropna(subset=[date_col]).sort_values(date_col).set_index(date_col)
if 'vol' in df.columns:
    df = df.rename(columns={'vol': 'volume'})

data = bt.feeds.PandasData(dataname=df, fromdate=START_DATE, todate=END_DATE_EXCL)
cerebro.adddata(data)
cerebro.addstrategy(DeepBiasStrategy)
cerebro.broker.setcash(INIT_CASH)
cerebro.broker.set_coc(True)
cerebro.broker.set_coo(True)
cerebro.broker.set_checksubmit(False)
cerebro.addsizer(FullLotSizer)
cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade')
cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
cerebro.broker.addcommissioninfo(AStockCommission())

print(f'初始资金: {cerebro.broker.getvalue():.2f}')
results = cerebro.run()
print(f'最终资金: {cerebro.broker.getvalue():.2f}')

strat = results[0]
print(f'夏普比率: {strat.analyzers.sharpe.get_analysis()}')

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

final_cash = cerebro.broker.getvalue()
total_ret = final_cash / INIT_CASH - 1
years = (END_DATE_EXCL - START_DATE).days / 365.25
annual_ret = (final_cash / INIT_CASH) ** (1 / years) - 1 if years > 0 else 0.0
print(f'\n=== 收益率 ===')
print(f'总收益率: {total_ret*100:.2f}%')
print(f'年化收益率: {annual_ret*100:.2f}%')

# ===== Plotly 绘图（与 main.py 同一版式）=====
start_dt, end_dt = START_DATE, END_DATE_EXCL
df['ma5'] = df['close'].rolling(5).mean()
df['ma10'] = df['close'].rolling(10).mean()
df['ma20'] = df['close'].rolling(20).mean()
df['pct_chg'] = (df['close'] / df['close'].shift(1) - 1) * 100
seg = df[(df.index >= start_dt) & (df.index < end_dt)].copy()
x_labels = seg.index.strftime('%Y-%m-%d').tolist()

buy_x, buy_y, sell_x, sell_y = [], [], [], []
date_to_row = {d: i for i, d in enumerate(seg.index)}
open_buy = None
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
        comm_b, _stamp_b = calc_fee('buy', bprice * qty)
        comm_s, stamp_s = calc_fee('sell', price * qty)
        comm = comm_b + comm_s
        stamp = stamp_s
        fee_total = comm + stamp
        pnl = (price - bprice) * qty - fee_total
        pnl_rate = pnl / (bprice * qty + comm_b) * 100
        trades.append((sig, bdate, bprice, fill_date, price, qty,
                       comm, stamp, fee_total, pnl, pnl_rate))
        open_buy = None

vol_colors = np.where(seg['close'] >= seg['open'], '#ef5350', '#00c853')

fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04)
fig.update_layout(
    yaxis=dict(domain=[0.49, 0.96]),
    yaxis2=dict(domain=[0.335, 0.455]),
)

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

fig.add_trace(go.Scatter(x=x_labels, y=seg['ma5'], mode='lines',
                         line=dict(color='#ffffff', width=1.4), name='MA5'), row=1, col=1)
fig.add_trace(go.Scatter(x=x_labels, y=seg['ma10'], mode='lines',
                         line=dict(color='#ffeb3b', width=1.4), name='MA10'), row=1, col=1)
fig.add_trace(go.Scatter(x=x_labels, y=seg['ma20'], mode='lines',
                         line=dict(color='#ce93d8', width=1.4), name='MA20'), row=1, col=1)

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
                   bgcolor='rgba(0,0,0,0.45)',
                   font=dict(size=13, color='#ef5350'))
fig.add_annotation(xref='paper', yref='paper', x=0.985, y=0.952,
                   text='↓ 卖出', showarrow=False, xanchor='right', yanchor='top',
                   bgcolor='rgba(0,0,0,0.45)',
                   font=dict(size=13, color='#00c853'))

fig.add_trace(go.Bar(
    x=x_labels, y=seg['volume'],
    marker_color=vol_colors, name='成交量', showlegend=False,
), row=2, col=1)

# 交易明细表
total_qty = sum(t[5] for t in trades)
total_comm = sum(t[6] for t in trades)
total_stamp = sum(t[7] for t in trades)
total_fee = sum(t[8] for t in trades)
total_pnl = sum(t[9] for t in trades)
final_value = cerebro.broker.getvalue()
total_rate = (final_value / INIT_CASH - 1) * 100
pnl_color = '#ef5350' if total_pnl >= 0 else '#00c853'

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
_cell_h = min(21, max(15, int((0.286 * 1900 - 90) / (n + 2)))) if n else 21
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

fig.add_annotation(xref='paper', yref='paper', x=0.02, y=0.316,
                   text='满仓买入（100股整数倍）｜买入：手续费万五，不足5元按5元，无印花税；'
                        '卖出：手续费万五（最低5元）+ 印花税万五'
                        '｜最大盈利率/回撤率为持仓期最高/最低价相对买入价（毛口径），合计行为均值',
                   showarrow=False, xanchor='left',
                   font=dict(size=12, color='#b0bec5'))

fig.update_layout(
    title=dict(text=f'{STOCK_CODE} 深乖离抄底策略（收盘重站MA5 且 (收盘-MA20)/MA20<-15% 且 沪深300在MA20上方 且 近20日无超板跳空 → 收盘买；'
                    f'持仓收盘跌破MA5卖出·T+1，{RANGE_TEXT}）',
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

months = seg.index.to_series().dt.month
first_idx = [i for i in range(len(months))
             if i == 0 or months.iloc[i] != months.iloc[i - 1]]
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
fig.update_layout(hovermode='x unified')

fig.write_image(OUTPUT_PNG, scale=1)
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
