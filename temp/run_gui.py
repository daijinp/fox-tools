import pandas as pd
import re
import os
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
import threading

def clean_and_reorder_csv(file_path, log_callback=None):
    """
    读取CSV文件，删除内容全为 '65535' 的列，并对带有数字后缀的列重新编号，
    同时严格保持列的原始相对顺序。
    """
    def log(message):
        if log_callback:
            log_callback(message + "\n")
        else:
            print(message)
    
    try:
        # 读取CSV文件
        df = pd.read_csv(file_path)
        # 存储原始列名顺序
        original_cols = list(df.columns)
        log(f"✅ 成功读取文件：{os.path.basename(file_path)}")
        log(f"📊 原始列数：{len(df.columns)}")
    except FileNotFoundError:
        log(f"❌ 错误：文件未找到在路径：{file_path}")
        return False
    except Exception as e:
        log(f"❌ 读取文件时发生错误：{e}")
        return False

    # --- 1. 确定要删除的列 ---
    cols_to_drop = []
    for col in df.columns:
        # 检查所有非空值是否全部等于 65535
        try:
            if (df[col].dropna() == 65535).all() and not df[col].empty:
                cols_to_drop.append(col)
        except:
            continue

    if cols_to_drop:
        df = df.drop(columns=cols_to_drop)
        log(f"🗑️ 已删除 {len(cols_to_drop)} 列（内容全部为 65535）")
    else:
        log("ℹ️ 未找到内容全部为 65535 的列")
    
    # 获取删除后的列顺序模板 (即：原始顺序中哪些列保留了)
    remaining_cols_order_template = [col for col in original_cols if col not in cols_to_drop]

    # --- 2. 重新编号并生成重命名映射 ---
    
    # 匹配模式: (英文字母和点.) + (数字)
    pattern = re.compile(r'^([a-zA-Z.]+)\.(\d+)$')
    rename_map = {}
    
    # 计数器：记录每个前缀应该开始的新数字
    prefix_counters = {} 
    
    # 遍历保留下来的列，按照它们在原始文件中的顺序进行处理
    for col_old in remaining_cols_order_template:
        match = pattern.match(col_old)
        
        if match:
            prefix = match.group(1) # e.g., 'Cel.Res.'
            
            # 初始化或获取该前缀的计数器
            if prefix not in prefix_counters:
                prefix_counters[prefix] = 1
            
            # 生成新名称
            new_number = prefix_counters[prefix]
            col_new = f'{prefix}.{new_number}'
            
            # 如果新旧名称不同，则加入重命名映射
            if col_old != col_new:
                 rename_map[col_old] = col_new
            
            # 计数器递增
            prefix_counters[prefix] += 1
            

    if rename_map:
        df = df.rename(columns=rename_map)
        log(f"🔄 已重新编号 {len(rename_map)} 个列（数字后缀连续化）")
    else:
        log("ℹ️ 未找到需要重新编号的列")

    # --- 3. 应用最终列顺序（保持原始相对顺序） ---
    
    # 构造最终的列顺序列表：遍历模板，将旧名称替换为新名称
    final_cols_order = []
    for col_old in remaining_cols_order_template:
        # 如果旧列名在重命名映射中，则使用新名称，否则保留旧名称
        col_final = rename_map.get(col_old, col_old)
        final_cols_order.append(col_final)
        
    # 应用最终列顺序，确保顺序正确
    df = df[final_cols_order]
    log(f"📋 最终列数：{len(df.columns)}")
    
    # --- 4. 输出执行完的文件 ---
    base, ext = os.path.splitext(file_path)
    output_file_path = f"{base}_cleaned{ext}"

    try:
        df.to_csv(output_file_path, index=False)
        log(f"\n✅ 处理完成！")
        log(f"💾 新文件已保存：{os.path.basename(output_file_path)}")
        return output_file_path
    except Exception as e:
        log(f"❌ 保存文件时发生错误：{e}")
        return False


class CSVCleanerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("CSV 数据清理工具 v1.0")
        self.root.geometry("700x500")
        self.root.resizable(True, True)
        
        # 设置窗口图标（如果有的话）
        try:
            # self.root.iconbitmap('icon.ico')  # 可选：添加图标
            pass
        except:
            pass
        
        # 文件路径变量
        self.file_path = tk.StringVar()
        
        # 创建界面
        self.create_widgets()
        
    def create_widgets(self):
        # 顶部标题
        title_frame = tk.Frame(self.root, bg="#4CAF50", height=60)
        title_frame.pack(fill=tk.X, pady=(0, 10))
        title_frame.pack_propagate(False)
        
        title_label = tk.Label(
            title_frame, 
            text="CSV 数据清理工具", 
            font=("微软雅黑", 16, "bold"),
            bg="#4CAF50",
            fg="white"
        )
        title_label.pack(expand=True)
        
        # 文件选择区域
        file_frame = tk.Frame(self.root, padx=20, pady=10)
        file_frame.pack(fill=tk.X)
        
        tk.Label(file_frame, text="选择CSV文件：", font=("微软雅黑", 10)).pack(anchor=tk.W)
        
        input_frame = tk.Frame(file_frame)
        input_frame.pack(fill=tk.X, pady=(5, 0))
        
        self.file_entry = tk.Entry(
            input_frame, 
            textvariable=self.file_path,
            font=("微软雅黑", 9),
            state='readonly'
        )
        self.file_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        browse_btn = tk.Button(
            input_frame,
            text="浏览...",
            command=self.browse_file,
            font=("微软雅黑", 9),
            bg="#2196F3",
            fg="white",
            width=10,
            cursor="hand2"
        )
        browse_btn.pack(side=tk.LEFT, padx=(5, 0))
        
        # 功能说明
        info_frame = tk.Frame(self.root, padx=20, pady=5)
        info_frame.pack(fill=tk.X)
        
        info_text = "功能说明：\n" \
                    "• 删除所有值都为 65535 的列\n" \
                    "• 对带数字后缀的列（如 Cel.Res.1, Cel.Res.2）重新编号，使其连续\n" \
                    "• 保持原始列的相对顺序不变"
        
        info_label = tk.Label(
            info_frame,
            text=info_text,
            font=("微软雅黑", 9),
            justify=tk.LEFT,
            fg="#555",
            bg="#f0f0f0",
            padx=10,
            pady=10
        )
        info_label.pack(fill=tk.X)
        
        # 处理按钮
        btn_frame = tk.Frame(self.root, padx=20, pady=10)
        btn_frame.pack(fill=tk.X)
        
        self.process_btn = tk.Button(
            btn_frame,
            text="开始处理",
            command=self.process_file,
            font=("微软雅黑", 11, "bold"),
            bg="#4CAF50",
            fg="white",
            height=2,
            cursor="hand2"
        )
        self.process_btn.pack(fill=tk.X)
        
        # 日志区域
        log_frame = tk.Frame(self.root, padx=20, pady=10)
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        tk.Label(log_frame, text="处理日志：", font=("微软雅黑", 10)).pack(anchor=tk.W)
        
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            font=("Consolas", 9),
            wrap=tk.WORD,
            bg="#f9f9f9",
            height=10
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(5, 0))
        
    def browse_file(self):
        filename = filedialog.askopenfilename(
            title="选择CSV文件",
            filetypes=[("CSV文件", "*.csv"), ("所有文件", "*.*")]
        )
        if filename:
            self.file_path.set(filename)
            self.log("📂 已选择文件：" + os.path.basename(filename))
    
    def log(self, message):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()
    
    def process_file(self):
        file_path = self.file_path.get()
        
        if not file_path:
            messagebox.showwarning("警告", "请先选择一个CSV文件！")
            return
        
        if not os.path.exists(file_path):
            messagebox.showerror("错误", "所选文件不存在！")
            return
        
        # 清空日志
        self.log_text.delete(1.0, tk.END)
        self.log("=" * 50)
        self.log("开始处理文件...")
        self.log("=" * 50)
        
        # 禁用按钮
        self.process_btn.config(state=tk.DISABLED, text="处理中...")
        
        # 在新线程中处理，避免UI冻结
        def process_thread():
            result = clean_and_reorder_csv(file_path, log_callback=self.log)
            
            # 恢复按钮
            self.process_btn.config(state=tk.NORMAL, text="开始处理")
            
            if result:
                self.log("=" * 50)
                messagebox.showinfo(
                    "完成", 
                    f"处理完成！\n\n输出文件：\n{os.path.basename(result)}"
                )
                # 询问是否打开文件所在目录
                if messagebox.askyesno("打开文件夹", "是否打开输出文件所在的文件夹？"):
                    os.startfile(os.path.dirname(result))
        
        thread = threading.Thread(target=process_thread, daemon=True)
        thread.start()


def main():
    root = tk.Tk()
    app = CSVCleanerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

 