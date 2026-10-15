# -*- coding: utf-8 -*-
"""隔离实例 /admin/api/* 的 Bearer 头 (2026-10-12 起接口整体要求鉴权)。

用法 (脚本在 tools/ 下, `python tools/xxx.py` 时 tools/ 即 sys.path[0], 可直接 import):

    from adminauth import headers
    urllib.request.Request(url, headers=headers(iso_data_dir))

token 由网关在启动时生成在 `<data_dir>/.admin_token`, 所以**每次调用时才读**
(导入期读一次会拿到空 —— e2e_accounts.py 踩过这个坑)。
读不到时返回 {}: 老 exe / AIGATE_OPEN_ADMIN=1 的实例不需要 token, 不该被挡住。
"""
import io
import os
import json
import urllib.request
import urllib.error


def headers(data_dir):
    """返回可直接并进 Request 的头字典; 无 token 时为空字典。"""
    try:
        tok = io.open(os.path.join(data_dir, ".admin_token"),
                      encoding="utf-8", errors="replace").read().strip()
    except Exception:
        return {}
    return {"Authorization": "Bearer " + tok} if tok else {}


def _req(method, url, data=None, hdrs=None, timeout=60):
    h = dict(hdrs or {})
    if data is not None:
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        body = data if isinstance(data, bytes) else data.encode("utf-8")
    else:
        body = None
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except Exception:
                # 流式 (SSE) 响应 / 非 JSON 体: 原样返回, 调用方按需解析.
                return {"_raw": raw, "status": resp.status}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        try:
            return json.loads(raw)
        except Exception:
            return {"error": raw or str(e.code), "status": e.code}


def get_json(url, hdrs=None, timeout=60):
    return _req("GET", url, hdrs=hdrs, timeout=timeout)


def post_json(url, data=None, hdrs=None, timeout=60):
    return _req("POST", url, data=data, hdrs=hdrs, timeout=timeout)

