# -*- coding: utf-8 -*-
"""
Trae 令牌保活 + GitHub Secret 同步（Windows 计划任务入口）
流程（有界，一次运行即退出）：
  1) 若 Trae SOLO CN 未运行则后台拉起（让客户端静默刷新 refreshToken 轮换链）
  2) 等待 TRAE_WAIT_SECONDS 秒（默认 100s）给客户端完成会话刷新
  3) 从 E:/devTools/AI/TraeCN/User/globalStorage/storage.json 同实例导出
     TRAE_ACCOUNTS + 4 个设备密钥（内存内，不落盘不回显）
  4) SealedBox 加密 PUT 更新 GitHub 5 个 Secret
  5) 结果写 local/logs/sync_YYYYMMDD.log（保留最近 30 份）
之后 GitHub Actions 于北京时间 00:23 / 08:23 两班跑签到。
"""
import base64, json, os, ssl, subprocess, sys, time, importlib.util
from datetime import datetime
from pathlib import Path
from io import StringIO

import urllib.request

# ── 配置 ──────────────────────────────────────────────
TRAE_EXE   = r"E:\devTools\trae\solo\TRAE SOLO CN.exe"
STORAGE    = Path(r"E:/devTools/AI/TraeCN/User/globalStorage/storage.json")
LOCAL_ENV  = Path(r"E:/project/github/workbuddy-checkin/local.env")   # WB_GITHUB_TOKEN
REPO       = "sclyxnet/Trae-AutoCheckin"
GET_TOKEN  = Path(r"E:/project/github/Trae-AutoCheckin/trae_get_token.py")
LOG_DIR    = Path(r"E:/project/github/Trae-AutoCheckin/local/logs")
WAIT_S     = int(os.environ.get("TRAE_WAIT_SECONDS", "100"))
# 代理铁律: GitHub 走 socks5
os.environ["HTTPS_PROXY"] = "socks5h://127.0.0.1:10808"
os.environ["HTTP_PROXY"]  = "socks5h://127.0.0.1:10808"

# ── 日志 Tee（控制台 + 文件，保留最近 30 份）──────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
_TODAY = datetime.now().strftime("%Y%m%d")
_LOG_PATH = LOG_DIR / ("sync_%s.log" % _TODAY)
# ── 幂等守卫: 当天已成功同步过则直接退出（供 ONLOGON 补偿任务复用）──
# TRAE_FORCE=1 可跳过守卫(调试/演示用)
if os.environ.get("TRAE_FORCE") != "1" and \
   _LOG_PATH.exists() and "[7] 全部完成" in _LOG_PATH.read_text(encoding="utf-8", errors="replace"):
    print("%s  [SKIP] 今天(%s)已成功同步过, 补偿任务直接退出" % (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), _TODAY))
    sys.exit(0)
for old in sorted(LOG_DIR.glob("sync_*.log"))[:-30]:
    old.unlink(missing_ok=True)
_LOG_F = open(_LOG_PATH, "a", encoding="utf-8")
class _Tee:
    def write(self, s):
        try: sys.__stdout__.write(s)
        except Exception: pass
        _LOG_F.write(s); _LOG_F.flush()
    def flush(self):
        _LOG_F.flush()
sys.stdout = _Tee(); sys.stderr = _Tee()

def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)

def _launch_and_tray():
    """explorer 代理后台拉起 Trae, 窗口出现后发 WM_CLOSE 缩系统托盘(前台无感知)。
    实测: Popen/Start-Process 直接 CreateProcess 会秒退, 必须 explorer 启动。"""
    try:
        subprocess.Popen(["explorer.exe", TRAE_EXE])
        log("[1] Trae 未运行, 已 explorer 代理后台拉起")
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        WM_CLOSE = 0x0010
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        closed = []
        for _ in range(18):                       # 18 x 10s = 180s, Trae 启动慢
            time.sleep(10)
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                 capture_output=True, timeout=30).stdout.decode("gbk", "replace")
            target = {int(l.split('","')[1]) for l in out.splitlines()
                      if len(l.split('","')) >= 2 and l.split('","')[0].strip('"').lower() == "trae solo cn.exe"}
            if not target:
                continue
            def _cb(hwnd, _):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in target and user32.IsWindowVisible(hwnd):
                    t = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, t, 256)
                    if t.value:                    # 有标题的可见主窗口
                        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                        closed.append(t.value)
                        return False
                return True
            user32.EnumWindows(WNDENUMPROC(_cb), 0)
            if closed:
                break
        log("[1] 主窗口%s" % ("已发 WM_CLOSE -> 缩系统托盘(标题:%s)" % closed[0] if closed
                              else "未出现/已在托盘, 前台无感知"))
    except Exception as e:
        log("[1][X] 拉起 Trae 失败: %r（继续用现有存储令牌尝试同步）" % e)

def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 0) storage.json 新鲜度检查: 进程即使驻留托盘, 也可能几天没真正刷过会话。
    #    mtime 不是今天 -> 杀掉进程强制重拉(重拉后会刷新)。
    stale = False
    try:
        stale = datetime.fromtimestamp(STORAGE.stat().st_mtime).date() != datetime.now().date()
    except OSError:
        stale = True
    def kill_trae():
        r = subprocess.run(["taskkill", "/F", "/IM", "TRAE SOLO CN.exe", "/T"],
                           capture_output=True, timeout=60)
        log("[0] 已杀掉旧 TRAE 进程(storage.json 停留在 %s), 强制重拉刷新会话" %
            datetime.fromtimestamp(STORAGE.stat().st_mtime).strftime("%Y-%m-%d %H:%M"))
        time.sleep(3)
    # 1) 拉起 Trae（若未运行 或 数据过期）
    def trae_running():
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq TRAE SOLO CN.exe"],
                                 capture_output=True, timeout=30).stdout.decode("gbk", "replace").lower()
            return "trae solo cn.exe" in out
        except Exception:
            return False
    running = trae_running()
    if running and stale:
        kill_trae()
        running = False
    if not running:
        _launch_and_tray()
    else:
        log("[1] Trae 已在运行且 storage.json 是今天的, 跳过拉起")
    # 2) 等客户端刷新会话
    log("[2] 等待 %ds 让 Trae 刷新会话..." % WAIT_S)
    time.sleep(WAIT_S)
    # 3) 同实例导出（内存）
    spec = importlib.util.spec_from_file_location("tg", str(GET_TOKEN))
    tg = importlib.util.module_from_spec(spec); spec.loader.exec_module(tg)
    ex = tg.extract(str(STORAGE))
    assert ex and ex.get("accessToken"), "storage.json 提取失败: %s" % STORAGE
    # 设备密钥（与 update_trae_full.py 同源，同实例防 20405）
    s = json.load(open(STORAGE, encoding="utf-8"))
    did = ""
    for k in s:
        if k.startswith("iCubeAuthInfo://icube-dc:"):
            d = k.split(":")[-1]
            if d.isdigit(): did = d; break
    assert did, "storage.json 中无 iCubeAuthInfo://icube-dc:<did> 设备密钥"
    dev_enc = s.get("iCubeAuthInfo://icube-dc:%s" % did)
    dev = json.loads(tg.decrypt_storage_value(dev_enc.strip()).decode("utf-8", "replace"))
    priv = (dev.get("privateKeyPEM") or "").strip()
    pub = (dev.get("publicKeyPEM") or "").strip()
    machineId = (s.get("telemetry.machineId") or "").strip()
    assert priv and pub, "设备私钥/公钥为空"
    accounts = json.dumps([{"accessToken": ex["accessToken"], "refreshToken": ex.get("refreshToken"),
                            "uid": ex.get("uid"), "name": ex.get("nickname") or ""}],
                          ensure_ascii=False, separators=(",", ":"))
    # 校验 accessToken 有效
    import calendar
    try:
        p = ex["accessToken"].split(".")[1]; p += "=" * (-len(p) % 4)
        exp = json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
        valid = exp > time.time()
        log("[3] 导出 OK: uid=%s accessToken 有效=%s 到期=%s" % (
            ex.get("uid"), valid, datetime.fromtimestamp(exp)))
        assert valid, "accessToken 已过期，Trae 客户端未完成会话刷新"
    except AssertionError:
        raise
    except Exception as e:
        log("[3][!] exp 校验异常(%r)，继续同步" % e)
    # 4) 更新 5 个 GitHub Secret
    TOKEN = None
    for line in LOCAL_ENV.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("WB_GITHUB_TOKEN="):
            TOKEN = line.strip().split("=", 1)[1].strip()
    assert TOKEN, "local.env 中找不到 WB_GITHUB_TOKEN"
    from nacl.public import PublicKey, SealedBox
    API = "https://api.github.com"
    H = {"Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
         "User-Agent": "trae-keepalive", "X-GitHub-Api-Version": "2022-11-28"}
    CTX = ssl.create_default_context()
    def api(path, data=None, method="GET"):
        req = urllib.request.Request(API + path,
            data=(json.dumps(data).encode() if data is not None else None),
            headers=H, method=method)
        if data is not None: req.add_header("Content-Type", "application/json")
        body = urllib.request.urlopen(req, context=CTX, timeout=60).read().decode()
        return json.loads(body) if body.strip() else {}
    pk = api("/repos/%s/actions/secrets/public-key" % REPO)
    box = SealedBox(PublicKey(base64.b64decode(pk["key"])))
    def put(name, value):
        enc = base64.b64encode(box.encrypt(value.encode())).decode()
        api("/repos/%s/actions/secrets/%s" % (REPO, name),
            {"encrypted_value": enc, "key_id": pk["key_id"]}, "PUT")
        log("[4] [PUT] %s OK" % name)
    put("TRAE_ACCOUNTS", accounts)
    put("TRAE_DEVICE_ID", did)
    put("TRAE_MACHINE_ID", machineId)
    put("TRAE_DEVICE_KEY_PEM", priv.replace("\n", "\\n"))
    put("TRAE_DEVICE_PUB_PEM", pub.replace("\n", "\\n"))
    log("[5] 5 个 Secret 已同步, dispatch 触发一次签到...")
    api("/repos/%s/actions/workflows/trae-auto-checkin.yml/dispatches" % REPO,
        {"ref": "main"}, "POST")
    rid = None
    for _ in range(20):          # 有界: 最多 ~100s 找到本次 run
        runs = api("/repos/%s/actions/runs?per_page=3" % REPO)["workflow_runs"]
        for r in runs:
            if r["event"] == "workflow_dispatch" and r["status"] in ("queued", "in_progress"):
                rid = r["id"]; break
        if rid: break
        time.sleep(5)
    if not rid:
        log("[5][!] 未捕获到 dispatch run（可能排队中），GitHub 侧稍后自会执行")
    else:
        log("[6] run id=%s, 等待完成(最多 ~240s)..." % rid)
        concl = None
        for _ in range(24):
            r = api("/repos/%s/actions/runs/%s" % (REPO, rid))
            if r["status"] == "completed":
                concl = r["conclusion"]; break
            time.sleep(10)
        log("[6] 签到 run 结论: %s" % (concl or "timeout(仍在运行, 推送结果以 PushPlus 为准)"))
    log("[7] 全部完成：Secret 已同步 + 已触发签到，结果看 PushPlus 推送")

if __name__ == "__main__":
    try:
        main()
        rc = 0
    except Exception as e:
        log("[FATAL] %r" % e)
        rc = 1
    # 日志轮转：保留最近 30 份
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logs = sorted(LOG_DIR.glob("sync_*.log"))
        for old in logs[:-30]:
            old.unlink(missing_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        # 将本次 stdout 重写为日志文件（简单方案：追加式太复杂，直接在这里把关键信息已打印）
    except Exception:
        pass
    sys.exit(rc)
