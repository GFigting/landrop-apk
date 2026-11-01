#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAN Drop · Android 主程序 (Kivy)
- 手机当主机: 点击启动服务, 页面显示局域网地址 + 二维码
- 其他设备(电脑/平板/电视)浏览器扫码打开即可互传
- 服务端逻辑完全复用 lan_share.py (HTTP 中转版)

移动端适配要点:
- 所有尺寸用 dp/sp (随屏幕密度缩放, 不同手机观感一致)
- 二维码固定正方形、居中、不超出屏幕
- 整页可滚动, 底部留安全边距(避开手势条)
- 多行文本绑定 text_size, 换行居中不裁切
"""

import os
import socket
import socketserver
import threading

import qrcode

import lan_share
import dlna
import updater

from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import LabelBase
from kivy.metrics import dp
from kivy.uix.anchorlayout import AnchorLayout
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window

PORT = 8765
BASE = os.path.dirname(os.path.abspath(__file__))

# 注册中文字体(Kivy 默认 Roboto 无 CJK 字形)
try:
    LabelBase.register(name="CJK",
                       fn_regular=os.path.join(BASE, "fonts", "NotoSansSC-Regular.otf"))
except Exception:
    pass

INK = (0.12, 0.16, 0.20, 1)
GREEN = (0.10, 0.54, 0.24, 1)
RED = (0.64, 0.18, 0.18, 1)
BLUE = (0.09, 0.37, 0.65, 1)
GRAY = (0.35, 0.42, 0.50, 1)
FAINT = (0.50, 0.55, 0.62, 1)


def _center_label(**kw):
    """多行居中 Label: 绑定 text_size 使 halign/valign 生效。"""
    lbl = Label(**kw)
    lbl.bind(size=lambda inst, v: setattr(inst, "text_size", inst.size))
    return lbl


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


def android_toast(text):
    """弹 Android 原生 Toast; 失败静默(桌面环境无此能力)。"""
    activity, _ = _android_env()
    if not activity:
        return False
    try:
        from jnius import autoclass, PythonJavaClass, java_method

        class _ToastTask(PythonJavaClass):
            __javainterfaces__ = ["java/lang/Runnable"]
            __javacontext__ = "app"

            def __init__(self, msg):
                super(_ToastTask, self).__init__()
                self.msg = msg

            @java_method("()V")
            def run(self):
                Toast = autoclass("android.widget.Toast")
                Context = autoclass("android.content.Context")
                Toast.makeText(activity, self.msg,
                               Toast.LENGTH_SHORT).show()

        activity.runOnUiThread(_ToastTask(text))
        return True
    except Exception:
        return False


def copy_to_clipboard(text):
    """复制文本到系统剪贴板 (Android 用 ClipboardManager)。"""
    activity, _ = _android_env()
    if not activity:
        return False
    try:
        from jnius import autoclass
        Context = autoclass("android.content.Context")
        ClipData = autoclass("android.content.ClipData")
        cm = activity.getSystemService(Context.CLIPBOARD_SERVICE)
        cm.setPrimaryClip(ClipData.newPlainText("landrop", text))
        return True
    except Exception:
        return False


class QrWidget(Widget):
    """二维码绘制: 单元格保持正方形, 整体在控件内居中。"""

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
        cell = min(self.width, self.height) / n
        total = cell * n
        ox = self.x + (self.width - total) / 2
        oy = self.y + (self.height - total) / 2
        with self.canvas:
            from kivy.graphics import Color, Rectangle
            Color(1, 1, 1, 1)
            Rectangle(pos=(ox - cell * 2, oy - cell * 2),
                      size=(total + cell * 4, total + cell * 4))
            Color(0, 0, 0, 1)
            for ry, row in enumerate(m):
                y = oy + (n - 1 - ry) * cell
                for rx, v in enumerate(row):
                    if v:
                        Rectangle(pos=(ox + rx * cell, y),
                                  size=(cell + 0.6, cell + 0.6))


class DropApp(App):
    title = "LAN Drop"

    def build(self):
        Window.clearcolor = (0.96, 0.97, 0.98, 1)
        self.server = None
        self._last_count = -1

        root = ScrollView(do_scroll_x=False, bar_width=dp(3))
        page = BoxLayout(orientation="vertical", padding=[dp(20), dp(14), dp(20), dp(24)],
                         spacing=dp(10), size_hint_y=None)
        page.bind(minimum_height=page.setter("height"))
        root.add_widget(page)

        # ── 更新通知横幅 (默认隐藏, 检查发现新版本时显示) ──
        from kivy.graphics import Color, Rectangle
        self._remote = None
        self.update_bar = BoxLayout(orientation="horizontal", size_hint_y=None,
                                    height=0, opacity=0, padding=[dp(12), dp(6)],
                                    spacing=dp(10))
        self.update_bar.canvas.before.add(Color(0.09, 0.37, 0.65, 1))
        self.update_bar.canvas.before.add(Rectangle(
            pos=self.update_bar.pos, size=self.update_bar.size))
        self.update_bar.bind(
            pos=lambda i, v: setattr(i.canvas.before.children[-1], "pos", v),
            size=lambda i, v: setattr(i.canvas.before.children[-1], "size", v))
        self.update_lbl = _center_label(text="", font_name="CJK", font_size="13sp",
                                        color=(1, 1, 1, 1), size_hint=(1, None),
                                        height=dp(36))
        self.update_bar.add_widget(self.update_lbl)
        btn_dl = Button(text="下载", font_name="CJK", font_size="13sp",
                        size_hint=(None, None), size=(dp(64), dp(36)),
                        background_normal="", background_color=(1, 1, 1, 1),
                        color=(0.09, 0.37, 0.65, 1))
        btn_dl.bind(on_press=self.open_update)
        self.update_bar.add_widget(btn_dl)
        btn_x = Button(text="✕", font_name="CJK", font_size="14sp",
                       size_hint=(None, None), size=(dp(36), dp(36)),
                       background_normal="", background_color=(0, 0, 0, 0),
                       color=(1, 1, 1, 1))
        btn_x.bind(on_press=self.hide_update)
        self.update_bar.add_widget(btn_x)
        page.add_widget(self.update_bar)

        # 启动后后台检查更新 (仅通知 + 一键下载, 不自动安装)
        try:
            updater.check_async(self._on_update_check, BASE)
        except Exception:
            pass

        # ── 标题 ──
        page.add_widget(Label(text="局域网文件互传", font_name="CJK", font_size="24sp",
                              bold=True, color=INK,
                              size_hint_y=None, height=dp(44)))
        self.status = Label(text="服务未启动", font_name="CJK", font_size="15sp",
                            color=GRAY, size_hint_y=None, height=dp(28))
        page.add_widget(self.status)

        # ── 三步引导条 (未启动时也可见, 先教再用) ──
        page.add_widget(_center_label(
            text="① 连接同一 WiFi    ② 扫码 / 输网址    ③ 互传文件",
            font_name="CJK", font_size="13sp", color=FAINT,
            size_hint_y=None, height=dp(24)))

        # ── 二维码 (正方形, 居中, 固定大小不撑爆屏幕) ──
        qr_anchor = AnchorLayout(size_hint_y=None, height=dp(296))
        self.qr = QrWidget(size_hint=(None, None), size=(dp(272), dp(272)))
        qr_anchor.add_widget(self.qr)
        page.add_widget(qr_anchor)

        # ── 地址 + 复制按钮 ──
        self.url_lbl = _center_label(text="—", font_name="CJK", font_size="18sp",
                                     bold=True, color=BLUE,
                                     size_hint_y=None, height=dp(32))
        page.add_widget(self.url_lbl)
        self.copy_btn = Button(text="复制地址", font_name="CJK", font_size="14sp",
                               size_hint_y=None, height=dp(42),
                               background_normal="", background_color=(0.88, 0.91, 0.95, 1),
                               color=BLUE)
        self.copy_btn.bind(on_press=self.copy_url)
        page.add_widget(self.copy_btn)

        # ── 主按钮 ──
        self.btn = Button(text="启动服务", font_name="CJK", font_size="17sp",
                          size_hint_y=None, height=dp(54), background_normal="",
                          background_color=BLUE, color=(1, 1, 1, 1))
        self.btn.bind(on_press=self.toggle)
        page.add_widget(self.btn)

        # ── 收文件统计 (服务运行时实时刷新) ──
        self.files_lbl = _center_label(text="", font_name="CJK", font_size="13sp",
                                       color=GREEN, size_hint_y=None, height=dp(22))
        page.add_widget(self.files_lbl)

        # ── 底部说明 ──
        page.add_widget(_center_label(
            text="文件保存在 LANDrop 文件夹\n(数据线连电脑 / 部分文件管理器可见,\n也可在网页里直接下载)",
            font_name="CJK", font_size="12sp", color=FAINT,
            size_hint_y=None, height=dp(60)))
        page.add_widget(Widget(size_hint_y=None, height=dp(16)))  # 底部安全边距

        Clock.schedule_interval(self.refresh_files, 2)
        return root

    # ── 交互反馈 ──

    def copy_url(self, *a):
        url = self.url_lbl.text
        if not url or url == "—":
            return
        if copy_to_clipboard(url):
            android_toast("地址已复制")
            self.copy_btn.text = "已复制 ✓"
            Clock.schedule_once(self._restore_copy_btn, 1.2)

    def _restore_copy_btn(self, *a):
        self.copy_btn.text = "复制地址"

    # ── 更新通知 ──

    def _on_update_check(self, remote):
        """后台线程回调, 切回主线程刷新 UI。"""
        Clock.schedule_once(lambda dt: self.show_update(remote), 0)

    def show_update(self, remote):
        if not remote:
            return
        self._remote = remote
        self.update_lbl.text = "发现新版本 v%s" % remote.get("version", "")
        self.update_bar.height = dp(48)
        self.update_bar.opacity = 1

    def hide_update(self, *a):
        self.update_bar.height = 0
        self.update_bar.opacity = 0

    def open_update(self, *a):
        url = (self._remote or {}).get("download_page") or \
              "https://github.com/GFigting/landrop-apk/releases"
        self._open_url(url)

    def _open_url(self, url):
        """优先用 Android 系统浏览器打开; 桌面环境回退 webbrowser。"""
        activity, _ = _android_env()
        if activity:
            try:
                from jnius import autoclass
                Intent = autoclass("android.content.Intent")
                Uri = autoclass("android.net.Uri")
                intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
                activity.startActivity(intent)
                return
            except Exception:
                pass
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def refresh_files(self, dt):
        """每 2 秒刷新已收文件数。"""
        if not self.server:
            if self.files_lbl.text:
                self.files_lbl.text = ""
                self._last_count = -1
            return
        try:
            n = len([f for f in os.listdir(lan_share.SHARED)
                     if not f.startswith(".")])
        except Exception:
            return
        if n != self._last_count:
            self._last_count = n
            self.files_lbl.text = "已收到 %d 个文件" % n if n else "暂未收到文件"

    # ── 服务开关 ──

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
        self._last_count = -1
        self.status.text = "● 服务运行中 (中转版)"
        self.status.color = GREEN
        self.url_lbl.text = url
        self.qr.set_text(url)
        self.btn.text = "停止服务"
        self.btn.background_color = RED
        android_toast("服务已启动, 其他设备扫码即可连接")

    def stop_server(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.server = None
        self.status.text = "服务已停止"
        self.status.color = GRAY
        self.url_lbl.text = "—"
        self.qr.clear_qr()
        self.files_lbl.text = ""
        self._last_count = -1
        self.btn.text = "启动服务"
        self.btn.background_color = BLUE


if __name__ == "__main__":
    DropApp().run()
