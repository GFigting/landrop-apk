#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DLNA / UPnP 投屏模块 (纯标准库, 无第三方依赖)

作用: 在局域网内发现支持 AVTransport 的播放设备(智能电视/机顶盒/投影),
      并把一个 HTTP 媒体 URL 推送到该设备播放(充当 DMC 控制器角色)。

限制:
- 需要本机网络支持 UDP 多播(绝大多数局域网都支持; 部分公有云/隔离沙箱不支持, 此时优雅降级为"找不到设备")。
- 目标设备必须是一个 DLNA 渲染器(DMR)。老电视若系统无 DLNA 接收端, 仍无法投屏。
"""

import socket
import threading
import time
import html
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
import urllib.request

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
ST = "urn:schemas-upnp-org:service:AVTransport:1"

_renderers = []          # 已发现设备: [{"name","location","control"}]
_lock = threading.Lock()


def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _x(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def discover_once(timeout=4):
    """发一次 M-SEARCH, 收集 timeout 秒内的响应, 返回渲染器列表。"""
    locations = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        msg = "\r\n".join([
            "M-SEARCH * HTTP/1.1",
            "HOST: %s:%d" % (SSDP_ADDR, SSDP_PORT),
            'MAN: "ssdp:discover"',
            "MX: %d" % min(int(timeout), 3),
            "ST: %s" % ST,
            "", "",
        ]).encode("utf-8")
        try:
            sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
        except Exception:
            pass
        end = time.time() + timeout
        while True:
            remain = end - time.time()
            if remain <= 0:
                break
            sock.settimeout(remain)
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            except Exception:
                break
            txt = data.decode("utf-8", "ignore")
            loc = None
            for line in txt.splitlines():
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
            if loc:
                locations[loc] = True
        sock.close()
    except Exception:
        return []

    out = []
    for loc in locations:
        try:
            r = _describe(loc)
            if r:
                out.append(r)
        except Exception:
            pass
    return out


def _describe(location):
    try:
        req = urllib.request.Request(location, headers={"USER-AGENT": "LAN-Drop/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = resp.read()
    except Exception:
        return None
    try:
        root = ET.fromstring(data)
    except Exception:
        return None

    name = None
    for el in root.iter():
        if _local(el.tag) == "friendlyName" and el.text:
            name = el.text.strip()
            break

    for svc in root.iter():
        if _local(svc.tag) != "service":
            continue
        stype = None
        ctrl = None
        for c in svc:
            t = _local(c.tag)
            if t == "serviceType":
                stype = c.text or ""
            elif t == "controlURL":
                ctrl = (c.text or "").strip()
        if stype and "AVTransport" in stype and ctrl:
            if ctrl.startswith("/"):
                p = urlparse(location)
                ctrl = "%s://%s%s" % (p.scheme, p.netloc, ctrl)
            return {"name": name or location, "location": location, "control": ctrl}
    return None


def push(control_url, media_url, title, media_class):
    """把 media_url 推到指定渲染器并播放。成功返回 True。"""
    didl = (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="1" parentID="0" restricted="1">'
        '<dc:title>%s</dc:title>'
        '<upnp:class>%s</upnp:class>'
        '<res protocolInfo="*:*:*:*">%s</res>'
        '</item></DIDL-Lite>'
    ) % (_x(title), media_class, _x(media_url))

    set_uri = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        '<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID>'
        '<CurrentURI>%s</CurrentURI>'
        '<CurrentURIMetadata>%s</CurrentURIMetadata>'
        '</u:SetAVTransportURI></s:Body></s:Envelope>'
    ) % (_x(media_url), didl)

    if not _soap(control_url, "SetAVTransportURI", set_uri):
        return False

    play = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        '<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID><Speed>1</Speed>'
        '</u:Play></s:Body></s:Envelope>'
    )
    return _soap(control_url, "Play", play)


def _soap(url, action, body):
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#%s"' % action,
    }
    try:
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
        return True
    except Exception:
        return False


def _loop(interval):
    while True:
        try:
            rs = discover_once(timeout=4)
            with _lock:
                global _renderers
                _renderers = rs
        except Exception:
            pass
        time.sleep(interval)


def start_discovery(interval=20):
    """启动后台周期性发现线程(守护线程, 主进程退出即停)。"""
    t = threading.Thread(target=_loop, args=(interval,), daemon=True)
    t.start()


def get_renderers():
    with _lock:
        return list(_renderers)
