# -*- coding: utf-8 -*-
"""按图内题材分组算首板溢价率(替代CSRC行业分组)。

输入:
  output/first_board_premium_YYYYMMDD.csv  (首板明细, 含 日期/代码/名称/行业/溢价等)
  output/topic_map_YYYYMMDD.csv            (图转录: date,topic,name)
输出:
  output/topic_premium_YYYYMMDD.csv  (题材分组表, 按样本数降序)
  控制台同时打印。
"""
import os
import sys
import pandas as pd
import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, 'output')

PREM = os.path.join(OUT, 'first_board_premium_20261021.csv')
TMAP = os.path.join(OUT, 'topic_map_20261021.csv')

# 图内题材名归一化(同主线跨日图名差异)
NORMALIZE = {
    '传媒': 'AI应用/传媒',
    'AI应用': 'AI应用/传媒',
    '大消费/冰雪产业': '大消费',
    '算力/半导体产业链': '算力/半导体',
    '军工/航天': '航天/低空经济',
    '航天': '航天/低空经济',
    '化工': '化工/染料',
    '染料': '化工/染料',
    '化工/大农业': '化工/染料',
    '风电': '电力/风电',
    '电力': '电力/风电',
    '电力/数据中心供电': '电力/风电',
    '煤炭/煤化工': '煤炭',
}

def main():
    prem = pd.read_csv(PREM, encoding='utf-8-sig')
    tmap = pd.read_csv(TMAP, encoding='utf-8-sig')
    tmap['topic_norm'] = tmap['topic'].replace(NORMALIZE)

    df = prem.merge(
        tmap.rename(columns={'date': '日期', 'name': '名称'})[['日期', '名称', 'topic_norm']],
        on=['日期', '名称'], how='left')
    df['topic_norm'] = df['topic_norm'].fillna('(未转录)')

    # 数值列
    for c in ['开盘溢价%', '收盘溢价%', '最高溢价%']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['一封到底_bin'] = (df['一封到底'] == '是').astype(int)
    df['次日涨停_bin'] = (df['次日涨停'] == '是').astype(int)

    g = df.groupby('topic_norm')
    tab = pd.DataFrame({
        '样本数': g.size(),
        '开盘溢价_均': g['开盘溢价%'].mean(),
        '开盘溢价_中': g['开盘溢价%'].median(),
        '收盘溢价_均': g['收盘溢价%'].mean(),
        '收盘溢价_中': g['收盘溢价%'].median(),
        '最高溢价_均': g['最高溢价%'].mean(),
        '高开率%': g.apply(lambda x: (x['开盘溢价%'] > 0).mean() * 100, include_groups=False),
        '次日涨停率%': g['次日涨停_bin'].mean() * 100,
        '一封到底率%': g['一封到底_bin'].mean() * 100,
    }).round(2)
    tab = tab[tab['样本数'] >= 3].sort_values('样本数', ascending=False)

    print(f'总样本: {len(df)}  已转录: {(df["topic_norm"] != "(未转录)").sum()}')
    print(f'\n=== 按题材分组 (样本数>=3) ===')
    print(tab.to_string())

    out_csv = os.path.join(OUT, 'topic_premium_20261021.csv')
    tab.to_csv(out_csv, encoding='utf-8-sig')
    print(f'\n保存: {out_csv}')

if __name__ == '__main__':
    main()
