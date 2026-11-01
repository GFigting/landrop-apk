#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAN Drop · Android 主程序 (Kivy)
- 手机当主机: 点击启动服务, 页面显示局域网地址 + 大二维码
- 其他设备(电脑/平板/电视)浏览器扫码打开即可互传
- 服务端逻辑完全复用 lan_share.py (HTTP 中转版)
"""

import os
import socket
import socketserver
import threading

import qrcode

import lan_share
import dlna

from kivy.app import App
from kivy.core.text import LabelBase
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.widget import Widget
from kivy.core.window import Window

PORT = 8765
BASE = os.path.dirname(os.path.abspath(__file__))

# 注册中文字体(Kivy 默认 Roboto 无 CJK 字形)
try:
    LabelBase.register(name="CJK",
                       fn_regular=os.path.join(BASE, "fonts", "NotoSansSC-Regular.otf"))
except Exception:
    pass

GREEN = (0.10, 0.54, 0.24, 1)
RED = (0.64, 0.18, 0.18, 1)
BLUE = (0.09, 0.37, 0.65, 1)
GRAY = (0.35, 0.42, 0.50, 1)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _android_env():
    """返回 (activity, packageName); 非 Android 环境返回 (None, None)。"""
    try:
        from jnius import autoclass
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        return activity, activity.getPackageName()
    except Exception:
        return None, None


def get_shared_dir():
    """收到的文件保存目录: Android 用应用专属外部目录(免存储权限, 数据线/MTP 可见)。"""
    activity, pkg = _android_env()
    if pkg:
        try:
            Env = __import__("jnius", fromlist=["autoclass"]).autoclass("android.os.Environment")
            ext = Env.getExternalStorageDirectory().getAbsolutePath()
            return os.path.join(ext, "Android", "data", pkg, "files", "LANDrop")
        except Exception:
            pass
    return os.path.join(BASE, "LANDrop")


def acquire_multicast_lock():
    """获取 WiFi 组播锁, 否则 DLNA/SSDP 设备发现可能搜不到。"""
    activity, _ = _android_env()
    if not activity:
        return
    try:
        wifi = activity.getSystemService("wifi")
        lock = wifi.createMulticastLock("landrop")
        lock.setReferenceCounted(False)
        lock.acquire()
    except Exception:
        pass


class QrWidget(Widget):
    def __init__(self, **kw):
        super(QrWidget, self).__init__(**kw)
        self.matrix = None
        self.bind(pos=self.redraw, size=self.redraw)

    def set_text(self, text):
        qr = qrcode.QRCode(border=2)
        qr.add_data(text)
        qr.make(fit=True)
        self.matrix = qr.get_matrix()
        self.redraw()

    def clear_qr(self):
        self.matrix = None
        self.redraw()

    def redraw(self, *a):
        self.canvas.clear()
        if not self.matrix:
            return
        m = self.matrix
        n = len(m)
        cw = self.width / n
        ch = self.height / n
        with self.canvas:
            from kivy.graphics import Color, Rectangle
            Color(1, 1, 1, 1)
            Rectangle(pos=self.pos, size=self.size)
            Color(0, 0, 0, 1)
            for ry, row in enumerate(m):
                y = self.y + (n - 1 - ry) * ch
                for rx, v in enumerate(row):
                    if v:
                        Rectangle(pos=(self.x + rx * cw, y), size=(cw + 0.6, ch + 0.6))


class DropApp(App):
    title = "LAN Drop"

    def build(self):
        Window.clearcolor = (0.96, 0.97, 0.98, 1)
        self.server = None
        root = BoxLayout(orientation="vertical", padding=24, spacing=12)
        root.add_widget(Label(text="局域网文件互传", font_name="CJK", font_size="26sp",
                              bold=True, color=(0.12, 0.16, 0.20, 1),
                              size_hint_y=None, height=52))
        self.status = Label(text="服务未启动", font_name="CJK", font_size="16sp",
                            color=GRAY, size_hint_y=None, height=36)
        root.add_widget(self.status)
        self.url_lbl = Label(text="", font_name="CJK", font_size="20sp", bold=True,
                             color=BLUE, size_hint_y=None, height=44)
        root.add_widget(self.url_lbl)
        self.qr = QrWidget(size_hint_y=1)
        root.add_widget(self.qr)
        self.btn = Button(text="启动服务", font_name="CJK", font_size="18sp",
                          size_hint_y=None, height=68, background_normal="",
                          background_color=BLUE, color=(1, 1, 1, 1))
        self.btn.bind(on_press=self.toggle)
        root.add_widget(self.btn)
        root.add_widget(Label(
            text="其他设备浏览器扫码或输地址即可互传\n文件保存在应用数据目录 LANDrop 文件夹\n(电脑数据线 / 部分文件管理器可见, 也可在网页直接下载)",
            font_name="CJK", font_size="12sp", color=(0.55, 0.60, 0.66, 1),
            size_hint_y=None, height=64, halign="center"))
        return root

    def toggle(self, *a):
        if self.server:
            self.stop_server()
        else:
            self.start_server()

    def start_server(self):
        shared = get_shared_dir()
        try:
            os.makedirs(shared, exist_ok=True)
        except Exception:
            shared = os.path.join(BASE, "LANDrop")
            os.makedirs(shared, exist_ok=True)
        lan_share.SHARED = shared
        url = "http://%s:%d" % (lan_ip(), PORT)
        lan_share.LAN_URL = url
        socketserver.TCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("0.0.0.0", PORT), lan_share.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        acquire_multicast_lock()
        try:
            dlna.start_discovery()
        except Exception:
            pass
        self.status.text = "● 服务运行中 (中转版)"
        self.status.color = GREEN
        self.url_lbl.text = url
        self.qr.set_text(url)
        self.btn.text = "停止服务"
        self.btn.background_color = RED

    def stop_server(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.server = None
        self.status.text = "服务已停止"
        self.status.color = GRAY
        self.url_lbl.text = ""
        self.qr.clear_qr()
        self.btn.text = "启动服务"
        self.btn.background_color = BLUE


if __name__ == "__main__":
    DropApp().run()
