# 下载/更新基准指数日线到 cache_data/_index_daily.csv（供偏离值计算）
# 上证指数 sh.000001 对应沪市(60开头)，深证成指 sz.399001 对应深市(00开头)
import os, sys, io
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'cache_data', '_index_daily.csv')

import baostock as bs


def fetch(code, start):
    rs = bs.query_history_k_data_plus(code, 'date,close', start_date=start, end_date='2026-12-31', frequency='d')
    rows = []
    while rs.error_code == '0' and rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=['date', 'close'])
    df['index'] = code
    df['close'] = df['close'].astype(float)
    return df


def main():
    bs.login()
    try:
        old = pd.read_csv(OUT, dtype={'date': str}) if os.path.exists(OUT) else pd.DataFrame(columns=['date', 'close', 'index'])
        parts = []
        for code in ('sh.000001', 'sz.399001'):
            have = old[old['index'] == code]
            start = '2020-01-01' if have.empty else (pd.to_datetime(have['date'].max()) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
            new = fetch(code, start)
            parts.append(have)
            if not new.empty:
                parts.append(new)
                print('%s 新增 %d 行 (至 %s)' % (code, len(new), new['date'].max()))
            else:
                print('%s 已是最新 (%s)' % (code, have['date'].max() if not have.empty else '无'))
        R = pd.concat(parts).drop_duplicates(subset=['index', 'date']).sort_values(['index', 'date'])
        R.to_csv(OUT, index=False, encoding='utf-8-sig')
        print('已存', OUT, len(R), '行')
    finally:
        bs.logout()


if __name__ == '__main__':
    main()
