# 四日形态扫描: T涨停 -> T+1放量不涨停(无炸板/无跌停) -> T+2缩量 -> T+3再缩量(量<T+2,无跌停)
# 仅主板(60/00), 剔除ST。信号日=T+3。输出信号明细 + 后续N日表现
import os, sys, io
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_DIR = os.path.join(ROOT, 'stockdb_csv')
HOLD = [1, 3, 5]
FEE = 0.0015


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
    s = 0
    j = i
    while j >= 1:
        if (closes[j] / closes[j - 1] - 1) * 100 >= th:
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
        C, V, H, D = df['close'].values, df['volume'].values, df['high'].values, df['date'].values
        n = len(df)
        for i in range(2, n):           # i = T+1, T=i-1, T+2=i+1, T+3=i+2
            t = i - 1
            j2, j3 = i + 1, i + 2
            if j3 >= n:
                break
            # T日涨停
            pct_t = (C[t] / C[t - 1] - 1) * 100
            if pct_t < 9.9:
                continue
            n板 = streak(C, t)
            # T+1: 不涨停、无炸板、无跌停、放量
            pct_t1 = (C[i] / C[t] - 1) * 100
            if pct_t1 >= 9.9 or pct_t1 <= -9.5:
                continue
            if H[i] >= round(C[t] * 1.10, 2) - 1e-6:
                continue
            if V[i] <= V[t]:
                continue
            # T+2: 缩量、非跌停
            pct_t2 = (C[j2] / C[i] - 1) * 100
            if pct_t2 <= -9.5 or V[j2] >= V[i]:
                continue
            # T+3: 再缩量(量<T+2)、非跌停
            pct_t3 = (C[j3] / C[j2] - 1) * 100
            if pct_t3 <= -9.5 or V[j3] >= V[j2]:
                continue
            rec = {'信号日': D[j3], 'code': code, 'name': nm.get(code, ''),
                   'T日': D[t], 'T连板数': n板, 'T日涨幅%': round(pct_t, 2),
                   'T1涨幅%': round(pct_t1, 2), 'T1量比T': round(V[i] / V[t], 2),
                   'T2涨幅%': round(pct_t2, 2), 'T2量比T1': round(V[j2] / V[i], 2),
                   'T3涨幅%': round(pct_t3, 2), 'T3量比T2': round(V[j3] / V[j2], 2),
                   '信号收盘': round(C[j3], 2)}
            for h in HOLD:
                k = j3 + h
                rec['后%d日%%' % h] = round((C[k] / C[j3] - 1) * 100 - FEE * 100, 2) if k < n else None
            sigs.append(rec)

    R = pd.DataFrame(sigs)
    if R.empty:
        print('无信号')
        return
    out = os.path.join(ROOT, 'output', 'zt_vol_shrink4_signals.csv')
    R.to_csv(out, index=False, encoding='utf-8-sig')
    print('四日形态总信号 %d 条 -> %s' % (len(R), out))
    R['y'] = R['信号日'].str[:4]
    for y, g in R.groupby('y'):
        print('  %s  n=%d  后5日均值%+.2f%%  胜率%.1f%%' % (y, len(g), g['后5日%'].mean(),
              (g['后5日%'] > 0).mean() * 100))
    print('\n最近10条:')
    print(R.tail(10)[['信号日', 'code', 'name', 'T连板数', 'T1涨幅%', 'T2涨幅%', 'T3涨幅%', '后5日%']].to_string(index=False))


if __name__ == '__main__':
    main()
