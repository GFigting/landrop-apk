#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网 P2P 文件互传 (WebRTC 版)
- 仅依赖 Python 标准库 + 同目录的 qrcode.min.js
- 运行: python p2p_share.py
- 同 WiFi 设备浏览器打开 http://<本机IP>:8766 , 加入同一房间(默认 lan)
- 文件通过 WebRTC 数据通道在设备间点对点直传, 服务器只转发握手信令(offer/answer/ICE)
- 因此: 数据不经过服务器, 局域网内延迟极低, 适合大文件/多模态内容

说明:
- 信令用长轮询(long-poll)实现, 无需额外 pip 包
- 默认配置一个公共 STUN 仅用于 ICE 候选发现; 纯同网段场景靠 host 候选也能直连,
  STUN 不可达时会自动退化, 不影响局域网内连接
"""

import http.server
import socketserver
import os
import json
import socket
import threading
import time
import uuid
import argparse
import urllib.parse
import hmac
import sys
import webbrowser
import updater

PORT = 8766
# 打包成 exe 后, qrcode.min.js 在 PyInstaller 解压资源目录(_MEIPASS)里
if getattr(sys, "frozen", False):
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 房间密码: 命令行 --password 或环境变量 P2P_PASS 设定; 不设定则无密码(向后兼容)
ROOM_PASSWORD = os.environ.get("P2P_PASS") or ""

# ---------------- 信令中继状态 (房间) ----------------
LOCK = threading.Lock()


def get_room(room):
    r = SIGNAL.get(room)
    if r is None:
        r = {"inbox": {}, "peers": {}, "cond": threading.Condition(LOCK)}
        SIGNAL[room] = r
    return r


SIGNAL = {}  # room -> {"inbox":{peer_id:[msg]}, "peers":{peer_id:name}, "cond":Condition}


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


LAN_URL = "http://%s:%d" % (lan_ip(), PORT)
NEED_PW = "true" if ROOM_PASSWORD else "false"  # 页面据此决定是否显示密码框


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔄</text></svg>">
<meta name="theme-color" content="#185fa5">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="P2P 互传">
<title>局域网 P2P 互传 (WebRTC)</title>
<script src="/qrcode.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
         background:#f5f7fa; color:#1f2933; }
  .wrap { max-width:760px; margin:0 auto; padding:24px 16px 48px; }
  header h1 { font-size:20px; margin:0 0 4px; }
  .url { background:#e8f0fe; color:#185fa5; border-radius:8px; padding:10px 14px; font-size:14px;
         word-break:break-all; margin:12px 0; }
  .url b { color:#0c447c; }
  .flex { display:flex; gap:14px; align-items:flex-start; margin-top:14px; }
  #qr { background:#fff; border:.5px solid #e3e8ef; border-radius:10px; padding:8px; line-height:0;
        min-width:148px; min-height:148px; }
  .hint { font-size:13px; color:#5f6b7a; line-height:1.7; }
  .hint b { color:#3a4656; }
  .bar { display:flex; gap:18px; align-items:center; flex-wrap:wrap; margin:16px 0 4px; font-size:13px; color:#3a4656; }
  .bar b { color:#185fa5; }
  .bar b.ok { color:#1a8a3c; }
  .guide { display:flex; gap:10px; margin:14px 0 2px; }
  .guide .g { flex:1; background:#fff; border:.5px solid #e3e8ef; border-radius:10px;
              padding:10px 12px; font-size:12px; color:#5f6b7a; line-height:1.7; }
  .guide .g b { color:#185fa5; font-size:14px; margin-right:2px; }
  @media (max-width: 600px) {
    header h1 { font-size: 22px; }
    #drop { padding: 40px 16px; }
    #drop .big { font-size: 15px; }
    .item button { padding: 9px 15px; font-size: 14px; }
    .guide { flex-direction: column; gap: 6px; }
  }
  .peers, .recv { margin-top:22px; }
  .peers h2, .recv h2 { font-size:15px; margin:0 0 10px; color:#3a4656; }
  .item { display:flex; align-items:center; gap:12px; background:#fff; border:.5px solid #e3e8ef;
          border-radius:10px; padding:10px 12px; margin-bottom:8px; }
  .item .meta { flex:1 1 auto; min-width:0; }
  .item .nm { font-size:14px; }
  .item .sz { font-size:12px; color:#8a94a3; }
  .item button { font-size:13px; color:#185fa5; background:none; border:.5px solid #b4c6e0;
          border-radius:6px; padding:5px 12px; cursor:pointer; }
  .item button:disabled { color:#b4bcc8; border-color:#e3e8ef; cursor:default; }
  #drop { border:2px dashed #b4c6e0; border-radius:12px; background:#fff; padding:30px 16px; text-align:center;
          color:#5f6b7a; cursor:pointer; transition:.15s; margin-top:18px; }
  #drop.on { border-color:#185fa5; background:#f0f6ff; color:#185fa5; }
  #drop.dis { opacity:.5; cursor:not-allowed; }
  #file { display:none; }
  .progress { margin-top:14px; }
  .prow { display:flex; gap:10px; align-items:center; margin-bottom:8px; font-size:13px; }
  .pbar { flex:1; height:8px; background:#e3e8ef; border-radius:6px; overflow:hidden; }
  .pbar > i { display:block; height:100%; width:0; background:#185fa5; transition:width .1s; }
  .empty { color:#9aa4b2; font-size:13px; padding:8px 0; }
  footer { margin-top:28px; font-size:12px; color:#9aa4b2; line-height:1.7; }
  footer code { background:#eef2f7; padding:1px 5px; border-radius:4px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>局域网 P2P 互传</h1>
    <div>基于 WebRTC，文件在设备间点对点直传，服务器只做握手。</div>
  </header>

  <div class="url">本机地址：<b id="myurl">__LAN_URL__</b> &nbsp;·&nbsp; 房间：<b id="room"></b></div>
  <div class="flex">
    <div id="qr"></div>
    <div class="hint">用相机 / 扫码 App 扫左侧二维码，<br>手机会自动打开<b>本房间</b>，<br>与本机点对点互传（无需手动输房间）。</div>
  </div>

  <div class="bar">状态：<b id="state">未连接</b> &nbsp;·&nbsp; 本机ID：<span id="pid">-</span></div>

  <div class="guide">
    <div class="g"><b>①</b> 其他设备也打开本页<br>自动进入同一房间</div>
    <div class="g"><b>②</b> 在「同房间设备」里<br>点对方右侧的「连接」</div>
    <div class="g"><b>③</b> 状态变为绿色「已连接」后<br>把文件拖到下方区域即可发送</div>
  </div>

  <div class="peers">
    <h2>同房间设备</h2>
    <div id="peerList"><div class="empty">暂无其他设备，让对方也打开本页并加入同一房间。</div></div>
  </div>

  <div id="drop" class="dis">
    <div class="big">连接后，把文件拖到这里（或点击选择）发送给对方</div>
    <input id="file" type="file" multiple>
  </div>
  <div class="progress" id="prog" style="display:none"></div>

  <div class="recv">
    <h2>接收中</h2>
    <div id="recvList"><div class="empty">还没有接收任务。</div></div>
  </div>

  <footer>
    原理：浏览器间建立 WebRTC 数据通道，文件分块直传；服务器仅中转 <code>offer/answer/ICE</code> 握手信息。<br>
    默认使用一个公共 STUN 仅做候选发现，纯同网段靠本机候选也能直连；数据全程不经服务器。
  </footer>
</div>

<div id="login" style="display:none; position:fixed; inset:0; background:rgba(245,247,250,.96);
     z-index:50; display:flex; align-items:center; justify-content:center;">
  <div style="background:#fff; border:.5px solid #e3e8ef; border-radius:14px; padding:28px 26px; width:300px;
       box-shadow:0 8px 30px rgba(20,40,80,.12); text-align:center;">
    <div style="font-size:17px; font-weight:600; color:#1f2933; margin-bottom:4px;">房间已上锁</div>
    <div style="font-size:13px; color:#5f6b7a; margin-bottom:16px;">输入房间密码以加入并互传</div>
    <input id="pwInput" type="password" placeholder="房间密码"
           style="width:100%; padding:10px 12px; border:1px solid #cfd8e3; border-radius:8px; font-size:14px;
                  outline:none; box-sizing:border-box;">
    <div id="pwErr" style="color:#d64545; font-size:12px; min-height:16px; margin-top:8px;"></div>
    <button id="pwBtn" style="width:100%; margin-top:6px; padding:10px 0; background:#185fa5; color:#fff;
            border:none; border-radius:8px; font-size:14px; cursor:pointer;">进入</button>
  </div>
</div>

<script>
const NEED_PW = __NEED_PW__;
const qs = new URLSearchParams(location.search);
const room = qs.get("room") || "lan";
document.getElementById("room").textContent = room;
let peerId = null;
let gPw = "";  // 房间密码(输入后保存, 每次请求附带)
const ICE = [{ urls: "stun:stun.l.google.com:19302" }];
let pc = null, dc = null, target = null;
let pendingCand = [];

function $(id){ return document.getElementById(id); }
function setState(s){ $("state").textContent = s; $("state").className = (s === "已连接") ? "ok" : ""; }
function pwParam(){ return gPw ? "&pw=" + encodeURIComponent(gPw) : ""; }

async function join(){
  const r = await fetch("/join?room=" + encodeURIComponent(room) + "&name=" + encodeURIComponent("设备" + Math.floor(Math.random()*1000)) + pwParam());
  const j = await r.json();
  peerId = j.peer;
  $("pid").textContent = peerId;
  pollSignals();
  refreshPeers();
}
async function refreshPeers(){
  if(!peerId) return;
  try{
    const r = await fetch("/peers?room=" + encodeURIComponent(room) + "&self=" + peerId + pwParam());
    const list = await r.json();
    renderPeers(list);
  }catch(e){}
  setTimeout(refreshPeers, 3000);
}
async function pollSignals(){
  try{
    const r = await fetch("/signal?room=" + encodeURIComponent(room) + "&peer=" + peerId + pwParam());
    const msgs = await r.json();
    for(const m of msgs) onSignal(m);
  }catch(e){}
  pollSignals();
}
async function send(to, data){
  await fetch("/signal?room=" + encodeURIComponent(room) + "&from=" + peerId + "&to=" + to + pwParam(),
    { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(data) });
}
function onSignal(m){
  const d = m.data;
  if(d.type === "offer"){ recvOffer(m.from, d.sdp); }
  else if(d.type === "answer"){ pc.setRemoteDescription(d.sdp).then(flushCand); }
  else if(d.type === "candidate"){ addCand(d.candidate); }
}
function addCand(c){
  if(pc && pc.remoteDescription){ pc.addIceCandidate(c).catch(function(){}); }
  else { pendingCand.push(c); }
}
function flushCand(){
  pendingCand.forEach(function(c){ pc.addIceCandidate(c).catch(function(){}); });
  pendingCand = [];
}
async function connectTo(t){
  if(pc) return;
  target = t; setState("正在连接...");
  pc = new RTCPeerConnection({ iceServers: ICE });
  dc = pc.createDataChannel("files");
  setupDc(dc);
  pc.onicecandidate = function(e){ if(e.candidate) send(target, { type:"candidate", candidate:e.candidate }); };
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  send(target, { type:"offer", sdp: pc.localDescription });
}
async function recvOffer(from, sdp){
  if(pc) return;
  target = from; setState("正在连接...");
  pc = new RTCPeerConnection({ iceServers: ICE });
  pc.ondatachannel = function(e){ setupDc(e.channel); };
  pc.onicecandidate = function(e){ if(e.candidate) send(from, { type:"candidate", candidate:e.candidate }); };
  await pc.setRemoteDescription(sdp); flushCand();
  const ans = await pc.createAnswer();
  await pc.setLocalDescription(ans);
  send(from, { type:"answer", sdp: pc.localDescription });
}
function setupDc(ch){
  dc = ch; ch.binaryType = "arraybuffer";
  ch.onopen = function(){ setState("已连接"); enableDrop(true); };
  ch.onclose = function(){ setState("已断开"); enableDrop(false); };
  ch.onmessage = onData;
}

const CHUNK = 16 * 1024;
function sendFile(file){
  if(!dc || dc.readyState !== "open"){ alert("尚未连接"); return; }
  dc.send(JSON.stringify({ t:"meta", name:file.name, size:file.size, mime:file.type }));
  let off = 0;
  function step(){
    if(off >= file.size){ dc.send(JSON.stringify({ t:"eof" })); return; }
    const slice = file.slice(off, off + CHUNK);
    slice.arrayBuffer().then(function(buf){
      dc.send(buf); off += buf.byteLength;
      setSend(file.name, off, file.size);
      if(dc.bufferedAmount > 1024 * 1024){
        dc.onbufferedamountlow = function(){ dc.onbufferedamountlow = null; step(); };
      } else { step(); }
    });
  }
  step();
}

let cur = null;
const recvRows = {};
function onData(e){
  if(typeof e.data === "string"){
    const m = JSON.parse(e.data);
    if(m.t === "meta"){
      cur = { name:m.name, size:m.size, mime:m.mime, parts:[], got:0 };
      addRecvRow(cur);
    } else if(m.t === "eof"){
      finalize(cur); cur = null;
    }
  } else {
    cur.parts.push(e.data); cur.got += e.data.byteLength;
    setRecv(cur);
  }
}
function finalize(r){
  const blob = new Blob(r.parts, { type: r.mime });
  const url = URL.createObjectURL(blob);
  const row = recvRows[r.name];
  if(row){
    row.querySelector(".act").innerHTML = '<a href="' + url + '" download="' + r.name + '">下载</a>';
  }
}

function renderPeers(list){
  const el = $("peerList");
  if(!list.length){ el.innerHTML = '<div class="empty">暂无其他设备，让对方也打开本页并加入同一房间。</div>'; return; }
  el.innerHTML = "";
  list.forEach(function(p){
    const row = document.createElement("div"); row.className = "item";
    row.innerHTML = '<div class="meta"><div class="nm">' + p.name + '</div><div class="sz">ID ' + p.id + '</div></div>' +
                    '<button data-id="' + p.id + '">连接</button>';
    row.querySelector("button").onclick = function(){ connectTo(p.id); };
    el.appendChild(row);
  });
}
function enableDrop(on){
  const d = $("drop");
  if(on){ d.classList.add("on"); d.classList.remove("dis"); }
  else { d.classList.remove("on"); d.classList.add("dis"); }
}
const drop = $("drop"), input = $("file");
drop.onclick = function(){ if(!drop.classList.contains("dis")) input.click(); };
input.onchange = function(){ for(const f of input.files) sendFile(f); input.value = ""; };
["dragenter","dragover"].forEach(function(ev){ drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.add("on"); }); });
["dragleave","drop"].forEach(function(ev){ drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.remove("on"); }); });
drop.addEventListener("drop", function(e){ for(const f of e.dataTransfer.files) sendFile(f); });

function setSend(name, got, size){
  const p = $("prog");
  if(p.style.display === "none") p.style.display = "block";
  let row = p.querySelector('[data-n="send"]');
  if(!row){
    row = document.createElement("div"); row.className = "prow"; row.setAttribute("data-n","send");
    row.innerHTML = '<span class="lbl"></span><div class="pbar"><i></i></div>';
    p.appendChild(row);
  }
  row.querySelector(".lbl").textContent = "发送 " + name + " " + Math.round(got/size*100) + "%";
  row.querySelector("i").style.width = (got/size*100) + "%";
}
function addRecvRow(r){
  const p = $("recvList");
  if(p.querySelector(".empty")) p.innerHTML = "";
  const row = document.createElement("div"); row.className = "item";
  row.innerHTML = '<div class="meta"><div class="nm">' + r.name + '</div><div class="sz">0%</div></div><div class="act"></div>';
  p.appendChild(row);
  recvRows[r.name] = row;
}
function setRecv(r){
  const row = recvRows[r.name];
  if(row) row.querySelector(".sz").textContent = Math.round(r.got/r.size*100) + "%";
}

function makeQR(text){
  for(let t=1;t<=12;t++){
    try{ const qr=qrcode(t,"M"); qr.addData(text); qr.make(); return qr.createSvgTag({cellSize:5,margin:2}); }catch(e){}
  }
  return "二维码生成失败";
}
$("qr").innerHTML = makeQR($("myurl").textContent.trim() + "?room=" + encodeURIComponent(room));

// 房间密码: 若服务端开启, 先弹出密码框, 输入正确后才 join
function startApp(){
  if(!NEED_PW){ join(); return; }
  const box = $("login");
  box.style.display = "flex";
  const input = $("pwInput"), err = $("pwErr"), btn = $("pwBtn");
  function tryLogin(){
    const v = input.value;
    if(!v){ err.textContent = "请输入密码"; return; }
    gPw = v;
    // 校验一次: 用 join 试探(返回 401 表示密码错)
    fetch("/join?room=" + encodeURIComponent(room) + "&name=" + encodeURIComponent("设备" + Math.floor(Math.random()*1000)) + pwParam())
      .then(function(r){
        if(r.status === 401){ err.textContent = "密码错误"; gPw = ""; return null; }
        return r.json();
      })
      .then(function(j){
        if(!j) return;
        box.style.display = "none";
        peerId = j.peer;
        $("pid").textContent = peerId;
        pollSignals();
        refreshPeers();
      })
      .catch(function(){ err.textContent = "连接失败"; gPw = ""; });
  }
  btn.onclick = tryLogin;
  input.addEventListener("keydown", function(e){ if(e.key === "Enter") tryLogin(); });
  input.focus();
}
startApp();
</script>
</body>
</html>
"""


def _run_tray(httpd, local_url, lan_url):
    """打包成 exe 后以托盘图标方式运行: 无终端窗口, 双击托盘打开页面, 菜单可退出。"""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        while True:
            time.sleep(3600)
    img = Image.new("RGB", (64, 64), (24, 95, 165))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(255, 255, 255))
    d.ellipse([20, 20, 44, 44], outline=(24, 95, 165), width=4)
    d.ellipse([28, 28, 36, 36], fill=(24, 95, 165))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def on_open(icon, item):
        webbrowser.open(local_url)

    def on_quit(icon, item):
        threading.Thread(target=httpd.shutdown, daemon=True).start()
        icon.stop()

    def on_download(icon, item):
        import webbrowser
        r = latest.get("remote") or {}
        url = r.get("download_page") or "https://github.com/GFigting/landrop-apk/releases"
        try:
            webbrowser.open(url)
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem("打开互传页面", on_open, default=True),
        pystray.MenuItem("下载新版本", on_download),
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon(
        "P2PDrop", img,
        "P2P 互传运行中\n本机: %s\n其他设备: %s" % (local_url, lan_url),
        menu)
    try:
        icon.notify("已启动: %s" % local_url)
    except Exception:
        pass

    # 后台检查更新: 发现新版本弹气泡提示, 点击菜单「下载新版本」前往
    latest = {"remote": None}
    _app_dir = os.path.dirname(os.path.abspath(sys.executable)) \
        if getattr(sys, "frozen", False) else BASE_DIR

    def _on_update(remote):
        if not remote:
            return
        latest["remote"] = remote
        try:
            msg = "LAN Drop 新版本 v%s，右键菜单下载" % remote.get("version", "")
            msg = msg if len(msg) <= 60 else msg[:59] + "…"
            icon.notify(msg)
        except Exception:
            pass

    try:
        updater.check_async(_on_update, _app_dir)
    except Exception:
        pass
    icon.run()


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

    def _json_code(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _check_pw(self, u):
        """房间已设密码时校验; 返回 True 表示放行"""
        if not ROOM_PASSWORD:
            return True
        qs = urllib.parse.parse_qs(u.query)
        got = qs.get("pw", [""])[0]
        # 防时序侧信道比较
        return hmac.compare_digest(got, ROOM_PASSWORD)

    def _static(self, fname, ctype):
        p = os.path.join(BASE_DIR, fname)
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

    def _serve_html(self):
        page = HTML.replace("__LAN_URL__", LAN_URL).replace("__NEED_PW__", NEED_PW)
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._serve_html()
        elif u.path == "/qrcode.min.js":
            self._static("qrcode.min.js", "application/javascript")
        elif u.path == "/peers":
            self._get_peers(u)
        elif u.path == "/signal":
            self._get_signal(u)
        else:
            self.send_error(404)

    def _get_peers(self, u):
        if not self._check_pw(u):
            return self._json_code(401, {"error": "密码错误"})
        qs = urllib.parse.parse_qs(u.query)
        room = qs.get("room", ["lan"])[0]
        self_ = qs.get("self", [""])[0]
        with LOCK:
            roomd = SIGNAL.get(room)
            if not roomd:
                return self._json([])
            peers = [{"id": k, "name": v} for k, v in roomd["peers"].items() if k != self_]
        self._json(peers)

    def _get_signal(self, u):
        if not self._check_pw(u):
            return self._json_code(401, {"error": "密码错误"})
        qs = urllib.parse.parse_qs(u.query)
        room = qs.get("room", ["lan"])[0]
        peer = qs.get("peer", [""])[0]
        deadline = time.time() + 25
        with LOCK:
            roomd = get_room(room)
            cond = roomd["cond"]
            while time.time() < deadline:
                box = roomd["inbox"].get(peer)
                if box:
                    msgs = roomd["inbox"].pop(peer)
                    return self._json(msgs)
                cond.wait(1.0)
            self._json([])

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/join":
            self._post_join(u)
        elif u.path == "/signal":
            self._post_signal(u)
        else:
            self.send_error(404)

    def _post_join(self, u):
        if not self._check_pw(u):
            return self._json_code(401, {"error": "密码错误"})
        qs = urllib.parse.parse_qs(u.query)
        room = qs.get("room", ["lan"])[0]
        name = qs.get("name", ["anon"])[0]
        pid = uuid.uuid4().hex[:8]
        with LOCK:
            roomd = get_room(room)
            roomd["peers"][pid] = name
            others = [{"id": k, "name": v} for k, v in roomd["peers"].items() if k != pid]
        self._json({"peer": pid, "peers": others})

    def _post_signal(self, u):
        if not self._check_pw(u):
            return self._json_code(401, {"error": "密码错误"})
        qs = urllib.parse.parse_qs(u.query)
        room = qs.get("room", ["lan"])[0]
        frm = qs.get("from", [""])[0]
        to = qs.get("to", [""])[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {}
        with LOCK:
            roomd = get_room(room)
            roomd["inbox"].setdefault(to, []).append({"from": frm, "data": data})
            roomd["cond"].notify_all()
        self._json({"ok": True})

    def log_message(self, *a):
        pass


def run(argv=None):
    """启动 P2P 信令服务。argv 为 None 时解析命令行; 供合并版启动器调用。"""
    global ROOM_PASSWORD, NEED_PW, LAN_URL
    ap = argparse.ArgumentParser(description="局域网 P2P 文件互传 (WebRTC)")
    ap.add_argument("--port", type=int, default=PORT, help="监听端口 (默认 8766)")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    ap.add_argument("--password", default=None, help="房间密码; 不填则不设密码(向后兼容)")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--console", action="store_true",
                    help="exe 里强制使用终端模式 (默认双击 exe 以托盘图标运行)")
    args = ap.parse_args(argv)

    if args.password is not None:
        ROOM_PASSWORD = args.password
    if ROOM_PASSWORD:
        NEED_PW = "true"

    socketserver.TCPServer.allow_reuse_address = True
    LAN_URL = "http://%s:%d" % (lan_ip(), args.port)
    httpd = socketserver.ThreadingTCPServer((args.host, args.port), Handler)
    local_url = "http://127.0.0.1:%d" % args.port
    try:
        if not args.no_browser:
            threading.Timer(1.0, webbrowser.open, args=(local_url,)).start()
        if getattr(sys, "frozen", False) and not args.console:
            _run_tray(httpd, local_url, LAN_URL)
            sys.exit(0)
        print("局域网 P2P 互传已启动 (WebRTC)")
        print("  本机访问 : %s" % local_url)
        print("  同网设备 : %s" % LAN_URL)
        print("  手机扫码 : 二维码已含房间号, 自动加入同一房间")
        if ROOM_PASSWORD:
            print("  房间密码 : 已启用 (同网设备需输入密码才能加入)")
        print("  按 Ctrl+C 停止")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()
