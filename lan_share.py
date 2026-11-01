#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网文件互传小网页 (LAN Drop)
- 仅依赖 Python 标准库，无需 pip install
- 一台机器运行: python lan_share.py
- 同 WiFi / 同交换机下的其他设备, 浏览器打开 http(s)://<本机IP>:<端口> 即可
- 支持: 拖拽/点击上传, 进度条, 文件列表, 图片预览, 下载, 删除
- 多模态内容(文字/图片/音频/视频/任意文件)底层都是字节流, 统一按文件处理
- 可选增强: 二维码扫码打开 / 访问密码 / 临时链接 / 单文件单次有效下载链接 / HTTPS(自签证书)
- 文件存放目录可自定义: --shared D:/MyShare 或环境变量 LAN_SHARED (默认程序目录下的 shared/)

防火墙提示(Windows):
  其他设备连不上时, 需在"Windows  Defender 防火墙 -> 允许应用"里放行 python,
  或临时关闭专用网络防火墙做测试。
"""

import http.server
import socketserver
import os
import json
import socket
import shutil
import urllib.parse
import argparse
import threading
import time
import hmac
import hashlib
import secrets
import ssl
import subprocess
import sys
import webbrowser
import dlna

PORT = 8765  # 默认端口(避开 8000 常被 C-Lodop/AirPlay 占用的情况)
# 打包成 exe(PyInstaller)后:
#   APP_DIR = exe 所在目录(可写, 存文件/证书)
#   RES_DIR = 解压出来的资源目录(只读, 放 qrcode.min.js)
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = RES_DIR = os.path.dirname(os.path.abspath(__file__))
# 上传的文件存放目录; 可被命令行 --shared 或环境变量 LAN_SHARED 覆盖(见 __main__)
SHARED = os.path.join(APP_DIR, "shared")


def lan_ip():
    """取本机在局域网中的 IP (只读路由表, 不真正发包, 离线也能用)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


LAN_URL = "http://%s:%d" % (lan_ip(), PORT)

# 二维码旁提示文案(普通模式 / 电视大屏模式)
NORMAL_QRNOTE = '''    <div style="font-size:13px;color:#5f6b7a;line-height:1.7;">
      <div style="font-weight:500;color:#3a4656;margin-bottom:4px;">手机扫码打开</div>
      用相机 / 扫码 App 扫左侧二维码，<br>即可在手机浏览器打开上传页，<br>免手动输入 IP。
    </div>'''
TV_QRNOTE = '''    <div style="font-size:15px;color:#cdd6e0;line-height:1.9;">
      <div style="font-weight:600;color:#9ec5ff;margin-bottom:6px;font-size:17px;">📺 推到大屏</div>
      用手机扫此码，选照片 / 视频发送，<br>自动在大屏显示。
    </div>'''

# ---------------- 访问保护 (密码 + 临时链接) ----------------
PASSWORD = ""            # 非空时启用密码保护, 由启动参数 --password 或环境变量 LAN_PASS 设置
SECRET = secrets.token_hex(16)
SESSIONS = {}            # sid -> 过期时间戳
LOCK = threading.Lock()
SESSION_MINS = 8 * 60    # 登录会话有效期(分钟)
TEMP_MINS = 30          # 临时链接有效期(分钟)


def _new_session(mins):
    sid = secrets.token_hex(16)
    with LOCK:
        SESSIONS[sid] = time.time() + mins * 60
    return sid


def _valid_session(sid):
    if not sid:
        return False
    with LOCK:
        exp = SESSIONS.get(sid)
        if not exp:
            return False
        if exp < time.time():
            SESSIONS.pop(sid, None)
            return False
        return True


def _make_temp_token(mins):
    exp = int(time.time()) + mins * 60
    raw = b"tmp:" + str(exp).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()[:32]
    return "%d.%s" % (exp, sig)


def _valid_temp_token(token):
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except Exception:
        return False
    if exp < time.time():
        return False
    raw = b"tmp:" + str(exp).encode()
    expect = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expect, sig)


# ---------------- 单文件 · 单次有效下载链接 ----------------
# token -> (文件名, 过期时间戳); 取用一次后即从字典删除(实现「单次有效」)
ONE_TOKENS = {}
ONE_MINS = 60  # 单次链接最长保留时间(分钟), 到期未用也失效


def _make_single_token(name, mins=ONE_MINS):
    tok = secrets.token_urlsafe(16)
    with LOCK:
        ONE_TOKENS[tok] = (name, time.time() + mins * 60)
    return tok


def _consume_single_token(tok):
    """取出并删除 token; 不存在/已过期返回 None(实现一次性)"""
    with LOCK:
        v = ONE_TOKENS.pop(tok, None)
    if not v:
        return None
    name, exp = v
    if exp < time.time():
        return None
    return name


def _run_tray(httpd, local_url, lan_url):
    """打包成 exe 后以托盘图标方式运行: 无终端窗口, 双击托盘打开页面, 菜单可退出。"""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        # 依赖缺失时退化为纯后台运行(Ctrl+C 不可用, 需任务管理器结束)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        while True:
            time.sleep(3600)
    # 画一个简单的"文件/传输"风格图标
    img = Image.new("RGB", (64, 64), (24, 95, 165))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(255, 255, 255))
    d.rectangle([14, 16, 50, 28], fill=(24, 95, 165))
    d.rectangle([14, 34, 42, 40], fill=(120, 170, 220))
    d.rectangle([14, 46, 34, 51], fill=(120, 170, 220))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def on_open(icon, item):
        webbrowser.open(local_url)

    def on_quit(icon, item):
        threading.Thread(target=httpd.shutdown, daemon=True).start()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开传输页面", on_open, default=True),
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon(
        "LANDrop", img,
        "LAN Drop 运行中\n本机: %s\n其他设备: %s" % (local_url, lan_url),
        menu)
    try:
        # Windows 气泡通知最长 64 字符, 且失败不能影响服务
        icon.notify("已启动: %s" % local_url)
    except Exception:
        pass
    icon.run()


def _gen_self_signed(cert_path):
    """用系统 openssl 生成自签证书(证书+私钥合并到 cert_path)。成功返回 True。"""
    openssl = shutil.which("openssl")
    if not openssl:
        return False
    d = os.path.dirname(os.path.abspath(cert_path))
    c = os.path.join(d, "_cert_tmp.pem")
    k = os.path.join(d, "_key_tmp.pem")
    try:
        subprocess.run(
            [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", k, "-out", c, "-days", "365", "-subj", "/CN=localhost"],
            check=True, capture_output=True, timeout=60)
        with open(cert_path, "wb") as out:
            out.write(open(c, "rb").read())
            out.write(open(k, "rb").read())
        return True
    except Exception:
        return False
    finally:
        for t in (c, k):
            try:
                os.remove(t)
            except OSError:
                pass


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📤</text></svg>">
<meta name="theme-color" content="#185fa5">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="LAN Drop">
<title>局域网文件互传</title>
<script src="/qrcode.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f5f7fa; color: #1f2933; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; }
  header h1 { font-size: 20px; margin: 0 0 4px; }
  .url { background: #e8f0fe; color: #185fa5; border-radius: 8px; padding: 10px 14px;
          font-size: 14px; word-break: break-all; margin: 12px 0; }
  .url b { color: #0c447c; }
  #drop { border: 2px dashed #b4c6e0; border-radius: 12px; background: #fff;
          padding: 32px 16px; text-align: center; color: #5f6b7a; cursor: pointer;
          transition: .15s; }
  #drop.hover { border-color: #185fa5; background: #f0f6ff; color: #185fa5; }
  #drop .big { font-size: 15px; font-weight: 500; }
  #file { display: none; }
  .progress { margin: 14px 0 0; }
  .bar { height: 8px; background: #e3e8ef; border-radius: 6px; overflow: hidden; }
  .bar > i { display: block; height: 100%; width: 0; background: #185fa5; transition: width .1s; }
  .plist { margin-top: 24px; }
  .plist h2 { font-size: 15px; margin: 0 0 10px; color: #3a4656; }
  .item { display: flex; align-items: center; gap: 12px; background: #fff; border: .5px solid #e3e8ef;
          border-radius: 10px; padding: 10px 12px; margin-bottom: 8px; }
  .item .ic { width: 40px; height: 40px; border-radius: 8px; background: #eef2f7; display: flex;
              align-items: center; justify-content: center; font-size: 18px; flex: 0 0 auto; }
  .item .meta { flex: 1 1 auto; min-width: 0; }
  .item .nm { font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .item .sz { font-size: 12px; color: #8a94a3; }
  .item a, .item button { font-size: 13px; text-decoration: none; color: #185fa5; background: none;
          border: .5px solid #b4c6e0; border-radius: 6px; padding: 5px 10px; cursor: pointer; }
  .item button.del { color: #a32d2d; border-color: #f0c0c0; }
  .empty { color: #9aa4b2; font-size: 13px; padding: 8px 0; }
  footer { margin-top: 28px; font-size: 12px; color: #9aa4b2; line-height: 1.7; }
  footer code { background: #eef2f7; padding: 1px 5px; border-radius: 4px; }
  /* 三步引导 */
  .guide { display: flex; gap: 10px; margin: 16px 0 2px; }
  .guide .g { flex: 1; background: #fff; border: .5px solid #e3e8ef; border-radius: 10px;
              padding: 10px 12px; font-size: 12px; color: #5f6b7a; line-height: 1.7; }
  .guide .g b { color: #185fa5; font-size: 14px; margin-right: 2px; }
  .plist h2 span { font-size: 12px; font-weight: 400; color: #8a94a3; margin-left: 6px; }
  body.tv .guide { display: none; }
  @media (max-width: 600px) {
    header h1 { font-size: 22px; }
    #drop { padding: 44px 16px; }
    #drop .big { font-size: 16px; }
    .item a, .item button { padding: 9px 15px; font-size: 14px; }
    .guide { flex-direction: column; gap: 6px; }
  }
  /* 电视 / 大屏展示模式 */
  body.tv { background: #0e1116; color: #e8edf3; }
  body.tv .wrap { max-width: 1120px; }
  body.tv header h1 { font-size: 30px; }
  body.tv #drop { display: none; }
  body.tv .url { background: #16202c; color: #9ec5ff; font-size: 18px; border-radius: 10px; }
  body.tv .url b { color: #cfe3ff; }
  body.tv .url span { color: #6b7785; }
  body.tv #qr { border-color: #28323f; background: #11161d; min-width: 200px; min-height: 200px; }
  body.tv .plist h2 { font-size: 22px; color: #cdd6e0; }
  body.tv .item { background: #161c24; border-color: #28323f; padding: 14px 16px; }
  body.tv .item .nm { font-size: 18px; }
  body.tv .item .sz { font-size: 14px; }
  body.tv .item a, body.tv .item button { font-size: 16px; padding: 8px 14px; }
  body.tv .thumb { width: 110px; height: 110px; object-fit: cover; border-radius: 10px; cursor: pointer;
                   background: #11161d; flex: 0 0 auto; }
  body.tv footer { color: #6b7785; }
  #lb { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.94); z-index: 50;
        align-items: center; justify-content: center; flex-direction: column; }
  #lbcontent img, #lbcontent video { max-width: 94vw; max-height: 88vh; border-radius: 8px; }
</style>
</head>
<body class="__TVCLASS__">
<div class="wrap">
  <header>
    <h1>局域网文件互传</h1>
    <div>__HEADNOTE__</div>
  </header>

  <div class="url">本机地址：<b id="myurl">__LAN_URL__</b> <span style="color:#8a94a3">(其他设备用浏览器打开)</span></div>
  <div style="margin-top:14px;display:flex;gap:14px;align-items:flex-start;">
    <div id="qr" style="background:#fff;border:.5px solid #e3e8ef;border-radius:10px;padding:8px;line-height:0;min-width:148px;min-height:148px;"></div>
    __QRNOTE__
  </div>

  <div class="guide">
    <div class="g"><b>①</b> 本机点下方区域<br>选择或拖入文件</div>
    <div class="g"><b>②</b> 其他设备扫上方二维码<br>（或输入地址）打开本页</div>
    <div class="g"><b>③</b> 双方传的文件都在下面<br>点「下载」保存 / 「预览」查看</div>
  </div>

  <span id="myauth" style="display:none">__AUTH__</span>
  <button id="tempBtn" onclick="makeTemp()" style="font-size:13px;color:#185fa5;background:#fff;border:.5px solid #b4c6e0;border-radius:6px;padding:5px 12px;cursor:pointer;margin-top:12px;display:none;">生成临时链接（30分钟有效）</button>
  <a href="/dlna" style="font-size:13px;color:#185fa5;background:#fff;border:.5px solid #b4c6e0;border-radius:6px;padding:5px 12px;cursor:pointer;margin-top:12px;margin-left:8px;text-decoration:none;display:inline-block;">📺 投屏到电视 (DLNA)</a>

  <div id="drop">
    <div class="big">点击选择文件，或把文件拖到这里</div>
    <div style="font-size:12px;margin-top:6px;">支持多文件 · 图片/视频/音频/文档均可</div>
    <input id="file" type="file" multiple>
  </div>
  <div class="progress" id="prog" style="display:none">
    <div class="bar"><i id="bar"></i></div>
    <div style="font-size:12px;color:#8a94a3;margin-top:6px;" id="ptxt"></div>
  </div>

  <div class="plist">
    <h2>已接收文件 <span id="fcount"></span></h2>
    <div id="list"></div>
  </div>

  <footer>
    原理：服务运行在 <code>0.0.0.0:__PORT__</code>，所有设备通过内网 IP 直连，数据不经过公网。<br>
    文件保存在 <code>__SHARED__</code> 文件夹。
  </footer>
</div>

<div id="lb" onclick="if(event.target===this)closeLb()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.94);z-index:50;align-items:center;justify-content:center;flex-direction:column;">
  <div id="lbcontent" style="max-width:94vw;max-height:88vh;"></div>
  <button onclick="closeLb()" style="position:absolute;top:18px;right:24px;font-size:30px;color:#fff;background:none;border:none;cursor:pointer;">✕</button>
</div>

<script>
const API = "/api/files";
const ICONS = { img: "🖼️", vid: "🎬", aud: "🎵", doc: "📄", zip: "🗜️", bin: "📦" };
function iconOf(name){
  const e = name.split(".").pop().toLowerCase();
  if(["png","jpg","jpeg","gif","webp","bmp","svg"].includes(e)) return ICONS.img;
  if(["mp4","mov","webm","mkv","avi"].includes(e)) return ICONS.vid;
  if(["mp3","wav","ogg","m4a","flac"].includes(e)) return ICONS.aud;
  if(["pdf","doc","docx","txt","md","xls","xlsx","ppt","pptx"].includes(e)) return ICONS.doc;
  if(["zip","rar","7z","tar","gz"].includes(e)) return ICONS.zip;
  return ICONS.bin;
}
function fmt(n){
  if(n < 1024) return n + " B";
  if(n < 1048576) return (n/1024).toFixed(1) + " KB";
  if(n < 1073741824) return (n/1048576).toFixed(1) + " MB";
  return (n/1073741824).toFixed(2) + " GB";
}
const drop = document.getElementById("drop");
const input = document.getElementById("file");
drop.onclick = () => input.click();
input.onchange = () => { for(const f of input.files) upload(f); input.value=""; };
["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("hover"); }));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("hover"); }));
drop.addEventListener("drop", e => { for(const f of e.dataTransfer.files) upload(f); });

const prog = document.getElementById("prog");
const bar = document.getElementById("bar");
const ptxt = document.getElementById("ptxt");
function upload(file){
  prog.style.display = "block";
  bar.style.width = "0%"; ptxt.textContent = "上传中：" + file.name;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/upload?name=" + encodeURIComponent(file.name));
  xhr.upload.onprogress = e => {
    if(e.lengthComputable){ const p = e.loaded/e.total; bar.style.width = (p*100).toFixed(0)+"%"; }
  };
  xhr.onload = () => { prog.style.display="none"; refresh(); };
  xhr.onerror = () => { ptxt.textContent = "上传失败，检查连接"; };
  xhr.send(file);
}

const TV = document.body.classList.contains("tv");
let seen = new Set();
let lbOpen = false;

async function refresh(){
  const r = await fetch(API); const items = await r.json();
  const list = document.getElementById("list");
  const fc = document.getElementById("fcount");
  if(!items.length){ fc.textContent = ""; list.innerHTML = '<div class="empty">还没有文件 —— 其他设备扫码上传，或在本机下方点选 / 拖入文件。</div>'; seen = new Set(); return; }
  const total = items.reduce((s,it)=>s+it.size,0);
  fc.textContent = "· " + items.length + " 个 · " + fmt(total);
  list.innerHTML = "";
  for(const it of items){
    const ext = it.name.split(".").pop().toLowerCase();
    const isImg = ["png","jpg","jpeg","gif","webp","bmp","svg"].includes(ext);
    const isVid = ["mp4","mov","webm","mkv","avi"].includes(ext);
    const isMedia = isImg || isVid;
    const row = document.createElement("div"); row.className = "item";
    if(TV && isImg){
      row.innerHTML = '<img class="thumb" src="/download/'+encodeURIComponent(it.name)+'">'
        + '<div class="meta"><div class="nm">'+it.name+'</div><div class="sz">'+fmt(it.size)+'</div></div>'
        + '<button class="del" data-n="'+it.name+'">删除</button>';
      row.querySelector(".thumb").onclick = () => openPreview(it.name, "img");
    } else if(TV && isVid){
      row.innerHTML = '<div class="ic">🎬</div>'
        + '<div class="meta"><div class="nm">'+it.name+'</div><div class="sz">'+fmt(it.size)+'</div></div>'
        + '<button class="play" data-n="'+it.name+'">播放</button>'
        + '<button class="del" data-n="'+it.name+'">删除</button>';
      row.querySelector(".play").onclick = () => openPreview(it.name, "vid");
    } else {
      row.innerHTML =
        '<div class="ic">'+iconOf(it.name)+'</div>' +
        '<div class="meta"><div class="nm">'+it.name+'</div><div class="sz">'+fmt(it.size)+'</div></div>' +
        '<a href="/download/'+encodeURIComponent(it.name)+'" download>下载</a>' +
        (isMedia ? '<button class="prev" data-n="'+it.name+'" data-t="'+(isImg?"img":"vid")+'">预览</button>' : '') +
        '<button class="shr" data-n="'+it.name+'">分享</button>' +
        '<button class="del" data-n="'+it.name+'">删除</button>';
      const pv = row.querySelector(".prev");
      if(pv) pv.onclick = () => openPreview(it.name, pv.dataset.t);
    }
    row.querySelector(".del").onclick = async () => { await fetch("/api/files/"+encodeURIComponent(it.name), {method:"DELETE"}); refresh(); };
    const sh = row.querySelector(".shr");
    if(sh) sh.onclick = async () => { share(it.name); };
    list.appendChild(row);
  }
  // 电视模式: 出现新媒体时自动大屏预览
  if(TV){
    const medias = items.filter(it => {
      const e = it.name.split(".").pop().toLowerCase();
      return ["png","jpg","jpeg","gif","webp","bmp","svg","mp4","mov","webm","mkv","avi"].includes(e);
    });
    const fresh = medias.filter(it => !seen.has(it.name));
    if(fresh.length && !lbOpen){
      const f = fresh[fresh.length-1];
      const e = f.name.split(".").pop().toLowerCase();
      openPreview(f.name, ["mp4","mov","webm","mkv","avi"].includes(e) ? "vid" : "img");
    }
  }
  items.forEach(it => seen.add(it.name));
}
function openPreview(name, type){
  const c = document.getElementById("lbcontent");
  if(type === "vid"){
    c.innerHTML = '<video src="/download/'+encodeURIComponent(name)+'" controls autoplay style="max-width:94vw;max-height:88vh;"></video>';
  } else {
    c.innerHTML = '<img src="/download/'+encodeURIComponent(name)+'" style="max-width:94vw;max-height:88vh;">';
  }
  document.getElementById("lb").style.display = "flex";
  lbOpen = true;
}
function closeLb(){
  document.getElementById("lb").style.display = "none";
  document.getElementById("lbcontent").innerHTML = "";
  lbOpen = false;
}
function makeQR(text){
  for(let t=1;t<=12;t++){
    try{ const qr=qrcode(t,"M"); qr.addData(text); qr.make();
      return qr.createSvgTag({cellSize:5, margin:2}); }catch(e){}
  }
  return "二维码生成失败";
}
document.getElementById("qr").innerHTML = makeQR(document.getElementById("myurl").textContent.trim());
if(document.getElementById("myauth").textContent.trim() !== "1"){ document.getElementById("tempBtn").style.display = "none"; }
async function makeTemp(){
  try{
    const r = await fetch("/api/templink?min=30", {method:"POST"});
    const j = await r.json();
    try{ await navigator.clipboard.writeText(j.url); }catch(e){}
    alert("临时链接已生成（30分钟有效），已尝试复制到剪贴板：\n\n" + j.url);
  }catch(e){ alert("生成失败，请确认已登录"); }
}
async function share(name){
  try{
    const r = await fetch("/api/share?name=" + encodeURIComponent(name), {method:"POST"});
    const j = await r.json();
    try{ await navigator.clipboard.writeText(j.url); }catch(e){}
    alert("单次下载链接已生成（打开一次后自动失效），已复制到剪贴板：\n\n" + j.url);
  }catch(e){ alert("生成失败"); }
}
refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔒</text></svg>">
<meta name="theme-color" content="#185fa5">
<title>访问验证</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
         background:#f5f7fa; color:#1f2933; }
  .box { max-width:340px; margin:12vh auto; background:#fff; border:.5px solid #e3e8ef; border-radius:12px;
         padding:28px 24px; text-align:center; }
  .box h1 { font-size:18px; margin:0 0 16px; }
  input { width:100%; padding:10px 12px; font-size:14px; border:.5px solid #b4c6e0; border-radius:8px; outline:none; }
  button { margin-top:14px; width:100%; padding:10px; font-size:14px; color:#fff; background:#185fa5;
           border:none; border-radius:8px; cursor:pointer; }
  .err { color:#a32d2d; font-size:13px; margin-top:10px; }
  .tip { font-size:12px; color:#9aa4b2; margin-top:14px; }
</style>
</head>
<body>
<div class="box">
  <h1>需要密码</h1>
  <form method="post" action="/login">
    <input type="password" name="password" placeholder="请输入访问密码" autofocus>
    <button type="submit">进入</button>
  </form>
  __ERR__
  <div class="tip">局域网文件互传 · 受密码保护</div>
</div>
</body>
</html>
"""

DLNA_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📤</text></svg>">
<meta name="theme-color" content="#185fa5">
<title>投屏到电视 (DLNA)</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
         background:#f5f7fa; color:#1f2933; }
  .wrap { max-width:760px; margin:0 auto; padding:24px 16px 48px; }
  header h1 { font-size:20px; margin:0 0 4px; }
  .note { font-size:13px; color:#5f6b7a; margin:6px 0 18px; line-height:1.7; }
  h2 { font-size:15px; color:#3a4656; margin:22px 0 10px; }
  .tv { background:#fff; border:.5px solid #e3e8ef; border-radius:10px; padding:12px 14px;
        margin-bottom:8px; display:flex; align-items:center; gap:12px; }
  .tv input { margin:0; }
  .tv .nm { font-size:14px; }
  .empty { color:#9aa4b2; font-size:13px; padding:6px 0; }
  .file { display:flex; align-items:center; gap:12px; background:#fff; border:.5px solid #e3e8ef;
          border-radius:10px; padding:10px 12px; margin-bottom:8px; }
  .file .nm { flex:1 1 auto; font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .file .warn { font-size:12px; color:#a32d2d; }
  button.push { font-size:13px; color:#fff; background:#185fa5; border:none; border-radius:6px;
                padding:6px 14px; cursor:pointer; }
  button.push:disabled { background:#b4c6e0; cursor:not-allowed; }
  a.back { font-size:13px; color:#185fa5; text-decoration:none; }
  footer { margin-top:28px; font-size:12px; color:#9aa4b2; line-height:1.7; }
  code { background:#eef2f7; padding:1px 5px; border-radius:4px; }
</style>
</head>
<body>
<div class="wrap">
  <header><h1>投屏到电视 (DLNA)</h1></header>
  <div class="note">
    选择一台已发现的电视 / 机顶盒，再点文件的「推到电视」即可把照片或视频投上去播放。<br>
    若列表为空：确认电视与电脑在同一 WiFi，且电视的「投屏 / 多屏互动 / DLNA 接收」已开启。
  </div>

  <h2>已发现设备（每 5 秒刷新）</h2>
  <div id="tvs"><div class="empty">正在搜索…</div></div>

  <h2>已接收文件</h2>
  <div id="files"><div class="empty">还没有文件</div></div>

  <footer><a class="back" href="/">← 返回文件互传</a></footer>
</div>
<script>
let tvs = [];
async function loadTvs(){
  try{
    const r = await fetch("/api/dlna"); tvs = await r.json();
  }catch(e){ tvs = []; }
  const box = document.getElementById("tvs");
  if(!tvs.length){ box.innerHTML = '<div class="empty">未发现设备。确认电视已开启投屏接收，且与本机同 WiFi。</div>'; return; }
  box.innerHTML = "";
  tvs.forEach((t,i) => {
    const d = document.createElement("label"); d.className = "tv";
    d.innerHTML = '<input type="radio" name="tv" value="'+i+'"'+(i===0?' checked':'')+'><span class="nm">'+t.name+'</span>';
    box.appendChild(d);
  });
}
async function loadFiles(){
  try{
    const r = await fetch("/api/files"); const items = await r.json();
    const box = document.getElementById("files");
    if(!items.length){ box.innerHTML = '<div class="empty">还没有文件，去首页上传</div>'; return; }
    box.innerHTML = "";
    items.forEach(it => {
      const ext = it.name.split(".").pop().toLowerCase();
      const ok = ["png","jpg","jpeg","gif","webp","bmp","svg","mp4","mov","webm","mkv","avi","m4v"].includes(ext);
      const row = document.createElement("div"); row.className = "file";
      row.innerHTML = '<span class="nm">'+it.name+'</span>' +
        (ok ? '<button class="push" data-n="'+it.name+'">推到电视</button>'
            : '<span class="warn">仅支持图片/视频</span>');
      if(ok){
        row.querySelector(".push").onclick = () => push(it.name);
      }
      box.appendChild(row);
    });
  }catch(e){}
}
async function push(name){
  const sel = document.querySelector('input[name="tv"]:checked');
  if(!sel){ alert("请先选择一台电视"); return; }
  const idx = parseInt(sel.value, 10);
  try{
    const r = await fetch("/api/dlna/push", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({idx, name})});
    const j = await r.json();
    if(j.ok) alert("已推送：「"+name+"」，请查看电视");
    else alert("推送失败：电视未响应，或媒体格式不支持");
  }catch(e){ alert("推送失败"); }
}
loadTvs(); loadFiles();
setInterval(loadTvs, 5000);
setInterval(loadFiles, 5000);
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file_list(self):
        items = []
        for name in sorted(os.listdir(SHARED)):
            p = os.path.join(SHARED, name)
            if os.path.isfile(p):
                items.append({"name": name, "size": os.path.getsize(p)})
        return items

    def _static(self, fname, ctype):
        p = os.path.join(RES_DIR, fname)
        if not os.path.isfile(p):
            self.send_error(404)
            return
        with open(p, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _get_cookie(self, name):
        c = self.headers.get("Cookie", "")
        for part in c.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return ""

    def _has_session(self):
        if not PASSWORD:
            return True
        return _valid_session(self._get_cookie("sid"))

    def _serve_html(self, sid=None, tv=False):
        page = (HTML.replace("__LAN_URL__", LAN_URL)
                    .replace("__PORT__", str(PORT))
                    .replace("__SHARED__", SHARED)
                    .replace("__AUTH__", "1" if PASSWORD else "0")
                    .replace("__TVCLASS__", "tv" if tv else "")
                    .replace("__HEADNOTE__",
                             "这是大屏展示端 —— 用手机扫下方二维码，把照片 / 视频推上来即可在大屏显示。" if tv
                             else "把本页地址发给同 WiFi 下的其他设备，即可互相传文件。")
                    .replace("__QRNOTE__", TV_QRNOTE if tv else NORMAL_QRNOTE))
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if sid:
            self.send_header("Set-Cookie",
                             "sid=%s; Path=/; HttpOnly; Max-Age=%d" % (sid, SESSION_MINS * 60))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_login(self, err=False):
        page = LOGIN_HTML.replace("__ERR__", '<div class="err">密码错误，请重试</div>' if err else "")
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_dlna(self):
        data = DLNA_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_login(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        qs = urllib.parse.parse_qs(body.decode("utf-8", "ignore"))
        pw = qs.get("password", [""])[0]
        if hmac.compare_digest(pw, PASSWORD):
            sid = _new_session(SESSION_MINS)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie",
                             "sid=%s; Path=/; HttpOnly; Max-Age=%d" % (sid, SESSION_MINS * 60))
            self.end_headers()
        else:
            self._serve_login(err=True)

    def _download(self, name):
        p = os.path.join(SHARED, name)
        if not os.path.isfile(p):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Content-Length", str(os.path.getsize(p)))
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def _serve_one_time(self, token):
        # 单次有效下载链接: 取出并删除 token, 第二次访问即失效
        name = _consume_single_token(token)
        if not name:
            self.send_error(404)
            return
        p = os.path.join(SHARED, name)
        if not os.path.isfile(p):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Content-Length", str(os.path.getsize(p)))
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/s/"):
            return self._serve_one_time(u.path[len("/s/"):])
        if u.path == "/login":
            return self._serve_login()
        if u.path == "/qrcode.min.js":
            return self._static("qrcode.min.js", "application/javascript")
        if PASSWORD:
            qs = urllib.parse.parse_qs(u.query)
            t = qs.get("t", [""])[0]
            if t and _valid_temp_token(t):
                sid = _new_session(TEMP_MINS)
                return self._serve_html(sid)
            if not self._has_session():
                return self._serve_login()
        if u.path in ("/", "/index.html", "/tv"):
            self._serve_html(tv=(u.path == "/tv"))
        elif u.path == "/api/files":
            self._json(self._file_list())
        elif u.path == "/api/dlna":
            self._json([{"name": r["name"]} for r in dlna.get_renderers()])
        elif u.path == "/dlna":
            self._serve_dlna()
        elif u.path.startswith("/download/"):
            self._download(urllib.parse.unquote(u.path[len("/download/"):]))
        else:
            self.send_error(404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/share":
            # 单文件单次下载链接: 需已登录(或本就无密码); 生成的链接本身无需密码即可下载一次
            if PASSWORD and not self._has_session():
                self.send_error(401)
                return
            qs = urllib.parse.parse_qs(u.query)
            name = os.path.basename(qs.get("name", [""])[0])
            if not name or not os.path.isfile(os.path.join(SHARED, name)):
                self.send_error(404)
                return
            tok = _make_single_token(name)
            self._json({"url": "%s/s/%s" % (LAN_URL, tok)})
            return
        if u.path == "/login":
            return self._handle_login()
        if u.path == "/api/templink":
            if not self._has_session():
                self.send_error(401)
                return
            qs = urllib.parse.parse_qs(u.query)
            mins = int(qs.get("min", [str(TEMP_MINS)])[0])
            token = _make_temp_token(mins)
            self._json({"url": "%s/?t=%s" % (LAN_URL, token)})
            return
        if PASSWORD and not self._has_session():
            self.send_error(401)
            return
        if u.path == "/api/dlna/push":
            try:
                length = int(self.headers.get("Content-Length", 0))
                obj = json.loads(self.rfile.read(length) if length else b"{}")
            except Exception:
                obj = {}
            idx = obj.get("idx")
            name = os.path.basename(str(obj.get("name", "")))
            rs = dlna.get_renderers()
            if not isinstance(idx, int) or idx < 0 or idx >= len(rs):
                self.send_error(404)
                return
            p = os.path.join(SHARED, name)
            if not os.path.isfile(p):
                self.send_error(404)
                return
            # 密码模式下用单次链接(无需密码即可被电视拉取); 否则用普通下载链接
            if PASSWORD:
                url = "%s/s/%s" % (LAN_URL, _make_single_token(name))
            else:
                url = "%s/download/%s" % (LAN_URL, urllib.parse.quote(name))
            ext = name.split(".")[-1].lower()
            cls = "object.item.videoItem" if ext in ("mp4", "mov", "webm", "mkv", "avi", "m4v") else "object.item.imageItem"
            ok = dlna.push(rs[idx]["control"], url, name, cls)
            self._json({"ok": ok})
            return
        if u.path == "/upload":
            qs = urllib.parse.parse_qs(u.query)
            name = os.path.basename(qs.get("name", ["upload.bin"])[0]) or "upload.bin"
            dest = os.path.join(SHARED, name)
            base, ext = os.path.splitext(name)
            i = 1
            while os.path.exists(dest):
                dest = os.path.join(SHARED, "%s(%d)%s" % (base, i, ext))
                i += 1
            length = int(self.headers.get("Content-Length", 0))
            with open(dest, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            self._json({"ok": True, "name": os.path.basename(dest)})
        else:
            self.send_error(404)

    def do_DELETE(self):
        if PASSWORD and not self._has_session():
            self.send_error(401)
            return
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/api/files/"):
            name = os.path.basename(urllib.parse.unquote(u.path[len("/api/files/"):]))
            p = os.path.join(SHARED, name)
            if os.path.isfile(p):
                os.remove(p)
                self._json({"ok": True})
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


def run(argv=None):
    """启动 HTTP 中转版服务。argv 为 None 时解析命令行; 供合并版启动器调用。"""
    global SHARED, PASSWORD, LAN_URL
    ap = argparse.ArgumentParser(description="局域网文件互传小网页")
    ap.add_argument("--port", type=int, default=PORT, help="监听端口 (默认 8765)")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    ap.add_argument("--shared", default=os.environ.get("LAN_SHARED", ""),
                    help="文件存放目录 (默认程序目录下的 shared/; 也可用环境变量 LAN_SHARED; 支持绝对路径或相对路径)")
    ap.add_argument("--password", default=os.environ.get("LAN_PASS", ""),
                    help="访问密码 (启用后需输入密码才能进入; 也可用环境变量 LAN_PASS)")
    ap.add_argument("--https", action="store_true",
                    help="启用 HTTPS (自签证书; 浏览器会提示需手动信任)")
    ap.add_argument("--cert", default=os.path.join(APP_DIR, "server.pem"),
                    help="HTTPS 证书文件 (PEM, 含证书+私钥); 默认尝试自动生成 server.pem)")
    ap.add_argument("--no-browser", action="store_true",
                    help="启动后不自动打开浏览器 (默认会自动弹出本机页面)")
    ap.add_argument("--console", action="store_true",
                    help="exe 里强制使用终端模式 (调试用; 默认双击 exe 以托盘图标运行)")
    args = ap.parse_args(argv)

    # 解析自定义文件存放目录 (命令行 --shared > 环境变量 LAN_SHARED > 默认 ./shared)
    # 相对路径相对于程序所在目录解析(与默认 ./shared 行为一致), 绝对路径直接使用
    SHARED = args.shared if args.shared else SHARED
    SHARED = os.path.expanduser(SHARED)
    if not os.path.isabs(SHARED):
        SHARED = os.path.join(APP_DIR, SHARED)
    SHARED = os.path.abspath(SHARED)
    os.makedirs(SHARED, exist_ok=True)

    PASSWORD = args.password
    socketserver.TCPServer.allow_reuse_address = True
    scheme = "http"
    if args.https:
        scheme = "https"
        cert = args.cert
        if not os.path.isfile(cert):
            # 自动生成自签证书(仅依赖系统 openssl)
            ok = _gen_self_signed(cert)
            if not ok:
                print("无法生成自签证书: 请手动用 openssl 生成后通过 --cert 指定:")
                print('  openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365 -subj "/CN=localhost"')
                print('  copy /b cert.pem+key.pem server.pem')
                sys.exit(1)
        # 预热证书, 失败则退出并给出提示
        try:
            _ = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        except Exception as e:
            print("SSL 初始化失败:", e)
            sys.exit(1)
    LAN_URL = "%s://%s:%d" % (scheme, lan_ip(), args.port)
    dlna.start_discovery()
    httpd = socketserver.ThreadingTCPServer((args.host, args.port), Handler)
    local_url = "%s://127.0.0.1:%d" % (scheme, args.port)
    try:
        if args.https:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        if not args.no_browser:
            # 服务起来后自动在本机打开页面(延迟1秒确保端口已就绪)
            threading.Timer(1.0, webbrowser.open, args=(local_url,)).start()
        if getattr(sys, "frozen", False) and not args.console:
            # exe 默认: 无终端, 托盘图标运行
            _run_tray(httpd, local_url, LAN_URL)
            sys.exit(0)
        print("局域网文件互传已启动")
        print("  本机访问 : %s" % local_url)
        print("  同网设备 : %s" % LAN_URL)
        print("  电视大屏 : %s/tv  (手机扫码推送照片/视频到大屏展示)" % LAN_URL)
        print("  DLNA投屏 : %s/dlna  (把照片/视频推到支持DLNA的电视/机顶盒)" % LAN_URL)
        print("  文件目录 : %s" % SHARED)
        if args.https:
            print("  传输安全 : HTTPS 已启用 (自签证书, 浏览器需手动信任)")
        if PASSWORD:
            print("  访问保护 : 已开启密码 (会话 %d 小时, 临时链接默认 %d 分钟)" % (SESSION_MINS // 60, TEMP_MINS))
        else:
            print("  访问保护 : 未开启 (用 --password 设置密码)")
        print("  按 Ctrl+C 停止")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()
