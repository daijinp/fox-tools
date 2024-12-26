import pandas as pd
# 读取 CSV 文件
df = pd.read_csv('check_not_mark_log_0906.csv')

# 去除空行
df_cleaned = df.dropna(how='all')

# 保存到新的 CSV 文件
df_cleaned.to_csv('check_not_mark_log_0906_csvoutput.csv', index=False)
