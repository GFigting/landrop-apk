#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAN Drop · 合并版启动器 (单 exe)
- 双击 exe: 弹出模式选择窗口 -> 中转版(推荐) / P2P 直传版
- 命令行: landrop.py http --port 9000   或   landrop.py p2p --password 123
- 选定后以托盘图标运行(无终端), 托盘菜单可打开页面 / 退出
"""

import sys
import os


def ask_mode():
    """弹出模式选择窗口; 无 GUI 环境时默认回中转版。"""
    try:
        import tkinter as tk
    except Exception:
        return "http"
    choice = {"m": None}
    root = tk.Tk()
    root.title("LAN Drop")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frm = tk.Frame(root, bg="#f5f7fa", padx=28, pady=20)
    frm.pack()
    tk.Label(frm, text="局域网文件互传", font=("Microsoft YaHei", 17, "bold"),
             bg="#f5f7fa", fg="#1f2933").pack()
    tk.Label(frm, text="请选择传输模式（启动后右下角托盘图标可退出）",
             font=("Microsoft YaHei", 10), bg="#f5f7fa", fg="#5f6b7a").pack(pady=(4, 14))

    def pick(m):
        choice["m"] = m
        root.destroy()

    tk.Button(frm, text="中转版（推荐）\n电脑/手机/平板/电视 打开网页即传\n含电视大屏 · DLNA 投屏 · 临时链接",
              font=("Microsoft YaHei", 11), bg="#185fa5", fg="white",
              activebackground="#124a82", activeforeground="white",
              relief="flat", cursor="hand2", justify="left",
              width=32, pady=8, command=lambda: pick("http")).pack(pady=4)
    tk.Button(frm, text="P2P 直传版\n设备之间点对点，文件不经过本机\n适合手机之间快速互传大文件",
              font=("Microsoft YaHei", 11), bg="#ffffff", fg="#185fa5",
              activebackground="#eef4fc", relief="flat", cursor="hand2",
              justify="left", width=32, pady=8, command=lambda: pick("p2p")).pack(pady=4)
    root.update_idletasks()
    root.geometry("+%d+%d" % ((root.winfo_screenwidth() - root.winfo_width()) // 2,
                              max(0, (root.winfo_screenheight() - root.winfo_height()) // 2 - 60)))
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return choice["m"]


def main():
    argv = sys.argv[1:]
    mode = None
    if argv and argv[0] in ("http", "p2p"):
        mode = argv[0]
        argv = argv[1:]
    else:
        mode = ask_mode()
    if not mode:
        return
    if mode == "p2p":
        import p2p_share
        p2p_share.run(argv)
    else:
        import lan_share
        lan_share.run(argv)


if __name__ == "__main__":
    main()
