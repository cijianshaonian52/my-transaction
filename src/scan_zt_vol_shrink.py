# 形态扫描: T涨停 -> T+1放量不涨停(无炸板/无跌停) -> T+2缩量
# 仅主板(60/00), 剔除ST。输出信号明细 + 后续N日表现
import os, sys, io
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_DIR = os.path.join(ROOT, 'stockdb_csv')
HOLD = [1, 3, 5]          # 信号日(T+2)收盘后, 统计后续1/3/5日收益
FEE = 0.0015              # 双边费用


def load_name():
    p = os.path.join(ROOT, 'cache_data', '_stock_basic.csv')
    if os.path.exists(p):
        df = pd.read_csv(p, dtype=str)
        for col in ('ts_code', 'code', 'symbol'):
            if col in df.columns:
                c = df[col].str.split('.').str[0]
                ncol = 'name' if 'name' in df.columns else df.columns[1]
                return dict(zip(c, df[ncol]))
    return {}


def streak(closes, i, th=9.9):
    """截止i日的连续涨停数"""
    s = 0
    j = i
    while j >= 1:
        pct = (closes[j] / closes[j - 1] - 1) * 100
        if pct >= th:
            s += 1
            j -= 1
        else:
            break
    return s


def main():
    nm = load_name()
    sigs = []
    for fn in os.listdir(CSV_DIR):
        if not fn.endswith('.csv'):
            continue
        code = fn.split('#')[1].split('.')[0]
        if code[:2] in ('30', '68'):
            continue
        if 'ST' in str(nm.get(code, '')).upper():
            continue
        try:
            df = pd.read_csv(os.path.join(CSV_DIR, fn), encoding='gbk', skiprows=2, header=None,
                             names=['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'turnover'])
        except Exception:
            continue
        if len(df) < 60:
            continue
        df['date'] = df['date'].astype(str)
        for c in ('open', 'high', 'low', 'close', 'volume'):
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['close']).reset_index(drop=True)
        if len(df) < 60:
            continue
        C, V, H, L, O, D = df['close'].values, df['volume'].values, df['high'].values, df['low'].values, df['open'].values, df['date'].values
        n = len(df)
        for i in range(2, n):          # i = T+1 日, T日=i-1, T+2日=i+1(可为最后一根)
            t = i - 1
            j2 = i + 1
            if j2 >= n:                 # T+2 超出数据范围则跳过
                break
            # T日: 涨停(收>=9.9%)
            pct_t = (C[t] / C[t - 1] - 1) * 100
            if pct_t < 9.9:
                continue
            # 记录T日是第几连板(不再限制必须首板)
            n板 = streak(C, t)
            # T+1日(i): 不涨停、无炸板(盘中最高不触涨停)、无跌停、放量
            pct_t1 = (C[i] / C[t] - 1) * 100
            if pct_t1 >= 9.9:                       # 仍涨停
                continue
            if pct_t1 <= -9.5:                      # 跌停
                continue
            limit_up_t1 = round(C[t] * 1.10, 2)     # 主板10%涨停价
            if H[i] >= limit_up_t1 - 1e-6:          # 炸板(盘中触涨停)
                continue
            if V[i] <= V[t]:                        # 未放量
                continue
            # T+2日(i+1): 缩量(量<T+1)、非跌停
            j2 = i + 1
            pct_t2 = (C[j2] / C[i] - 1) * 100
            if pct_t2 <= -9.5:
                continue
            if V[j2] >= V[i]:                       # 未缩量
                continue
            # 后续表现(以T+2收盘为买入参考)
            rec = {'信号日': D[j2], 'code': code, 'name': nm.get(code, ''),
                   'T日': D[t], 'T连板数': n板, 'T日涨幅%': round(pct_t, 2),
                   'T1涨幅%': round(pct_t1, 2), 'T1量比T': round(V[i] / V[t], 2),
                   'T2涨幅%': round(pct_t2, 2), 'T2量比T1': round(V[j2] / V[i], 2),
                   '信号收盘': round(C[j2], 2)}
            for h in HOLD:
                k = j2 + h
                rec['后%d日%%' % h] = round((C[k] / C[j2] - 1) * 100 - FEE * 100, 2) if k < n else None
            sigs.append(rec)

    R = pd.DataFrame(sigs)
    if R.empty:
        print('无信号')
        return
    out = os.path.join(ROOT, 'output', 'zt_vol_shrink_signals.csv')
    R.to_csv(out, index=False, encoding='utf-8-sig')
    print('总信号 %d 条 -> %s' % (len(R), out))
    R['y'] = R['信号日'].str[:4]
    for y, g in R.groupby('y'):
        print('  %s  n=%d  后5日均值%+.2f%%  胜率%.1f%%' % (y, len(g), g['后5日%'].mean(),
              (g['后5日%'] > 0).mean() * 100))
    print('\n最近10条:')
    print(R.tail(10)[['信号日', 'code', 'name', 'T1涨幅%', 'T1量比T', 'T2涨幅%', 'T2量比T1', '后5日%']].to_string(index=False))


if __name__ == '__main__':
    main()
