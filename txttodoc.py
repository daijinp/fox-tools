# -*- coding: utf-8 -*-
import os
from docx import Document


def batch_txt_to_docx(folder_path):
    for filename in os.listdir(folder_path):
        if filename.endswith(".txt"):
            txt_filepath = os.path.join(folder_path, filename)
            docx_filepath = os.path.join(folder_path, filename[:-4] + ".docx")  # 输出为docx格式

            try:
                with open(txt_filepath, 'r', encoding='utf-8') as f:
                    content = f.read()

                document = Document()
                document.add_paragraph(content)
                document.save(docx_filepath)

                print(f"转换成功：{txt_filepath} -> {docx_filepath}")

                # 如果需要保存为 .doc (需要安装 win32com，见方法二)
                # doc_filepath = os.path.join(folder_path, filename[:-4] + ".doc")
                # convert_docx_to_doc(docx_filepath,doc_filepath)

            except FileNotFoundError:
                print(f"错误：找不到文件 {txt_filepath}")
            except Exception as e:
                print(f"转换 {txt_filepath} 过程中发生错误：{e}")


# 使用win32com将docx转为doc
def convert_docx_to_doc(docx_filepath, doc_filepath):
    try:
        import win32com.client
        word = win32com.client.Dispatch("Word.Application")
        doc = word.Documents.Open(docx_filepath)
        doc.SaveAs(doc_filepath, FileFormat=0)  # FileFormat=0 代表 .doc 格式
        doc.Close()
        word.Quit()
        os.remove(docx_filepath)  # 删除中间docx文件
        print(f"docx转doc成功：{docx_filepath} -> {doc_filepath}")
    except Exception as e:
        print(f"转换过程中发生错误：{e}")


# 使用示例
folder_path = "4"  # 替换为你的文件夹路径
batch_txt_to_docx(folder_path)
