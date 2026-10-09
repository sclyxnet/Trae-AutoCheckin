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
for old in sorted(LOG_DIR.glob("sync_*.log"))[:-30]:
    old.unlink(missing_ok=True)
_LOG_F = open(LOG_DIR / ("sync_%s.log" % datetime.now().strftime("%Y%m%d")), "a", encoding="utf-8")
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

def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 1) 拉起 Trae（若未运行）
    def trae_running():
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq TRAE SOLO CN.exe"],
                                 capture_output=True, timeout=30).stdout.decode("gbk", "replace").lower()
            return "trae solo cn.exe" in out
        except Exception:
            return False
    started = False
    if not trae_running():
        try:
            # 仅在 Trae 未运行时后台拉起，最小化、前台无感知
            subprocess.Popen([TRAE_EXE, "--start-minimized"], cwd=str(Path(TRAE_EXE).parent),
                             creationflags=0x00000008 | 0x00000004,  # DETACHED_PROCESS | CREATE_NO_WINDOW
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = True
            log("[1] Trae 未运行，已后台最小化拉起: %s" % TRAE_EXE)
        except Exception as e:
            try:
                # 兜底: PowerShell 最小化启动
                subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                                  "-Command", "Start-Process -FilePath '%s' -WindowStyle Minimized" % TRAE_EXE],
                                 creationflags=0x08000000)
                started = True
                log("[1] PowerShell 兜底最小化拉起 Trae（Popen 失败: %r）" % e)
            except Exception as e2:
                log("[1][X] 拉起 Trae 失败: %r / %r（继续用现有存储令牌尝试同步）" % (e, e2))
    else:
        log("[1] Trae 已在运行，跳过拉起")
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
    log("[5] 全部完成：GitHub Secret 已同步，等待 Actions 两班(00:23/08:23)签到")

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
