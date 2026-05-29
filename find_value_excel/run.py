import pandas as pd

# 读取 Excel 文件
file_path = "fox.xlsx"  # 替换为你的 Excel 文件路径
sheet1 = pd.read_excel(file_path, sheet_name="Sheet1")
sheet2 = pd.read_excel(file_path, sheet_name="Sheet2")

# 去除 Sheet1 中 Name 列的重复值，只保留第一个匹配的值
sheet1.drop_duplicates(subset=["Name"], keep="first", inplace=True)

# 将 Sheet1 的 Name 列设置为索引，方便查找
sheet1.set_index("Name", inplace=True)

# 在 Sheet2 中创建新的列，用于存储从 Sheet1 中查找的结果
sheet2["Label"] = None
sheet2["Type"] = None
sheet2["Uint"] = None
sheet2["ID"] = None

# 遍历 Sheet2 中的 Name，查找并填充数据
for index, row in sheet2.iterrows():
    name = row["Name"]
    if name in sheet1.index:  # 如果 Name 存在于 Sheet1 中
        sheet2.at[index, "Label"] = sheet1.loc[name, "Label"]
        sheet2.at[index, "Type"] = sheet1.loc[name, "Type"]
        sheet2.at[index, "Uint"] = sheet1.loc[name, "Uint"]
        sheet2.at[index, "ID"] = sheet1.loc[name, "ID"]

# 将结果保存到新的 Excel 文件
output_file_path = "output_excel_file.xlsx"  # 替换为输出文件路径
sheet2.to_excel(output_file_path, index=False)

print(f"处理完成，结果已保存到: {output_file_path}")