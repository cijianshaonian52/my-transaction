# -*- coding: utf-8 -*-
"""下载股票日线数据到 cache_data/ 目录（tushare 格式）

用法:
    # 下载单只股票
    python download_data.py 000017.SZ

    # 下载多只股票
    python download_data.py 000017.SZ 600519.SH

    # 下载沪深主板全部股票
    python download_data.py --all

    # 只下载沪市主板
    python download_data.py --all --market SH

    # 只下载深市主板
    python download_data.py --all --market SZ

    # 强制重新下载（覆盖已有数据）
    python download_data.py --all --force

    # 交互式输入
    python download_data.py
"""
import os
import sys
import glob
import time
import tushare as ts

# Tushare token 从环境变量读取（PowerShell: $env:TUSHARE_TOKEN='你的token'）
TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')

SAVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'cache_data')  # 项目根/cache_data（src 上一级）
START_DATE = '20240101'  # 数据起始日（多拉一些保证 MA20 在回测起始时有值）


def get_all_stocks(market=None):
    """获取沪深主板全部股票列表（用本地 stockdb_csv / _stock_list.csv，无需联网）

    Args:
        market: 'SH'=沪市主板, 'SZ'=深市主板, None=沪深全部主板
    Returns:
        list of ts_code, 如 ['000001.SZ', '000002.SZ', ...]
    """
    import pandas as pd

    cache_file = os.path.join(SAVE_DIR, '_stock_list.csv')
    if os.path.exists(cache_file):
        df = pd.read_csv(cache_file, dtype=str)
        df['symbol'] = df['ts_code'].str.split('.').str[0]
        print(f'读取本地股票列表：{len(df)} 只')
    else:
        # 从 stockdb_csv 逐股文件首行恢复代码与名称
        rows = []
        for p in glob.glob(os.path.join(os.path.dirname(SAVE_DIR), 'stockdb_csv', '*.csv')):
            code = os.path.basename(p)[3:9]
            try:
                with open(p, 'rb') as f:
                    first = f.readline().decode('gbk', errors='ignore')
                parts = first.split()
                name = parts[1] if len(parts) > 1 else ''
            except Exception:
                name = ''
            rows.append((code, name))
        if not rows:
            raise RuntimeError('未找到 stockdb_csv/*.csv，也没有 cache_data/_stock_list.csv 可用')
        df = pd.DataFrame(rows, columns=['symbol', 'name'])
        df['ts_code'] = df['symbol'].apply(
            lambda s: s + '.SH' if s.startswith('60') else s + '.SZ')
        print(f'读取 stockdb_csv 股票列表：{len(df)} 只')

    # 筛选主板：沪市 60 开头，深市 000 开头（排除中小板 002 和创业板 300）
    if market == 'SH':
        df = df[df['symbol'].str.startswith('60')]
    elif market == 'SZ':
        df = df[df['symbol'].str.startswith('000')]
    else:
        df = df[df['symbol'].str.match(r'^(60|000)')]

    df = df.copy()
    df['name'] = df['name'].fillna('').str.replace(' ', '')
    if 'list_date2' not in df.columns:
        df['list_date2'] = ''

    # 缓存到 cache_data/_stock_list.csv 方便下次快速读取
    df[['ts_code', 'symbol', 'name', 'list_date2']].to_csv(cache_file, index=False)
    print(f'主板股票：{len(df)} 只（{market or "沪深"}），已缓存 -> {cache_file}')

    return sorted(df['ts_code'].tolist())


def download(ts_code, force=False):
    """下载单只股票的日线数据"""
    filepath = os.path.join(SAVE_DIR, f'{ts_code}.csv')
    if os.path.exists(filepath) and not force:
        return False  # 跳过

    pro = ts.pro_api(token=TUSHARE_TOKEN)
    try:
        df = pro.daily(ts_code=ts_code, start_date=START_DATE)
        if len(df) == 0:
            print(f'  {ts_code} 无数据，跳过')
            return False
        df.to_csv(filepath, index=False)
        return True
    except Exception as e:
        print(f'  {ts_code} 下载失败：{e}')
        return False


def main():
    if not TUSHARE_TOKEN:
        print('未设置 TUSHARE_TOKEN 环境变量，无法下载数据。')
        sys.exit(1)
    os.makedirs(SAVE_DIR, exist_ok=True)

    # ── 参数解析 ──
    args = sys.argv[1:]
    download_all = '--all' in args
    market = None
    if '--market' in args:
        idx = args.index('--market')
        if idx + 1 < len(args):
            market = args[idx + 1].upper()
    force = '--force' in args

    if download_all:
        # ── 批量下载沪深主板 ──
        print(f'正在获取{"沪深" if not market else market}主板股票列表...')
        stocks = get_all_stocks(market=market)
        print(f'共 {len(stocks)} 只股票')

        # 检查已存在的文件
        if not force:
            existing = [s for s in stocks if os.path.exists(os.path.join(SAVE_DIR, f'{s}.csv'))]
            print(f'已有数据: {len(existing)} 只，跳过')
            stocks = [s for s in stocks if s not in existing]
            print(f'待下载: {len(stocks)} 只')

        if not stocks:
            print('所有股票数据已存在，无需下载。如需更新请加 --force')
            return

        # 分批下载（tushare 有限频）
        success = 0
        for i, ts_code in enumerate(stocks):
            print(f'[{i+1}/{len(stocks)}] {ts_code} ...', end=' ')
            if download(ts_code, force=force):
                success += 1
                print('OK')
            else:
                print('SKIP')
            # tushare 限频：每分钟 200 次，控制在 200/min
            if (i + 1) % 200 == 0:
                print('  达到 200 次限制，等待 60 秒...')
                time.sleep(60)

        print(f'\n下载完毕：{success} 只新下载，共 {len(stocks)} 只')

    else:
        # ── 下载指定股票 ──
        codes = [a for a in args if a not in ('--force', '--all', '--market') and not a.startswith('-')]
        if not codes:
            code = input('请输入股票代码（如 000017.SZ）：').strip()
            codes = [code]

        for code in codes:
            if download(code, force=force):
                print(f'  {code} 下载完成')
            else:
                print(f'  {code} 跳过（已存在或无数据）')

        print('\n运行回测：')
        for code in codes:
            print(f'  .venv\\Scripts\\python.exe main.py {code}')


if __name__ == '__main__':
    main()
