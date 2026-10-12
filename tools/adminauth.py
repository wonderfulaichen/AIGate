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


def headers(data_dir):
    """返回可直接并进 Request 的头字典; 无 token 时为空字典。"""
    try:
        tok = io.open(os.path.join(data_dir, ".admin_token"),
                      encoding="utf-8", errors="replace").read().strip()
    except Exception:
        return {}
    return {"Authorization": "Bearer " + tok} if tok else {}
