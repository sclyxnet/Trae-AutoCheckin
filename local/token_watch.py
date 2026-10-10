# -*- coding: utf-8 -*-
"""
token 刷新观察器：每天记录 storage.json 里 accessToken 的指纹/exp/iat，
追踪 Trae 客户端到底在第几天真正刷新 token（exp 或指纹变化 = 发生了刷新）。
只输出时间与指纹前 8 位，绝不落明文 token。
用法: python token_watch.py   （追加一行到 token_watch.log，变更时额外打 [CHANGED]）
"""
import base64, json, sys, importlib.util, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

STORAGE = Path(r"E:/devTools/AI/TraeCN/User/globalStorage/storage.json")
GET_TOKEN = Path(r"E:/project/github/Trae-AutoCheckin/trae_get_token.py")
LOG = Path(r"E:/project/github/Trae-AutoCheckin/local/token_watch.log")
BJ = timezone(timedelta(hours=8))

def jwt_claims(token):
    p = token.split(".")[1]; p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))

def main():
    spec = importlib.util.spec_from_file_location("tg", str(GET_TOKEN))
    tg = importlib.util.module_from_spec(spec); spec.loader.exec_module(tg)
    ex = tg.extract(str(STORAGE)) or {}
    at = ex.get("accessToken") or ""
    if not at:
        print("[X] 未提取到 accessToken"); sys.exit(1)
    c = jwt_claims(at)
    exp = int(c.get("exp", 0)); iat = int(c.get("iat", 0))
    fp = hashlib.sha256(at.encode()).hexdigest()[:8]          # 指纹, 非明文
    now = datetime.now(BJ)
    line = "%s  fp=%s  iat=%s  exp=%s  剩余=%.1f天" % (
        now.strftime("%Y-%m-%d %H:%M"),
        fp,
        datetime.fromtimestamp(iat, BJ).strftime("%m-%d %H:%M") if iat else "-",
        datetime.fromtimestamp(exp, BJ).strftime("%m-%d %H:%M"),
        (exp - now.timestamp()) / 86400)
    prev_fp = None
    if LOG.exists():
        for ln in reversed(LOG.read_text(encoding="utf-8").splitlines()):
            if "fp=" in ln:
                prev_fp = ln.split("fp=")[1].split()[0]; break
    changed = prev_fp is not None and prev_fp != fp
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + ("  [CHANGED! 发生了token刷新]" if changed else "") + "\n")
    print(line + ("  [CHANGED! 发生了token刷新, 上一次指纹=%s]" % prev_fp if changed else
                  "  (与上次相同, 未刷新)" if prev_fp else "  (首次观测)"))

if __name__ == "__main__":
    main()
