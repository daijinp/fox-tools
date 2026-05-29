"""
2025年3月20日15:11:09：这个脚本作翻译HTML使用
"""
import os
from pathlib import Path

translation_dict = {
    "公共信息": "Public information",
    "其他信息": "Other information",
    "是否必须": "Is it required",
    "默认值": "Default value",
    "基本信息": "Basic information",
    "接口描述": "Interface description",
    "请求参数": "Request parameter",
    "参数名称": "Parameter name",
    "返回数据": "Response Data",
    "枚举备注": "Enumeration note",
    "参数值": "Parameter value",
    "最大值": "Maximum value",
    "最小值": "Minimum value",
    "备注": "Note",
    "枚举": "Enumeration",
    "示例": "Example",
    "名称": "Name",
    "类型": "Type",
    "非必须": "Not Required",
    "是": "Required",
    "必须": "Required"
}


def replace_in_file(file_path, output_dir):
    with open(file_path, "r", encoding="utf-8") as file:
        content = file.read()

    for chinese, english in translation_dict.items():
        content = content.replace(chinese, english)

    output_file_path = os.path.join(output_dir, os.path.basename(file_path))

    with open(output_file_path, "w", encoding="utf-8") as file:
        file.write(content)


def process_directory(input_dir, output_dir):
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    for file_name in os.listdir(input_dir):
        if file_name.endswith(".html"):
            file_path = os.path.join(input_dir, file_name)
            replace_in_file(file_path, output_dir)
            print(f"Processed: {file_path} -> {os.path.join(output_dir, file_name)}")


# 示例用法
input_directory = "./input/"
output_directory = "./output/"
process_directory(input_directory, output_directory)
