#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAN Drop 更新检查器 (单一信息源: GitHub Pages 上的 version.json)
- 远程清单地址国内直连可达 (比 api.github.com 稳定)
- 仅通知 + 一键下载, 绝不静默自装
- 同一安装目录每日节流, 避免重复请求
"""
import json
import os
import threading
import time
import urllib.request

# 当前版本 (打包时由 CI 注入为发布版本; 本地开发可用此占位值)
CURRENT_VERSION = "2026.11.01"

# 远端版本清单 (与 APK / EXE 同一 GitHub Pages)
VERSION_URL = "https://gfigting.github.io/landrop-apk/version.json"

# 同一安装目录 12 小时内最多查一次
_CHECK_INTERVAL = 12 * 3600


def _state_file(app_dir):
    if not app_dir:
        return None
    return os.path.join(app_dir, ".landrop_update_check")


def _throttled(app_dir):
    p = _state_file(app_dir)
    if not p:
        return False
    try:
        return (time.time() - os.path.getmtime(p)) < _CHECK_INTERVAL
    except OSError:
        return False


def _touch(app_dir):
    p = _state_file(app_dir)
    if p:
        try:
            with open(p, "w") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass


def parse_ver(v):
    """'v2026.11.01' / '2026.11.01' -> (2026, 11, 1); 失败返回 (0,)。"""
    if not v:
        return (0,)
    try:
        return tuple(int(x) for x in v.lstrip("vV").split(".") if x.strip() != "")
    except Exception:
        return (0,)


def fetch_remote():
    """拉取远端 version.json, 失败返回 None。"""
    try:
        req = urllib.request.Request(
            VERSION_URL, headers={"User-Agent": "LANDrop-Updater"})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def check(app_dir=None):
    """返回远端信息 dict 或 None (无更新 / 失败 / 节流内)。"""
    if app_dir and _throttled(app_dir):
        return None
    remote = fetch_remote()
    if app_dir:
        _touch(app_dir)
    if not remote:
        return None
    rv = remote.get("version", "")
    if not rv or parse_ver(rv) <= parse_ver(CURRENT_VERSION):
        return None
    remote.setdefault(
        "download_page",
        "https://github.com/GFigting/landrop-apk/releases")
    return remote


def check_async(callback, app_dir=None):
    """后台线程检查, 结果回调 callback(remote_or_None), 不阻塞调用方。"""
    def _run():
        try:
            res = check(app_dir)
        except Exception:
            res = None
        try:
            callback(res)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()
