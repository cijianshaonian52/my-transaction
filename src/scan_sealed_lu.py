# -*- coding: utf-8 -*-
"""筛最新交易日"一封到底"涨停股 —— limit_up_study 检验5a的观察清单

背景(2026-09-25 全市场实证, src/limit_up_study.py):
  - 一封到底的涨停: 纸面3日 +7.49%/胜率64%, 但排队时间优先基本买不进(占首板3%)
  - 盘中开过的涨停(能成交的): 3日中位 0%
  - 一字开板后再接力: 均亏3% —— 故本清单仅用于观察/验证, 不构成买入建议
用法: python src/scan_sealed_lu.py [--date YYYY-MM-DD]
输出: 控制台 + output/sealed_lu_YYYYMMDD.csv
"""
import sys, io, os, argparse, datetime
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKDB_DIR = os.path.join(ROOT, 'stockdb_csv')
IND_FILE = os.path.join(ROOT, 'cache_data', '_stock_industry.csv')


def limit_threshold(code6, name):
    if code6.startswith(('300', '301', '688', '689')):
        return 19.8
    if code6.startswith(('43', '83', '87', '920')):
        return 29.8
    if name and 'ST' in str(name).upper():
        return 4.8
    return 9.8


def tail_rows(fp, n=30):
    """只读CSV尾部n行数据(跳过表头与#数据来源行)"""
    with open(fp, 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - n * 60))
        raw = f.read().decode('gbk', errors='ignore').splitlines()
    rows = []
    for ln in raw:
        p = ln.split(',')
        if len(p) in (7, 8) and p[0][:4].isdigit() and '-' in p[0]:
            rows.append(p)
    return rows[-n:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='指定交易日 YYYY-MM-DD, 默认取数据最新日')
    args = ap.parse_args()

    ind = pd.read_csv(IND_FILE, dtype=str)
    ind['code6'] = ind['ts_code'].str.split('.').str[0]
    name_map = ind.set_index('code6')['name'].to_dict()
    ind_map = ind.set_index('code6')['industry'].to_dict()

    recs = []
    latest = None
    for fn in sorted(os.listdir(STOCKDB_DIR)):
        if not fn.endswith('.csv'):
            continue
        code6 = fn.split('#')[1].split('.')[0]
        rows = tail_rows(os.path.join(STOCKDB_DIR, fn))
        if len(rows) < 3:
            continue
        d = [r[0] for r in rows]
        c = [float(r[4]) for r in rows]
        o = [float(r[1]) for r in rows]
        lo = [float(r[3]) for r in rows]
        v = [float(r[5]) for r in rows]
        a = [float(r[6]) for r in rows]
        if latest is None or d[-1] > latest:
            latest = d[-1]
        target = args.date if args.date else None
        # 找目标日在尾部窗口中的位置
        ti = None
        for i in range(len(d) - 1, -1, -1):
            if (target and d[i] == target) or (not target and i == len(d) - 1):
                ti = i
                break
        if ti is None or ti == 0:
            continue
        name = name_map.get(code6, '')
        thr = limit_threshold(code6, name)
        pct = (c[ti] / c[ti - 1] - 1) * 100
        if pct < thr:                       # 未涨停
            continue
        low_pct = (lo[ti] / c[ti - 1] - 1) * 100
        sealed = low_pct >= thr             # 最低价也在涨停价 → 一封到底
        # 连板高度(截至目标日, 窗口内连续涨停天数)
        bh = 1
        for k in range(ti - 1, 0, -1):
            if (c[k] / c[k - 1] - 1) * 100 >= thr:
                bh += 1
            else:
                break
        vol_ma20 = sum(v[max(0, ti - 20):ti]) / max(1, len(v[max(0, ti - 20):ti]))
        vr = v[ti] / vol_ma20 if vol_ma20 > 0 else None
        recs.append({
            '代码': code6, '名称': name,
            '行业': ind_map.get(code6, '未知') or '未知',
            '封板类型': '一封到底' if sealed else '盘中开过',
            '连板高度': bh, '收盘价': c[ti], '涨幅%': round(pct, 2),
            '成交额(亿)': round(a[ti] / 1e8, 2),
            '量比': round(vr, 2) if vr else None,
            '日期': d[ti],
        })

    df = pd.DataFrame(recs)
    date_used = args.date if args.date else latest
    df = df[df['日期'] == date_used]
    df = df.sort_values(['封板类型', '连板高度', '成交额(亿)'],
                        ascending=[True, False, False]).reset_index(drop=True)

    sealed = df[df['封板类型'] == '一封到底']
    opened = df[df['封板类型'] == '盘中开过']
    print(f'交易日 {date_used}: 全市场涨停 {len(df)} 只, '
          f'其中一封到底(买不到) {len(sealed)} 只 / 盘中开过(能成交) {len(opened)} 只\n')
    print('══ 一封到底(正期望但买不到, 仅观察) ══')
    if len(sealed):
        print(sealed[['代码', '名称', '行业', '连板高度', '收盘价', '涨幅%',
                      '成交额(亿)', '量比']].to_string(index=False))
    else:
        print('(无)')
    print(f'\n══ 对照: 盘中开过(能成交, 检验5a口径3日中位≈0%) 共{len(opened)}只, 前15 ══')
    if len(opened):
        print(opened[['代码', '名称', '行业', '连板高度', '收盘价', '涨幅%',
                      '成交额(亿)', '量比']].head(15).to_string(index=False))
    print('\n提醒: 一封到底的票次日若继续一字仍买不到; 若开板, 检验4显示开板接力3日均亏3%。'
          '本清单仅用于观察"正期望但买不到"群体的后续演化。')

    out = os.path.join(ROOT, 'output', f'sealed_lu_{date_used.replace("-", "")}.csv')
    df.to_csv(out, index=False, encoding='utf-8-sig')
    print(f'已保存: {out}')


if __name__ == '__main__':
    main()
