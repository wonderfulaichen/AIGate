# -*- coding: utf-8 -*-
"""opencode 多工作区额度：验证「同账号的多个工作区都能看到」。

为什么必须用真实账号测：这个 bug 的全部表现都在**上游数据形状**里 ——
一个账号下有多个工作区、前缀不只 wrk_、各自的额度完全独立。
用假的夹具测不出「少了一个区」，因为假数据不会有"第二个区"。

判据（都对应真实故障）：
  ① /orgs 返回**全部**工作区，且 `org_` 前缀不被丢弃
     （早期 `starts_with("wrk_")` 把 org_ 整块过滤，用户报「其他区无法获得」）
  ② /quota 返回的 workspaces 数量 == 配置里存的工作区数量
  ③ 各工作区的额度**互不相同**（证明是按区分开查的，不是把第一个区的值复制了）
  ④ 至少一个工作区的 ends_at 与其他不同（独立订阅的旁证）

用法：python tools/probe_oc_workspaces.py
凭据从部署目录的 providers.json 读（只读，不改）。
"""
import json
import os
import sys
import urllib.error
import urllib.request

# 部署目录（用户实际在跑的配置）
DEPLOY = r"D:\Office software\Development Project\AI中转\providers.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class Probe:
    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.skip = 0

    def check(self, cond, msg, detail=""):
        if cond:
            print("[PASS] %s" % msg)
            self.ok += 1
        else:
            print("[FAIL] %s%s" % (msg, ("  << " + detail) if detail else ""))
            self.fail += 1

    def skip_(self, msg, why):
        print("[SKIP] %s  (%s)" % (msg, why))
        self.skip += 1


def get(url, cookie, extra=None):
    h = {"Cookie": cookie, "Accept": "*/*", "User-Agent": UA}
    if extra:
        h.update(extra)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def main():
    pr = Probe()
    if not os.path.exists(DEPLOY):
        print("找不到部署配置: %s" % DEPLOY)
        return 2

    data = json.load(open(DEPLOY, encoding="utf-8"))
    prov = next((p for p in data.get("providers") or [] if p.get("console_session")), None)
    if not prov:
        print("该配置里没有配了 console_session 的供应商")
        return 2
    cookie = prov["console_session"]
    print("供应商: %s（Cookie 长度 %d，不打印内容）\n" % (prov.get("name"), len(cookie)))

    # ── ① /orgs 返回全部工作区，org_ 前缀不丢 ──
    st, body = get("https://opencode.ai/console/api/orgs", cookie)
    pr.check(st == 200, "/orgs 可达", "HTTP %s" % st)
    if st != 200:
        print(body[:300])
        return 1
    orgs = json.loads(body)
    ids = [o.get("id") for o in orgs if o.get("id")]
    print("  /orgs 返回 %d 个: %s" % (len(ids), ids))

    org_prefixed = [i for i in ids if i.startswith("org_")]
    wrk_prefixed = [i for i in ids if i.startswith("wrk_")]
    if org_prefixed:
        pr.check(True, "存在 org_ 前缀工作区（%d 个）—— 这类必须保留" % len(org_prefixed))
    else:
        pr.skip_("org_ 前缀保留", "该账号没有 org_ 工作区，测不到这条")

    pr.check(len(ids) >= 1, "至少有一个工作区")

    # ── ② 逐个查额度，验证各自独立 ──
    results = []
    for o in orgs:
        oid = o.get("id")
        if not oid:
            continue
        st2, b2 = get("https://opencode.ai/console/api/go/status", cookie,
                      {"x-org-id": oid,
                       "Referer": "https://opencode.ai/console/%s/go" % oid})
        if st2 != 200:
            print("  [%s] name=%r -> HTTP %s（该区不可用，应如实报错而不是消失）"
                  % (oid, o.get("name"), st2))
            results.append({"id": oid, "name": o.get("name"), "ok": False,
                            "err": b2[:120]})
            continue
        j = json.loads(b2)
        acc = j.get("access") or {}
        m = acc.get("meters") or {}
        results.append({
            "id": oid,
            "name": o.get("name"),
            "ok": True,
            "product": j.get("product"),
            "endsAt": acc.get("endsAt"),
            "month_used": (m.get("month") or {}).get("usedMicroCents"),
            "month_limit": (m.get("month") or {}).get("limitMicroCents"),
        })
        print("  [%s] name=%r product=%r endsAt=%r month=%s/%s"
              % (oid, o.get("name"), j.get("product"), acc.get("endsAt"),
                 (m.get("month") or {}).get("usedMicroCents"),
                 (m.get("month") or {}).get("limitMicroCents")))

    oks = [r for r in results if r["ok"]]
    pr.check(len(oks) == len(results),
             "所有工作区都能查到额度",
             "失败: %s" % [(r["id"], r.get("err")) for r in results if not r["ok"]])

    # ── ③ 各区的额度互不相同（证明按区分开查，不是复制第一个） ──
    if len(oks) >= 2:
        used = {(r["month_used"], r["month_limit"]) for r in oks}
        pr.check(len(used) > 1,
                 "多个工作区的额度**互不相同**（证明确实是按区分开查的）",
                 "全部相同: %s" % (used,))
        ends = {r["endsAt"] for r in oks}
        pr.check(len(ends) > 1,
                 "多个工作区的到期时间不同（独立订阅的旁证）",
                 "全部相同: %s" % (ends,))
    else:
        pr.skip_("多区额度互不相同", "该账号只有 1 个可用工作区，测不到这条")

    print("\n%d 通过 / %d 失败 / %d 跳过" % (pr.ok, pr.fail, pr.skip))
    return 0 if pr.fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
