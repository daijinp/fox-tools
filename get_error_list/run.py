"""Windows desktop entry point; run with ../venv/Scripts/python.exe."""

import calendar
from datetime import date, timedelta
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from api import ApiError, CONCURRENCY, DEFAULT_CONFIG, FoxClient, PAGE_SIZE, QueryCancelled, validate_config
from credentials import CredentialStore
from exporter import export_alarms, read_sns


def app_directory():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def load_config(directory):
    path = Path(directory) / "config.json"
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取 config.json，请检查文件内容是否为有效 JSON。") from exc
    if not isinstance(value, dict):
        raise ValueError("config.json 必须是 JSON 对象。")
    return validate_config(value)


class DateInput(ttk.Frame):
    def __init__(self, parent, value):
        super().__init__(parent)
        self.value = tk.StringVar(value=value.isoformat())
        self.entry = ttk.Entry(self, textvariable=self.value, width=14)
        self.entry.pack(side="left")
        self.button = ttk.Button(self, text="选择日期", command=self.choose)
        self.button.pack(side="left", padx=(6, 0))

    def get(self):
        try:
            return date.fromisoformat(self.value.get().strip())
        except ValueError as exc:
            raise ValueError("日期格式应为 YYYY-MM-DD，例如 2026-10-27。") from exc

    def enable(self, enabled):
        for widget in (self.entry, self.button):
            widget.configure(state="normal" if enabled else "disabled")

    def choose(self):
        try:
            selected = self.get()
        except ValueError:
            selected = date.today()
        dialog = tk.Toplevel(self)
        dialog.title("选择日期")
        dialog.resizable(False, False)
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.bind("<Escape>", lambda event: dialog.destroy())
        content = ttk.Frame(dialog, padding=12)
        content.pack()
        month_label = tk.StringVar()
        shown = [selected.year, selected.month]
        header = ttk.Frame(content)
        header.pack(fill="x")
        days = ttk.Frame(content)
        days.pack(pady=(10, 0))

        def select(day):
            self.value.set(date(shown[0], shown[1], day).isoformat())
            dialog.destroy()

        def render():
            month_label.set(f"{shown[0]} 年 {shown[1]:02d} 月")
            for child in days.winfo_children():
                child.destroy()
            for column, label in enumerate("一二三四五六日"):
                ttk.Label(days, text=label, anchor="center", width=5).grid(row=0, column=column, pady=3)
            for row, week in enumerate(calendar.monthcalendar(*shown), 1):
                for column, day in enumerate(week):
                    if day:
                        ttk.Button(days, text=str(day), width=4,
                                   command=lambda number=day: select(number)).grid(row=row, column=column, padx=2, pady=2)

        def move(step):
            position = shown[0] * 12 + shown[1] - 1 + step
            year, month = divmod(position, 12)
            if 1 <= year <= 9999:
                shown[:] = [year, month + 1]
                render()

        ttk.Button(header, text="上月", width=6, command=lambda: move(-1)).pack(side="left")
        ttk.Label(header, textvariable=month_label, anchor="center", width=20).pack(side="left", expand=True)
        ttk.Button(header, text="下月", width=6, command=lambda: move(1)).pack(side="right")
        render()


class AlarmApp:
    def __init__(self, root, directory=None, credential_directory=None):
        self.root = root
        self.directory = Path(directory) if directory is not None else app_directory()
        self.config = load_config(self.directory)
        self.store = CredentialStore(self.config["domain"], credential_directory)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.closing = False
        self.last_output = None
        root.title("设备告警导出工具")
        root.geometry("860x690")
        root.minsize(800, 630)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        outer = ttk.Frame(root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="设备告警导出", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text=f"读取 SN.csv，按日期范围查询；并发 {CONCURRENCY}，每个 SN 最多查询第一页 {PAGE_SIZE} 条。",
                  foreground="#555555").pack(anchor="w", pady=(6, 14))
        account = ttk.LabelFrame(outer, text="登录账号", padding=12)
        account.pack(fill="x")
        account.columnconfigure(1, weight=1)
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.remember = tk.BooleanVar(value=False)
        ttk.Label(account, text="账号").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.user_entry = ttk.Entry(account, textvariable=self.username)
        self.user_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(account, text="密码").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.password_entry = ttk.Entry(account, textvariable=self.password, show="●")
        self.password_entry.grid(row=1, column=1, sticky="ew", pady=(10, 0))
        self.remember_button = ttk.Checkbutton(account, text="保存账号密码（仅当前 Windows 用户可读取）", variable=self.remember)
        self.remember_button.grid(row=2, column=1, sticky="w", pady=(10, 0))
        self.forget_button = ttk.Button(account, text="清除已保存信息", command=self.forget)
        self.forget_button.grid(row=0, column=2, rowspan=2, padx=(16, 0))
        dates = ttk.LabelFrame(outer, text="查询日期", padding=12)
        dates.pack(fill="x", pady=(12, 0))
        today = date.today()
        ttk.Label(dates, text="开始日期").pack(side="left", padx=(0, 8))
        self.begin = DateInput(dates, today - timedelta(days=7))
        self.begin.pack(side="left")
        ttk.Label(dates, text="结束日期").pack(side="left", padx=(25, 8))
        self.end = DateInput(dates, today)
        self.end.pack(side="left")
        ttk.Label(outer, text=f"SN 文件：{self.directory / 'SN.csv'}", wraplength=800).pack(anchor="w", pady=(12, 4))
        ttk.Label(outer, text="每行一个 SN，无表头；修改后直接开始查询。重复 SN 会自动去重。",
                  foreground="#555555").pack(anchor="w")
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=12)
        self.start_button = ttk.Button(actions, text="开始查询并导出", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止查询", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(actions, text="打开结果文件夹", command=self.open_output, state="disabled")
        self.open_button.pack(side="left")
        self.progress = ttk.Progressbar(outer, mode="determinate")
        self.progress.pack(fill="x")
        self.status = tk.StringVar(value="就绪")
        ttk.Label(outer, textvariable=self.status, wraplength=800).pack(anchor="w", pady=(6, 8))
        self.log = scrolledtext.ScrolledText(outer, height=10, wrap="word", state="disabled",
                                           font=("Microsoft YaHei UI", 9))
        self.log.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"服务地址：{self.config['domain']}", foreground="#666666").pack(anchor="w", pady=(10, 0))
        try:
            saved = self.store.load()
            if saved:
                self.username.set(saved[0])
                self.password.set(saved[1])
                self.remember.set(True)
        except ValueError as exc:
            self.write_log(str(exc))
        root.after(100, self.poll)

    def write_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        # Bound widget memory for large SN batches; full log remains on disk.
        if int(self.log.index("end-1c").split(".")[0]) > 1500:
            self.log.delete("1.0", "301.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for widget in (self.user_entry, self.password_entry, self.remember_button,
                       self.forget_button, self.start_button):
            widget.configure(state=state)
        self.begin.enable(not busy)
        self.end.enable(not busy)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def forget(self):
        try:
            self.store.delete()
        except OSError:
            messagebox.showerror("清除失败", "无法清除已保存信息，请检查文件权限。", parent=self.root)
            return
        self.password.set("")
        self.remember.set(False)
        self.write_log("已清除本机保存的账号密码。")

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            username, password = self.username.get().strip(), self.password.get()
            if not username or not password:
                raise ValueError("请输入账号和密码。")
            begin, end = self.begin.get(), self.end.get()
            if begin > end:
                raise ValueError("开始日期不能晚于结束日期。")
            sns = read_sns(self.directory / "SN.csv")
        except (ValueError, OSError) as exc:
            messagebox.showerror("请检查输入", str(exc), parent=self.root)
            return
        self.cancel.clear()
        self.set_busy(True)
        self.progress.configure(maximum=len(sns), value=0)
        self.status.set(f"正在登录，待查询 {len(sns)} 个 SN…")
        self.write_log(f"准备查询 {len(sns)} 个 SN，日期 {begin} 至 {end}。")
        remember = self.remember.get()
        self.worker = threading.Thread(target=self.work, args=(username, password, remember, sns, begin, end),
                                       name="export-controller", daemon=False)
        self.worker.start()

    def work(self, username, password, remember, sns, begin, end):
        try:
            with FoxClient(self.config, self.cancel) as client:
                if not remember:
                    self.store.delete()
                client.login(username, password)
                self.events.put({"type": "log", "message": "登录成功。"})
                if remember:
                    try:
                        self.store.save(username, password)
                    except OSError:
                        self.events.put({"type": "log", "message": "本机账号密码保存失败，本次查询继续。"})
                directory, summary = export_alarms(client, sns, begin, end, self.directory,
                                                   self.cancel, self.events.put)
                self.events.put({"type": "done", "path": str(directory), "summary": summary})
        except QueryCancelled:
            self.events.put({"type": "error", "message": "已停止查询。"})
        except Exception as exc:
            reason = str(exc) if isinstance(exc, (ApiError, ValueError)) else f"操作失败（{type(exc).__name__}），请检查网络或目录写入权限。"
            self.events.put({"type": "error", "message": reason})

    def stop(self):
        self.cancel.set()
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止，等待已发出的请求结束并保存结果…")

    def poll(self):
        finished = False
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event["type"]
            if kind == "log":
                self.write_log(event["message"])
            elif kind == "output":
                self.last_output = Path(event["path"])
                self.open_button.configure(state="normal")
                self.write_log(f"结果文件夹：{event['path']}")
            elif kind == "progress":
                self.progress.configure(value=event["done"])
                if not self.cancel.is_set():
                    self.status.set(f"已查询 {event['done']} / {event['total']} 个 SN…")
            elif kind == "done":
                summary = event["summary"]
                label = "已停止" if summary["cancelled"] else ("查询完成" if summary["complete"] else "查询结束，请查看明细")
                text = (f"{label}：导出 {summary['exported_alarms']} 条，失败 {summary['failed_devices']} 个，"
                        f"未完整导出 {summary['incomplete_devices']} 个，未查询 {summary['not_queried_devices']} 个。")
                self.status.set(text)
                self.write_log(text)
                self.set_busy(False)
                finished = True
            elif kind == "error":
                self.status.set(event["message"])
                self.write_log(event["message"])
                self.set_busy(False)
                finished = True
        if self.closing and (finished or not (self.worker and self.worker.is_alive())):
            self.root.destroy()
            return
        self.root.after(100, self.poll)

    def open_output(self):
        if self.last_output and self.last_output.is_dir():
            try:
                os.startfile(str(self.last_output))
            except OSError:
                messagebox.showerror("打开失败", f"请手动打开：{self.last_output}", parent=self.root)

    def close(self):
        if self.worker and self.worker.is_alive():
            self.closing = True
            self.stop()
            self.status.set("正在保存结果，当前请求结束后自动关闭…")
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    try:
        AlarmApp(root)
    except (ValueError, OSError) as exc:
        root.withdraw()
        messagebox.showerror("启动失败", str(exc), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
