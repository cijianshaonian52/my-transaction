# import tushare as ts
# import time
# import os
# import pandas as pd
# ts.set_token(os.environ['TUSHARE_TOKEN'])
# pro = ts.pro_api()
#
# save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache_data')
# os.makedirs(save_dir, exist_ok=True)
# stocks_file = os.path.join(save_dir, 'stocks.csv')
#
# # 股票列表本地缓存：有就直接读，没有才拉取
# if os.path.exists(stocks_file):
#     stocks = pd.read_csv(stocks_file, dtype={'ts_code': str})
#     print(f"从本地读取股票列表：{len(stocks)}只")
# else:
#     stocks = pro.stock_basic(list_status="L", fields='ts_code,name')
#     stocks.to_csv(stocks_file, index=False)
#     print(f"从tushare拉取股票列表：{len(stocks)}只，已缓存到 {stocks_file}")
#
# print(f"全市场{len(stocks)}只股票")
# for i,row in stocks.iterrows():
#     code=row['ts_code']
#     filepath = os.path.join(save_dir,f"{code}.csv")
#     # 已有的跳过
#     if os.path.exists(filepath):
#         continue
#     try:
#         df = pro.daily(ts_code=code,start_date='20160101')
#         if len(df) > 0:
#             df.to_csv(filepath, index=False)
#     except Exception as e:
#         print(f"{code}失败：{e}")
#     #tushare限速：每分钟50次，每次请求后等待1.3秒
#     time.sleep(1.3)
#     if i > 0 and i % 50 == 0:
#         print(f" 已下载{i}/{len(stocks)}...")
#
# print("全部下载完成！")


import baostock as bs
import pandas as pd

#### 登陆系统 ####
lg = bs.login()
# 显示登陆返回信息
print('login respond error_code:' + lg.error_code)
print('login respond  error_msg:' + lg.error_msg)

#### 获取沪深A股历史K线数据 ####
# 详细指标参数，参见“历史行情指标参数”章节；“分钟线”参数与“日线”参数不同。“分钟线”不包含指数。
# 分钟线指标：date,time,code,open,high,low,close,volume,amount,adjustflag
# 周月线指标：date,code,open,high,low,close,volume,amount,adjustflag,turn,pctChg
rs = bs.query_history_k_data_plus("sh.600000",
                                  "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST",
                                  start_date='2024-07-01', end_date='2024-12-31',
                                  frequency="d", adjustflag="3")
print('query_history_k_data_plus respond error_code:' + rs.error_code)
print('query_history_k_data_plus respond  error_msg:' + rs.error_msg)

#### 打印结果集 ####
data_list = []
while (rs.error_code == '0') & rs.next():
    # 获取一条记录，将记录合并在一起
    data_list.append(rs.get_row_data())
result = pd.DataFrame(data_list, columns=rs.fields)

#### 结果集输出到csv文件 ####
result.to_csv("D:\\history_A_stock_k_data.csv", index=False)
print(result)

#### 登出系统 ####
bs.logout()