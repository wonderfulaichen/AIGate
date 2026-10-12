# -*- coding: utf-8 -*-
"""CodeBuddy(buddy) 真实凭据的两级来源 (2026-10-12 抽公共, 同 probe_buddy_v3config 口径)。

① 网关的 data/accounts.json —— 账号化之后 token 落在那里 (部署目录 data/ 或仓库 data/),
   条目可能是**数组**(多账号) 取第一个, 已是规范化形态 (expires_at 秒);
② 老路 %TEMP%\\cb_plain.json (账号化之前由 IDE 解密导出, 早已不存在 —— 只作兜底,
   expiresAt 是**毫秒**, 这里统一折成秒)。

返回 (cred_dict, source_path); cred_dict = {"access_token", "refresh_token", "expires_at"}
(expires_at/refresh_token 可能为 None); 都找不到返回 (None, None)。
"""
import json
import os
import tempfile


def load_buddy_cred():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "accounts.json")
    # 部署目录的 data (线上实例的账号表); 文件名含中文, 不用通配
    alt = r"D:\Office software\Development Project\AI中转\data\accounts.json"
    for cand in (alt, p):
        if not os.path.exists(cand):
            continue
        try:
            j = json.load(open(cand, encoding="utf-8"))
        except Exception:
            continue
        ent = j.get("buddy")
        if isinstance(ent, list):
            ent = ent[0] if ent else None
        if isinstance(ent, dict) and ent.get("access_token"):
            return {"access_token": ent["access_token"],
                    "refresh_token": ent.get("refresh_token"),
                    "expires_at": ent.get("expires_at")}, cand
    cb = os.path.join(tempfile.gettempdir(), "cb_plain.json")
    if os.path.exists(cb):
        try:
            a = json.load(open(cb, encoding="utf-8"))["auth"]
            exp = a.get("expiresAt")
            return {"access_token": a["accessToken"],
                    "refresh_token": a.get("refreshToken"),
                    "expires_at": int(exp) // 1000 if exp else None}, cb
        except Exception:
            return None, None
    return None, None
