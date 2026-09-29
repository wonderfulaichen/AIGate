# -*- coding: utf-8 -*-
"""AIGate 产品演示 (单文件 HTML) —— 含视觉系统 / 动效 / 设计取舍讲解。

⚠️ **本脚本是 `docs/product-deck.html` 的唯一来源**: 产物不许手改 —— 手改的改动会在
   下一次重跑时被覆盖 (2026-09-30 踩过: 演示稿的 Apple 化是直接改在产物上的, 生成器
   没同步, 直到把 CSS/JS/标/装饰/文案整体回灌进来才对上)。改样式请改这里。

本次三项改进 (对应三条反馈):
  ① **美术**: SVG 图示 (架构图 / 流向动画 / 环形指标)、分层阴影、字阶对比拉开;
     配色自 2026-09-30 起与面板 / 落地页**同源** —— Apple 语义 token (见 CSS 的 :root,
     与 src/admin/base.css、site/index.html 三处必须一起改), 不再用 indigo→violet→cyan 三色系。
  ② **动效**: 入场错峰 (子元素逐个浮现)、数字滚动计数、流向虚线流动、图表描边绘制、
     卡片悬停抬升、进度条脉动。全部由 .on 类触发 → 每次翻到该页重放。
  ③ **原理**: 原理页从"罗列条目"改为**设计取舍四段式** ——
     朴素做法 → 为什么不行 → 我们的做法 → 代价与边界。这才是能讲的内容。

氛围层只有两层: 环境光 canvas + 章节纹理 (.fx-tex)。原第三层"跟随鼠标的光晕"
已于 2026-09-30 按 docs/frontend.md §4.1b 移除 (小范围高 alpha = 光斑, 不是环境光)。

数据来源 (实测): 真实请求日志, 由 tools/savings_audit.py 统计并写出 savings_audit.json,
本脚本**读取该 JSON** 而非写死数字 —— 日志增长后重跑审计即可让演示自动跟上
(此前写死的数字已过期过: 3.84 亿 → 4.03 亿, 233 个模型 → 652 个)。
"""
import base64, io, json, os, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根 —— 本脚本在 tools/ 下, 上溯两级
TOOLS = os.path.dirname(os.path.abspath(__file__))                   # 本脚本目录: 4 个口径快照 JSON 与脚本同目录
SHOTS = os.path.join(ROOT, ".openbitfun", "tmp", "shots")
OUT = os.path.join(ROOT, "docs", "product-deck.html")


def shot(name):
    from PIL import Image
    p = os.path.join(SHOTS, name)
    if not os.path.exists(p):
        return None
    im = Image.open(p).convert("RGB")
    w = min(im.width, 1500)
    im = im.resize((w, int(im.height * w / im.width)), Image.LANCZOS)
    im = im.crop((0, 0, min(im.width, w), min(im.height, int(w * 10 / 16))))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


IMG = {n: shot(n) for n in ["s_dashboard.png", "s_light_dashboard.png", "s_analytics.png",
                            "s_drawer.png", "s_providers.png", "s_records.png"]}

# 真实「剥离前/后」对照数据 —— 由 capture_strip_diff.py 起隔离实例 + mock 上游抓取,
# 不是手写示意 (演示里的数字与字段必须有出处)。缺失则相关页跳过而不是编造。
DIFF = None
_diff_path = os.path.join(TOOLS, "strip_diff.json")
if os.path.exists(_diff_path):
    DIFF = json.load(io.open(_diff_path, encoding="utf-8"))

# 实测省量数字 —— 与 savings_audit.py 同源 (由它写出 JSON, 本脚本读取)。
# 为什么不再写死: 日志会持续增长, 写死的数字会悄悄过期 —— 已踩过一次
# (稿子里 3.84 亿 / 233 个模型, 实际已 4.03 亿 / 652 个)。缺失时用回退值并在末尾告警。
AUDIT = None
_audit_path = os.path.join(TOOLS, "savings_audit.json")
if os.path.exists(_audit_path):
    AUDIT = json.load(io.open(_audit_path, encoding="utf-8"))

# 真实多轮会话增长曲线 —— 由 capture_multiturn.py 跑 10 轮会话抓取 (开/关剥离各一遍)。
MT = None
_mt_path = os.path.join(TOOLS, "multiturn.json")
if os.path.exists(_mt_path):
    MT = json.load(io.open(_mt_path, encoding="utf-8"))

# 省量分布形状 —— 由 savings_shape.py 统计真实日志 (集中度 / 量级分层)。
SHAPE = None
_shape_path = os.path.join(TOOLS, "savings_shape.json")
if os.path.exists(_shape_path):
    SHAPE = json.load(io.open(_shape_path, encoding="utf-8"))


def _fmt_yi(n):
    """亿为单位的紧凑写法 (如 4.03)。"""
    return "%.2f" % (n / 1e8)


def _fb(key, fallback):
    if AUDIT and key in AUDIT:
        return AUDIT[key]
    print("  ⚠️ 审计数据缺失 (%s), 回退到内置值 %r —— 请先跑 savings_audit.py" % (key, fallback))
    return fallback


N_LOGS = _fb("logs", 5000)
N_SAVED_YI = _fmt_yi(_fb("total_saved", 403813372))
# 「剥离集中在哪一层」这个占比, 全稿必须**同源**。注意两个 JSON 的算法口径不同:
#   savings_audit.json  toolcall_share_pct = strip_toolcall / total_saved      → 97.53%
#   savings_shape.json  toolcall_share     = toolcall_tokens / saved_total     → 97.69%
# (两者的"省量总量"分母本身差约 1 千万 tokens)。**以 audit 为准** —— 对外口径见 HANDOFF §5.3,
# 且 fd2bfac 就是按 audit 把演示稿里这几处从 97.7% 改回 97.5% 的。别再改用 shape 的那个字段。
N_TOOLCALL_SHARE = "%.1f" % _fb("toolcall_share_pct", 97.5)
N_PLAIN_SHARE = "%.1f" % (100 - _fb("toolcall_share_pct", 97.5))
N_STRIP_TC_YI = _fmt_yi(_fb("strip_toolcall", 393848629))
N_RATIO = "%.0f" % _fb("in_out_ratio", 379.5)
N_HIT = "%.1f" % _fb("hit_rate_pct", 98.8)
N_SAVED_SHARE = "%.1f" % _fb("saved_share_of_input_pct", 24.0)
N_BENEFIT = "%.1f" % _fb("req_with_savings_pct", 65.4)
N_IN_YI = _fmt_yi(_fb("prompt_tokens", 1684308622))
N_OUT_WAN = "%.0f" % (_fb("completion", 4438135) / 1e4)

S = []

# ═══ 1 封面 ═══
S.append(("cover", dict(
    eyebrow="LOCAL LLM GATEWAY",
    title="AIGate",
    claim="把重复的上下文，<br>不再重复传一遍",
    sub="一个地址接入所有编程工具，一个面板管住所有模型供应商",
    facts=[(N_RATIO + " : 1", "输入 : 输出（实测）"),
           (N_SAVED_SHARE + "%", "省下输入总量的比例"),
           ("3 协议", "OpenAI / Anthropic / Responses"),
           ("单文件", "一个 exe，零外部依赖")],
)))

# ═══ 2 痛点 ═══
S.append(("pain", dict(
    h="上下文在重复搬运，却看不出重复在哪",
    lead="先用数据看清请求体的结构 —— 这决定了该优化什么",
    pains=[
        ("输入是输出的 " + N_RATIO + " 倍",
         "实测 " + str(N_LOGS) + " 次请求：输入 " + N_IN_YI + " 亿 tokens，输出仅 " + N_OUT_WAN + " 万。"
         "大头从来不是「模型想了多久」，而是「每次重发了多少历史」"),
        (N_HIT + "% 的输入命中缓存", "也就是说：这些内容<b>本来就在被反复重发</b>。"
                                "优化方向不是少发请求，而是<b>别把重复内容再传一遍</b>"),
        ("却看不见重复在哪", "客户端只告诉你发了多少，不告诉你请求体里什么在重复；"
                          "换一个供应商，一切又从零开始猜"),
        ("协议与配置还得各配一遍", "OpenAI / Anthropic / Responses 三种协议并存，"
                                  "每个客户端单独配 base_url 与密钥"),
    ],
)))

# ═══ 3 核心数字 ═══
S.append(("nums", dict(
    h="省下来的，是重复的上下文",
    lead="本产品的第一目标：<b>不破坏上游缓存，同时剥掉真正冗余的部分</b>",
    nums=[(N_RATIO, "倍", "输入 ÷ 输出<br>大头在重复搬运"),
          (N_HIT, "%", "输入命中 KV 缓存<br>本就是重复内容"),
          (N_SAVED_YI, "亿", "剥离省下的 tokens<br>占输入总量 " + N_SAVED_SHARE + "%"),
          (N_BENEFIT, "%", "从中受益的请求占比")],
    foot="省下的 token 是<b>实在的</b>：不论上游怎么变，少发的 tokens 就不必再传输一遍。"
         "这也是为什么面板把省量<b>按 token 逐项记账</b> —— 剥离 / 裁剪 / 缓存三类分开，看得见每一项省了多少。",
)))

# ═══ 4 架构 ═══
S.append(("flow", dict(
    h="把复杂性收进一个本地网关",
    lead="客户端只认一个地址；协议、路由、计量、韧性，全在网关里解决",
    left_title="你的编程工具", left=["Cursor", "Claude Code", "Trae / 其他"],
    mid="AIGate", mid_sub="本地 · 单文件 · 零外部依赖",
    right_title="模型供应商", right=["OpenAI 兼容", "Anthropic", "自建 / 中转"],
    foot="任意客户端 × 任意供应商，只配一次",
)))

# ═══ 协议匹配 (合并: 结论 + 取舍, 原两页压成一页) ═══
S.append(("proto", dict(
    h="协议由客户端决定，不由网关替你选",
    lead="客户端打哪个入口，就用哪个协议转发 —— 上游本来就支持就直传，不支持才转换",
    cols=["客户端入口", "网关行为", "发往上游"],
    rows=[("<code>/v1/chat/completions</code>", "命中即直传", "<code>/chat/completions</code>"),
          ("<code>/v1/messages</code>", "命中即直传", "<code>/messages</code>"),
          ("<code>/v1/responses</code>", "命中即直传", "<code>/responses</code>")],
    fold=[
        ("为什么不让你手动勾", "让配置者逐个模型勾选支持什么协议",
         "真实配置里 <b>652 个模型无一勾选</b> —— 功能等于不存在"),
        ("为什么也不靠猜", "按模型名推断（claude-* → anthropic）",
         "同名模型<b>跨服务方协议不同</b>：<code>claude-opus-4-8</code> 在官方走 anthropic、"
         "在转售商走 openai"),
        ("没证据时怎么办", "没有任何证据说上游不支持 → 尊重客户端的选择",
         "宁可让上游回一个诚实的错误，也不要静默换协议（后者会掩盖真实失败点）"),
    ],
    note="「直传」= 请求体<b>逐字节保真</b>：thinking 块与 signature、reasoning item 都不会被往返翻译掉。"
         "反之，<b>转换必有代价</b> —— 这也是为什么 responses 直通路径的省量恒为 0（不经过剥离）。",
)))

# ═══ 7 能力 ═══
S.append(("grid", dict(
    h="核心能力",
    lead="以省量为主线，其余三项都是为它服务",
    cards=[("↓", "省量（主线）", "剥离推理链、历史裁剪、响应缓存 —— 且<b>不破坏上游 KV 缓存</b>",
            True),
           ("↔", "协议自动匹配", "客户端入口即意图；命中直传，字段不被往返翻译", False),
           ("◇", "模型别名与共存", "中转 ID 与上游真名解耦；同名模型跨供应商各自成路由", False),
           ("◷", "可观测与韧性", "省量逐项可见；熔断、退避重试、超时与体积上限", False)],
)))

# ═══ 7b 为什么可以剥 (概念: 过程 ≠ 结果) ═══
# 这是原先缺的一环: 前面讲了"推理链越长越大、省了多少、删了什么", 却没讲**为什么删它是合理的**。
# 依据在代码注释 (proxy.rs: 推理链"无推理价值, 不应被喂回模型")。
S.append(("why", dict(
    h="推理链是「过程」，不是「结果」",
    lead="客户端的做法是「把上一轮的全部回传」—— 但模型需要的从来是结果，不是自己上次怎么想的",
    keep=["用户的提问", "助手的最终回答", "工具调用的入参与返回",
          "system 提示词"],
    drop=["assistant 的 <code>reasoning_content</code> / <code>reasoning</code>",
          "Responses 协议里独立的 <code>reasoning</code> item",
          "<code>reasoning_details</code>（结构化思考）"],
    note="推理链只在<b>生成本轮回答的那一刻</b>有用；回答已经产出，它的使命就结束了。"
         "把它回传给下一轮，既<b>不改善回答质量</b>，又让请求体随轮次线性增长 —— "
         "这就是「重复搬运」的主体。",
)))


# ═══ 8 多轮累积 (原理: 为什么重复上下文是大头) —— 真实实测曲线 ═══
# 数据由 capture_multiturn.py 跑 10 轮 agent 会话抓取: 同一会话在「开剥离 / 关剥离」
# 两种设置下各跑一遍, 记录**上游实际收到的请求体体积**。原先那版是按轮次建模的示意柱图,
# 标注了"示意"但说服力弱 —— 现在换成实测双曲线。
if MT:
    S.append(("curve", dict(
        h="推理链越滚越大，而每轮都要重发",
        lead="实测 10 轮会话：客户端发出的上下文一路增长，网关剥掉历史推理链后把它压平",
        without=MT["without"], with_strip=MT["with_strip"], final=MT["final"],
    )))

# ═══ 8b 省量形状 (真实分布: 为什么值得做) ═══
if SHAPE:
    S.append(("shape", dict(
        h="省掉的量级有多大",
        lead="真实请求日志的省量分布 —— 单请求量级、占输入比重、以及汇聚到哪一层",
        s=SHAPE,
    )))

# ═══ 9 剥离前后对照 (真实数据) ═══
if DIFF:
    S.append(("diff", dict(
        h="剥离前后：结构不变，推理链消失",
        lead="同一个多轮会话，网关转发前后的真实对照（隔离实例 + mock 上游抓取，非示意）",
        before=DIFF["before"], after=DIFF["after"], detail=DIFF["detail"],
        saved_bytes=DIFF["saved_bytes"], est=DIFF["strip_saved_tokens_est"],
    )))

# ═══ 9b 剥离的判定 (机制: 什么情况剥、什么情况绝不剥) ═══
# 这一环原先只埋在"省量手段"页的一大段文字里 —— 而它才是"怎么工作"的核心。
# 判定依据全部取自代码 (proxy.rs::resolve_strip_toolcall):
#   anthropic 优先于模型级开关 > 模型级显式 > 协议白名单(chat 默认开 / responses 默认关)。
S.append(("decide", dict(
    h="剥不剥，先看协议，再看层",
    lead="判据是<b>上游协议</b>而非供应商 —— 供应商只是换 endpoint，协议才决定剥离是否安全",
    rows=[
        ("Anthropic 协议", "thinking 块必须连签名回传，剥了上游必回 400。"
                           "这条<b>优先于</b>模型级开关 —— 宁可少省，也不能造出必然失败的请求", "never"),
        ("chat 协议 · 普通 assistant", "默认开启。这是省量的基本盘", "yes"),
        ("chat 协议 · 带 tool_calls", f"默认开启（实测安全）—— 这也是省量的<b>大头（{N_TOOLCALL_SHARE}%）</b>", "yes"),
        ("Responses 协议 · reasoning item", "与相邻 function_call 的配对关系<b>未经实测</b>，"
                                            "剥掉可能因序列不合法而 400 —— 交用户在真实上游试开", "maybe"),
        ("响应缓存 / 历史裁剪", "裁剪默认 <b>0 = 关</b>（优先保上下文）；缓存只对非流式生效", "maybe"),
    ],
    note="三种协议、两层结构、一个总闸 —— 判定顺序不可颠倒：<b>协议安全的红线优先于一切省量</b>。",
)))


# ═══ 11 省量手段 (讲"是什么"; 判据见前一页) ═══
S.append(("stats", dict(
    h="三种手段，各管一段",
    lead="剥离是主力，另两项按需开启 —— 判据上一页讲过，这里只说各自<b>省的是哪一块</b>",
    items=[
        ("剥离推理链", "<b>省的是「过程」</b>：把历史里累积的推理链摘掉。"
                       f"这是省量的绝对主力 —— 实测占剥离总量的 <b>{N_TOOLCALL_SHARE}%</b>。"
                       "字段层面涉及 <code>reasoning_content</code> / <code>reasoning</code> / "
                       "<code>reasoning_details</code>"),
        ("历史裁剪", "<b>省的是「过早的轮次」</b>：只保留最近 N 个 user 轮，更早的整段丢弃 —— "
                     "连带的工具链一起走，不会把一轮切一半。默认 <b>0 = 不裁剪</b>，"
                     "因为丢历史会改变模型能看到的上下文"),
        ("响应缓存", "<b>省的是「完全一样的请求」</b>：非流式请求若字节级重复，"
                     "直接本地回放，<b>零 token</b> 到上游。对同一条问题反复问的场景最有效"),
    ],
    note="三者不叠加成「省得更多」就完事 —— 每一项都有明确的代价：剥离丢思考过程、"
         "裁剪丢历史、缓存只对非流式生效。所以默认只开<b>代价最可控</b>的那一项。",
)))

# ═══ 9 省量取舍 ═══
S.append(("tradeoff", dict(
    h="剥离到什么程度，才不至于帮倒忙",
    lead="剥离是<b>有损且不可逆</b>的 —— 所以边界要靠判据划，不能一刀切",
    rows=[
        ("粗暴做法", "把所有历史里的推理链都剥掉，省得最多",
         "推理链与 <code>tool_calls</code> 常常是<b>配对</b>关系：客户端（或上游）要求二者并存时，"
         "剥了就得到 400 —— 省下的 token 还不够赔一次失败重试"),
        ("另一种做法", "干脆不剥，保持请求体完全不变",
         "最稳，但白白多付那部分推理链的传输费。实测这类内容在多轮会话里占比很高，"
         "且<b>每轮都会重发</b>"),
        ("我们的做法", "分两层，且按<b>上游协议</b>判定（不是按供应商 —— 同一家常同时提供三种协议）："
                        "普通 assistant 推理链默认剥；带 <code>tool_calls</code> 的走协议开关；"
                        "<b>Anthropic 恒不剥</b>",
         "既拿到省量，又不制造必然失败的请求。<code>anthropic_mode</code> 那条<b>优先于</b>模型级开关 —— "
         "宁可少省，也不能因为误勾而让请求必挂"),
        ("为什么按协议而非供应商", "供应商只是换 endpoint，<b>协议</b>才决定剥离是否安全",
         "实测反例：<code>claude-opus-4-8</code> 在官方走 anthropic、在转售商走 openai —— "
         "按供应商开关必然误判"),
        ("代价与边界", "剥离有损且不可逆",
         "可选开启「降级可回取」：把被剔离的原文留档，事后可核查「这次到底删了什么」（默认关，因含思考明文）"),
    ],
)))

# ═══ 界面 ═══
S.append(("gallery", dict(
    h="界面：暗色与亮色，同一套设计语言",
    lead="首帧即正确，不会先闪一下暗色；主题在顶栏随时切换",
    pics=[("s_dashboard.png", "概览：省量、关键指标与趋势"),
          ("s_light_dashboard.png", "同一页面的亮色主题")],
)))
S.append(("gallery", dict(
    h="省了多少，逐项可见",
    lead="三类省量分别记账，与命中率、上下文规模同屏对照",
    pics=[("s_records.png", "请求记录：状态 / 延迟 / token / 缓存命中"),
          ("s_analytics.png", "用量分析：趋势、分布、模型与供应商排名")],
)))
S.append(("gallery", dict(
    h="供应商与模型，一处管住",
    lead="卡片与表格双视图；拉取模型时自动探测上游支持的协议",
    pics=[("s_providers.png", "供应商列表：状态、密钥、协议一览"),
          ("s_drawer.png", "模型明细：别名、思考强度、协议")],
)))

# ═══ 16 架构实现 ═══
S.append(("single", dict(
    h="一个二进制，扔到机器上就能跑",
    lead="为了部署简单，刻意把技术面做窄",
    bullets=[
        "前端（HTML / CSS / JS）在编译期拼进可执行文件，<b>不依赖 Node，也不需要静态文件目录</b>",
        "<code>providers.json</code> 描述供应商与模型；密钥走环境变量或面板管理，不落明文",
        "改端口或数据目录即可跑出互不干扰的多个实例",
        "上游调用是异步非阻塞的：单个慢上游不会拖住其他供应商的请求",
    ],
    badge="Rust · axum · 单进程异步",
)))

# ═══ 17 上手 ═══
S.append(("steps", dict(
    h="三步开始使用",
    steps=[("1", "拿到 exe", "从爱发电获取，或自行 <code>cargo build --release</code>"),
           ("2", "启动", "直接运行 <code>AIGate.exe</code>，浏览器打开面板完成初始化"),
           ("3", "改客户端", "把编程工具的 API Base 指向 <code>http://127.0.0.1:8787/v1</code>")],
    foot="下载：afdian.com/item/6043ebfa923111f1854d5254001e7c00 · 仓库：github.com/wonderfulaichen/AIGate",
)))


# ── Markdown 粗体 ──
def md(s):
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=re.S)


def render(kind, k):
    if kind == "cover":
        facts = "".join(
            f'<div class="f"><span class="fv">{a}</span><span class="fl">{b}</span></div>'
            for a, b in k["facts"])
        return f"""<section class="s cover">
  <div class="cv">
    <div class="eyebrow">{k['eyebrow']}</div>
    <div class="logo"><svg viewBox="0 0 256 256"><rect x="8" y="8" width="240" height="240" rx="56" fill="#0a84ff"/><path d="M148 56 H52 V200 H148" fill="none" stroke="#ffffff" stroke-width="20" stroke-linecap="round" stroke-linejoin="round"/><path d="M104 112 H176 V104 L220 128 L176 152 V144 H104 Z" fill="#ffffff"/></svg></div>
    <h1>{k['title']}</h1>
    <p class="claim">{k['claim']}</p>
    <p class="sub">{k['sub']}</p>
    <div class="facts">{facts}</div>
  </div></section>"""

    if kind == "pain":
        cards = "".join(
            f'<div class="pain"><span class="pn">{i+1}</span><div><h4>{md(a)}</h4><p>{md(b)}</p></div></div>'
            for i, (a, b) in enumerate(k["pains"]))
        return f'<section class="s"><h2>{md(k["h"])}</h2><p class="lead">{md(k["lead"])}</p><div class="pain-grid">{cards}</div></section>'

    if kind == "nums":
        # **初值即真实数字**: 计数动画只负责"从 0 数上来"。若把初值写成 0 再靠 JS 填,
        # 那么 JS 未执行(禁用/打印/截图/后台标签)时页面会显示 0 —— 比没有动画更糟。
        cells = "".join(
            f'<div class="nm"><div class="nmv"><span class="cnt" data-to="{v}">{v}</span>'
            f'<i>{u}</i></div><div class="nml">{md(d)}</div></div>' for v, u, d in k["nums"])
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <div class="num-row">{cells}</div><p class="foot-note">{md(k['foot'])}</p></section>"""

    if kind == "flow":
        L = "".join(f'<span class="node">{x}</span>' for x in k["left"])
        R = "".join(f'<span class="node">{x}</span>' for x in k["right"])
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <div class="flow">
    <div class="fcol"><div class="fcol-t">{k['left_title']}</div>{L}</div>
    <div class="wire"><svg viewBox="0 0 80 12" preserveAspectRatio="none"><path d="M0 6 H68" class="wireline"/><path d="M64 1 L74 6 L64 11" class="wirehead"/></svg></div>
    <div class="hub"><div class="hub-ico"><svg viewBox="0 0 256 256"><rect x="8" y="8" width="240" height="240" rx="56" fill="#0a84ff"/><path d="M148 56 H52 V200 H148" fill="none" stroke="#ffffff" stroke-width="20" stroke-linecap="round" stroke-linejoin="round"/><path d="M104 112 H176 V104 L220 128 L176 152 V144 H104 Z" fill="#ffffff"/></svg></div>
      <div class="hub-n">{k['mid']}</div><div class="hub-s">{k['mid_sub']}</div></div>
    <div class="wire"><svg viewBox="0 0 80 12" preserveAspectRatio="none"><path d="M0 6 H68" class="wireline"/><path d="M64 1 L74 6 L64 11" class="wirehead"/></svg></div>
    <div class="fcol"><div class="fcol-t">{k['right_title']}</div>{R}</div>
  </div><p class="foot-note">{md(k['foot'])}</p></section>"""

    if kind == "proto":
        # 结论表在上、设计取舍在下 —— 原先是两页, 现压成一页 (协议不是产品主线, 一页足够)。
        head = "".join(f"<th>{c}</th>" for c in k["cols"])
        rows = "".join(f'<tr><td>{a}</td><td class="ok">{b}</td><td>{c}</td></tr>' for a, b, c in k["rows"])
        fold = ""
        for label, what, why in k["fold"]:
            fold += (f'<div class="pf"><div class="pf-k">{md(label)}</div>'
                     f'<div class="pf-v">{md(what)}<span class="pf-o">{md(why)}</span></div></div>')
        return f"""<section class="s proto"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <table class="matrix compact"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>
  <div class="pf-list">{fold}</div>
  <p class="foot-note">{md(k['note'])}</p></section>"""

    if kind == "matrix":
        head = "".join(f"<th>{c}</th>" for c in k["cols"])
        rows = "".join(f'<tr><td>{a}</td><td class="ok">{b}</td><td>{c}</td></tr>' for a, b, c in k["rows"])
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <table class="matrix"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>
  <p class="foot-note">{md(k['note'])}</p></section>"""

    if kind == "tradeoff":
        # 每行 = (标签, 做法说明, 结果/代价...)。结果另起一行并加左边框强调 ——
        # "为什么不行"正是这一页要讲的重点, 不能和做法混在一句里。
        rows = ""
        for row in k["rows"]:
            label, what = row[0], row[1]
            outs = [x for x in row[2:] if x]
            cls = "tr hi" if "我们" in label else "tr"
            body = f'<div class="tr-w">{md(what)}</div>'
            if outs:
                body += "".join(f'<div class="tr-o">{md(o)}</div>' for o in outs)
            rows += f'<div class="{cls}"><div class="tr-k">{md(label)}</div><div class="tr-v">{body}</div></div>'
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <div class="tr-list">{rows}</div></section>"""

    if kind == "grid":
        cards = ""
        for ic, t_, d, core in k["cards"]:
            cards += (f'<div class="gc{" core" if core else ""}"><div class="gci">{ic}</div>'
                      f'<h4>{md(t_)}</h4><p>{md(d)}</p></div>')
        lead = f'<p class="lead">{md(k["lead"])}</p>' if k.get("lead") else ""
        return f'<section class="s"><h2>{md(k["h"])}</h2>{lead}<div class="grid-2">{cards}</div></section>'

    if kind == "decide":
        # 判定表: 每行 = 一种情形 + 判定徽章 + 理由。徽章颜色即语义 (绝不剥 / 剥 / 可选)。
        badge = {"never": ("绝不剥", "bd-never"), "yes": ("剥", "bd-yes"),
                 "maybe": ("可选", "bd-maybe")}
        rows = ""
        for cond, why, cls in k["rows"]:
            txt, bcls = badge[cls]
            rows += (f'<div class="dc"><div class="dc-c">{md(cond)}</div>'
                     f'<div class="dc-v"><span class="bdg2 {bcls}">{txt}</span></div>'
                     f'<div class="dc-w">{md(why)}</div></div>')
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <div class="dc-list">{rows}</div>
  <div class="honest">{md(k['note'])}</div></section>"""

    if kind == "why":
        keep = "".join(f'<li>{md(x)}</li>' for x in k["keep"])
        drop = "".join(f'<li>{md(x)}</li>' for x in k["drop"])
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <div class="why">
    <div class="wy-col keep"><div class="wy-h"><span class="wy-b">✓</span>必须回传</div>
      <ul>{keep}</ul></div>
    <div class="wy-col drop"><div class="wy-h"><span class="wy-b">−</span>可以剥掉</div>
      <ul>{drop}</ul></div>
  </div>
  <div class="honest">{md(k['note'])}</div></section>"""

    if kind == "curve":
        # 双折线: 不剥离 (暖色, 一路上扬) vs 剥离后 (主色, 压平) —— 用 SVG 画真实数据点。
        wo, ws = k["without"], k["with_strip"]
        W, H, PL, PR, PT, PB = 1000, 300, 74, 130, 26, 46
        n = max(len(wo), 1)
        peak = max([d["recv_bytes"] for d in wo] + [1])

        def px(i):
            return PL + (W - PL - PR) * (i / max(n - 1, 1))

        def py(v):
            return PT + (H - PT - PB) * (1 - v / peak)

        def path(series):
            pts = [(px(i), py(d["recv_bytes"])) for i, d in enumerate(series)]
            return " ".join(("M" if i == 0 else "L") + "%.1f %.1f" % p for i, p in enumerate(pts))

        grid = ""
        for gv in (0, 0.25, 0.5, 0.75, 1.0):
            y = py(peak * gv)
            grid += (f'<line x1="{PL}" y1="{y:.1f}" x2="{W-PR}" y2="{y:.1f}" class="cg"/>'
                     f'<text x="{PL-10}" y="{y+4:.1f}" class="ct" text-anchor="end">'
                     f'{int(peak*gv/1000)}K</text>')
        xlab = "".join(f'<text x="{px(i):.1f}" y="{H-PB+20}" class="ct" text-anchor="middle">'
                       f'{d["turn"]}</text>' for i, d in enumerate(wo))
        dots_w = "".join(f'<circle cx="{px(i):.1f}" cy="{py(d["recv_bytes"]):.1f}" r="3" class="dw"/>'
                         for i, d in enumerate(wo))
        dots_s = "".join(f'<circle cx="{px(i):.1f}" cy="{py(d["recv_bytes"]):.1f}" r="3" class="ds"/>'
                         for i, d in enumerate(ws))
        last_w = wo[-1]["recv_bytes"]
        last_s = ws[-1]["recv_bytes"]
        f = k["final"]
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <div class="curve-wrap">
    <svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" class="curve">
      {grid}
      <path d="{path(wo)}" class="lw"/><path d="{path(ws)}" class="ls"/>
      {dots_w}{dots_s}{xlab}
    </svg>
    <div class="clab" style="top:{py(last_w)/H*100:.1f}%">
      <b class="lw-t">不剥离</b><span>{last_w:,} B</span></div>
    <div class="clab" style="top:{py(last_s)/H*100:.1f}%">
      <b class="ls-t">剥离后</b><span>{last_s:,} B</span></div>
  </div>
  <div class="cnote">
    末轮：客户端发出 <b>{f['sent']:,} B</b> → 不剥离时上游收 <b class="warn">{f['recv_no_strip']:,} B</b>，
    剥离后只收 <b class="ok">{f['recv_strip']:,} B</b>（<b>省 {f['saved']:,} B，{(100.0*f['saved']/max(1,f['recv_no_strip'])):.0f}%</b>）。<br>
    差值逐轮<b>扩大</b>（第 1 轮 −830 B → 第 10 轮 −8,300 B）：历史越长，重复搬运越多。
  </div></section>"""

    if kind == "shape":
        s = k["s"]
        cards = [
            ("单请求省量中位数", f"{s['median_saved']:,}", "tokens",
             f"p90 达 {s['p90_saved']:,} —— 不是省零头"),
            ("剥离占该请求输入", f"{s['median_strip_ratio']:.0f}", "%",
             f"中位数；p90 高达 {s['p90_strip_ratio']:.0f}%"),
            ("有省量的请求占比", f"{s['req_with_savings_pct']:.1f}", "%",
             f"{s['req_with_savings']:,} / {s['ok']:,} 条成功请求"),
            ("单请求输入中位数", f"{s['median_input']:,}", "tokens",
             f"{s['big_input_pct']:.0f}% 的请求输入 ≥10 万"),
        ]
        cells = "".join(
            f'<div class="shc"><div class="shl">{t}</div>'
            f'<div class="shv">{v}<i>{u}</i></div><div class="shs">{d}</div></div>'
            for t, v, u, d in cards)
        fps = "".join(
            f'<div class="fp"><b>{m["model"]}</b><span>省 {m["saved"]/1e8:.2f} 亿 · {m["reqs"]:,} 次</span></div>'
            for m in s["top_models"][:4])
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <div class="sh-grid">{cells}</div>
  <div class="sh-two">
    <div class="sh-box">
      <div class="sh-bt">剥离集中在哪一层</div>
      <div class="bar2"><div class="b2-tc" style="width:{N_TOOLCALL_SHARE}%"></div></div>
      <div class="sh-lg"><span><i class="sw tc"></i>带 tool_calls 的轮次
        <b>{N_TOOLCALL_SHARE}%</b></span><span><i class="sw pl"></i>普通 assistant
        <b>{N_PLAIN_SHARE}%</b></span></div>
      <div class="sh-note">大头在多工具调用的轮次 —— 而这一层默认<b>仅在 chat 协议</b>开启。</div>
    </div>
    <div class="sh-box">
      <div class="sh-bt">省量前几名模型</div>
      <div class="fp-list">{fps}</div>
    </div>
  </div>
  <p class="foot-note">省量最大的 <b>20%</b> 请求贡献了 <b>{s['top20_pct_share']:.0f}%</b> 的省量总量；
    单请求输入中位数已达 <b>{s['median_input']:,}</b> tokens —— 长会话里绝大部分是重复上下文。</p></section>"""

    if kind == "diff":
        b, a = k["before"], k["after"]
        rows = ""
        for d in k["detail"]:
            kindtxt = "带 tool_calls" if d["has_tool_calls"] else "普通"
            rows += (f'<tr><td>#{d["index"]}</td><td>assistant · {kindtxt}</td>'
                     f'<td class="num">{d["reasoning_before"]:,} B</td>'
                     f'<td class="num ev">{d["reasoning_after"]} B</td>'
                     f'<td class="{"ok" if d["reasoning_after"] == 0 else ""}">'
                     f'{"已剥离" if d["reasoning_after"] == 0 else "保留"}</td></tr>')
        pct = 100.0 * (b["body_bytes"] - a["body_bytes"]) / max(1, b["body_bytes"])
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <div class="diff-wrap">
    <div class="df-card">
      <div class="df-h">剥离前 <span>(客户端发来的)</span></div>
      <div class="df-big">{b['body_bytes']:,}<i>B</i></div>
      <div class="df-kv"><span>消息条数</span><b>{b['messages']}</b></div>
      <div class="df-kv"><span>含推理链的消息</span><b class="hl">{b['messages_with_reasoning']}</b></div>
      <div class="df-kv"><span>推理链体积</span><b class="hl">{b['reasoning_bytes']:,} B</b></div>
    </div>
    <div class="df-mid"><div class="df-arrow">→</div><div class="df-pct">−{pct:.0f}%</div>
      <div class="df-mid-sub">请求体</div></div>
    <div class="df-card ok">
      <div class="df-h">剥离后 <span>(上游实收的)</span></div>
      <div class="df-big">{a['body_bytes']:,}<i>B</i></div>
      <div class="df-kv"><span>消息条数</span><b>{a['messages']}</b></div>
      <div class="df-kv"><span>含推理链的消息</span><b>{a['messages_with_reasoning']}</b></div>
      <div class="df-kv"><span>推理链体积</span><b>{a['reasoning_bytes']:,} B</b></div>
    </div>
  </div>
  <table class="matrix compact"><thead><tr><th>#</th><th>消息</th><th>剥离前推理链</th>
    <th>剥离后</th><th>结果</th></tr></thead><tbody>{rows}</tbody></table>
  <p class="foot-note">消息<b>条数与顺序完全不变</b>，只有推理链字段被移除；省下 {k['saved_bytes']:,} 字节
    （≈ {k['est']:,} tokens，按字符数 ÷ 4 估算）。注意这是网关对<b>请求体</b>做的事 —— 体积少了，
    上游那一轮收到的上下文就少了这么多。</p></section>"""

    if kind == "stats":
        items = "".join(f'<div class="st-item"><h4>{md(t_)}</h4><p>{md(d)}</p></div>' for t_, d in k["items"])
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <div class="st-row three">{items}</div><div class="honest">{md(k['note'])}</div></section>"""

    if kind == "gallery":
        figs = "".join(f'<figure><img src="{IMG[n]}" alt=""><figcaption>{c}</figcaption></figure>'
                       for n, c in k["pics"] if IMG.get(n))
        lead = f'<p class="lead">{md(k["lead"])}</p>' if k.get("lead") else ""
        return f'<section class="s gallery"><h2>{md(k["h"])}</h2>{lead}<div class="shots">{figs}</div></section>'

    if kind == "single":
        lis = "".join(f"<li>{md(x)}</li>" for x in k["bullets"])
        return f"""<section class="s"><h2>{md(k['h'])}</h2><p class="lead">{md(k['lead'])}</p>
  <div class="single">
    <div class="box"><div class="box-t">AIGate.exe</div>
      <div class="box-rows"><span>后台服务</span><span>管理面板</span><span>路由与计量</span></div></div>
    <ul class="checks">{lis}</ul></div><p class="foot-note">{md(k['badge'])}</p></section>"""

    if kind == "steps":
        st = "".join(f'<div class="step"><span class="sn">{n}</span><div><span class="st">{md(t_)}</span>'
                     f'<span class="sd">{md(d)}</span></div></div>' for n, t_, d in k["steps"])
        return f'<section class="s"><h2>{md(k["h"])}</h2><div class="steps">{st}</div><p class="foot-note">{md(k["foot"])}</p></section>'
    return ""


slides = "\n".join(render(kind, k) for kind, k in S)

# ─────────────────────────────────────────────────────────────────────────────
CSS = """
:root{
  color-scheme:dark;
  /* ══════════════════════════════════════════════════════════════════════════
     ⚠️ 语义 token 与 src/admin/base.css、site/index.html 的 :root **同源**
        （Apple 系统语义色）。改颜色 / 材质 / 圆角 / 动效 / 字体时**三处都要改** ——
        否则又变成"产品是苹果风、演示稿不是"（这正是本次返工的原因）。
        刻意**不搬**面板的排版与间距刻度（--sp-* / --fs-*）：面板是密集数据界面，
        演示稿是投屏尺度，本就该不同 —— 见本块末尾「演示稿专属」。
     ══════════════════════════════════════════════════════════════════════════ */
  /* 表面层: Apple 系统语义色 (secondarySystemBackground / tertiary) */
  --bg:#1c1c1e; --card:#2c2c2e; --elevated:#3a3a3c; --input:#2c2c2e;
  /* 文本: label / secondaryLabel / tertiaryLabel */
  --fg:#fff; --muted:rgba(235,235,245,.62); --dim:rgba(235,235,245,.40);
  /* 描边: 由 Apple separator 分档 */
  --border-subtle:rgba(84,84,88,.36); --border:rgba(84,84,88,.5); --border-s:rgba(84,84,88,.72);
  --surface-border:rgba(255,255,255,.09);
  /* 灰阶填充 (Apple fill 体系: 同一中性灰配不同 alpha) */
  --fill-1:rgba(120,120,128,.12); --fill-2:rgba(120,120,128,.20);
  --fill-4:rgba(120,120,128,.32); --fill-5:rgba(120,120,128,.40);
  /* 强调色: Apple systemBlue (--primary 填充 / --accent 文字图标线) */
  --primary:#0b6fe0; --primary-h:#0961c6; --primary-fg:#fff;
  --accent:#0a84ff; --secondary:#bf5af2;
  --primary-soft:rgba(10,132,255,.16); --primary-soft-deep:rgba(10,132,255,.12);
  --primary-badge:rgba(10,132,255,.2); --primary-bd-soft:rgba(10,132,255,.32);
  --primary-bd:rgba(10,132,255,.42); --row-hover:rgba(10,132,255,.10);
  --secondary-soft:rgba(191,90,242,.16);
  /* 状态色: Apple systemGreen / Orange / Red / Teal */
  --ok:#30d158; --ok-fg:#30d158; --ok-bg:rgba(48,209,88,.16); --ok-bd:rgba(48,209,88,.32);
  --warn:#ff9f0a; --warn-fg:#ff9f0a; --warn-bg:rgba(255,159,10,.16); --warn-bd:rgba(255,159,10,.32);
  --err:#ff453a; --err-fg:#ff453a; --err-bg:rgba(255,69,58,.16); --err-bd:rgba(255,69,58,.32);
  --info:#40c8e0; --info-fg:#40c8e0; --info-bg:rgba(64,200,224,.16); --info-bd:rgba(64,200,224,.32);
  --neutral-bg:rgba(120,120,128,.24); --neutral-fg:#8e8e93; --neutral-bd:rgba(120,120,128,.42);
  /* 材质层: 半透明 + 背景模糊 (Apple vibrancy) */
  --card-glass:rgba(44,44,46,.72); --card-nest:rgba(58,58,60,.62);
  --glass-blur:blur(30px) saturate(180%);
  /* 画布氛围: 低饱和多段径向渐变 —— 既是"桌面感"的底, 也是玻璃的"可模糊内容" */
  --amb:radial-gradient(1500px 800px at 6% -14%,rgba(10,132,255,.20),transparent 74%),
        radial-gradient(1300px 740px at 94% -4%,rgba(191,90,242,.15),transparent 72%),
        radial-gradient(1400px 860px at 56% 116%,rgba(64,200,224,.13),transparent 74%);
  /* 章节纹理发丝线 (中性灰, 不参与色相 —— 原版是红/青/紫/琥珀/绿五套高饱和纹理) */
  --tex:rgba(255,255,255,.045);
  /* 阴影三级 (苹果的阴影很轻, 分层靠材质亮度) + 状态柔光 + 顶边内高光 */
  --elev-1:0 1px 2px rgba(0,0,0,.32); --elev-2:0 6px 20px rgba(0,0,0,.38); --elev-3:0 18px 48px rgba(0,0,0,.55);
  --glow-primary:0 4px 18px rgba(10,132,255,.22);
  --glow-err:0 4px 18px rgba(255,69,58,.18);
  --glow-warn:0 4px 18px rgba(255,159,10,.18);
  --tint-primary:linear-gradient(90deg,rgba(10,132,255,.14),transparent);
  --tint-ok:linear-gradient(90deg,rgba(48,209,88,.12),transparent);
  --tint-warn:linear-gradient(90deg,rgba(255,159,10,.12),transparent);
  --inset-hi:inset 0 1px 0 rgba(255,255,255,.08);
  /* 几何: 四档 + 胶囊 (与面板同一几何语言; 50% 仅用于真圆) */
  --rs:6px; --rm:10px; --r:14px; --rl:18px; --pill:999px;
  /* 动效 (与面板同档; .s 的转场时长单独保留, 见下方"转场设计") */
  --dur-quick:.12s; --dur-fast:.16s; --dur:.24s; --dur-slow:.4s;
  --ease-out:cubic-bezier(.22,.8,.25,1);
  --ease-spring:cubic-bezier(.34,1.56,.64,1);
  /* 字体族 (同源: Inter 管拉丁 / PingFang 管中文; 数字走 tabular 而非等宽) */
  --font-ui:'Inter','Segoe UI',-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
  --font-mono:'JetBrains Mono',Consolas,monospace;
  /* ── 演示稿专属: 投屏尺度 (刻意不搬面板/落地页刻度) ──
     只把**跨组件复用的标题/数字档**收成 token; 单处微调的尺寸保持字面量 ——
     演示稿的每个小尺寸都是逐页对着排版调出来的, 强行归并会改版面。 */
  --fs-h1:clamp(3.2rem,8.2vw,6.8rem);
  --fs-h2:clamp(1.5rem,3vw,2.35rem);
  --fs-claim:clamp(1.35rem,2.7vw,2.35rem);
  --fs-lead:clamp(.93rem,1.25vw,1.1rem);
  --fs-kpi:clamp(1.55rem,3vw,2.5rem);
  --fs-fig:clamp(1.2rem,2.4vw,1.95rem);
  /* ※ 有少数 token 演示稿当前未用到 (--info-* / --neutral-* / --secondary* / --fill-2/5 /
     --pill / --dur-* / --ease-spring / --glow-warn 等), 保留是为与面板、落地页**同源、
     便于逐条对照**; 别因为"这里没用到"就删 —— 删了下一次同步又会漂移
     (site/index.html 的同名块里挂着同一条约定)。 */
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden}
/* 画布 = 纯色底 + 氛围渐变 (苹果的"桌面/壁纸"底): 毛玻璃卡片透过它才有内容可模糊。
   用 background-image 而非额外 DOM: 零元素、零重排, 且随主题走一个 token。 */
body{background-color:var(--bg);background-image:var(--amb);background-attachment:fixed;
 color:var(--fg);font-family:var(--font-ui);
 -webkit-font-smoothing:antialiased;font-feature-settings:'kern' 1}
/* code 只给**代码类内容**用 (端点 URL / 模型 ID / 文件名); 数字一律走 tabular figures */
code{font-family:var(--font-mono);font-size:.85em;
 background:var(--fill-1);color:var(--fg);padding:1px 6px;border-radius:var(--rs);white-space:nowrap}
b{color:var(--fg);font-weight:650}

/* 氛围层: 独立 canvas (环境光) + 章节纹理。均 pointer-events:none, 不抢翻页交互。
   两层各司其职: canvas 负责"会动的环境光", fx-tex 负责"不动的纹理",
   两者叠加才让每章底子真正不同 —— 见下方"章节氛围"注释。
   ⚠️ 原版还有第三个 .fx-mouse-glow (跟随鼠标的高饱和光晕, mix-blend-mode:screen):
      属"小范围高 alpha 的光斑"而非环境光, 与苹果的克制语言相悖 ——
      依 docs/frontend.md §4.1b 的判据移除 (canvas 光斑的色相/alpha 也一并收敛)。 */
.fx-bg{position:fixed;inset:0;z-index:0;pointer-events:none}

/* ── 章节纹理 (静态) ──
   为什么单独做成静态层: canvas 在 prefers-reduced-motion 下会被整体关掉,
   若章节差异只存在于 canvas, 无障碍用户就又看到"18 页一个样"。
   纹理是纯 CSS 渐变 + mask, 不消耗帧, 因此可以常驻。
   ⚠️ 中性化: 原版是红/青/紫/琥珀/绿五套**高饱和**纹理, 读起来像"工程图纸" ——
      见 docs/frontend.md §4.1b「不要在画布上再叠其它装饰」。现统一走中性发丝线
      (--tex), 只让**图案形状**随章节变, 色相不再参与。 */
.fx-tex{position:fixed;inset:0;z-index:0;pointer-events:none}
/* 封面: 无附加纹理 (画布氛围 + 封闭式构图已足够) */
html[data-chapter="pain"] .fx-tex{
 background-image:repeating-linear-gradient(115deg,var(--tex) 0 2px,transparent 2px 11px);
 -webkit-mask-image:radial-gradient(70% 62% at 18% 80%,#000,transparent);
 mask-image:radial-gradient(70% 62% at 18% 80%,#000,transparent)}
html[data-chapter="save"] .fx-tex{
 background-image:linear-gradient(var(--tex) 1px,transparent 1px),
                  linear-gradient(90deg,var(--tex) 1px,transparent 1px);
 background-size:52px 52px;
 -webkit-mask-image:radial-gradient(78% 72% at 50% 12%,#000,transparent);
 mask-image:radial-gradient(78% 72% at 50% 12%,#000,transparent)}
html[data-chapter="mech"] .fx-tex{
 background-image:repeating-radial-gradient(circle at 50% 44%,
  var(--tex) 0 1px,transparent 1px 74px);
 -webkit-mask-image:radial-gradient(72% 72% at 50% 44%,#000,transparent 82%);
 mask-image:radial-gradient(72% 72% at 50% 44%,#000,transparent 82%)}
html[data-chapter="judge"] .fx-tex{
 background-image:linear-gradient(160deg,var(--tex) 0%,transparent 46%),
                  linear-gradient(340deg,var(--tex) 0%,transparent 44%)}
html[data-chapter="setup"] .fx-tex{
 background-image:radial-gradient(var(--tex) 1px,transparent 1px);
 background-size:26px 26px;
 -webkit-mask-image:radial-gradient(82% 82% at 50% 92%,#000,transparent);
 mask-image:radial-gradient(82% 82% at 50% 92%,#000,transparent)}

/* 幕布之上才是内容, 保证正文永远压在氛围层上面 */
.deck{height:100%;overflow:hidden;position:relative;z-index:2}

/* ── 转场设计 (踩坑记录) ──
   基态 .s 就是"离场态", .s.on 是"在场态"。翻页只需增删 on 一个类。
   上一版曾额外加 .s.leaving 表示离场, 有两个必挂的坑, 故废弃该方案:
     ① .s.leaving 声明在 .s.on 之后且同权重 → 若某页同时带 on+leaving,
        opacity:0 会盖掉 opacity:1, 该页永久空白;
     ② 移除 leaving 靠 setTimeout, 但 show() 开头的 timers.forEach(clearTimeout)
        会把该定时器一并清掉 → 连按翻页必有页面被永久卡成空白。
   改用"基态即离场态"后: 无额外类、无定时器、无层叠冲突。
   过渡跑完后用 visibility 延迟 .62s 再隐藏 —— 否则离场瞬间 visibility:hidden,
   淡出/模糊根本来不及播 (上一版就是这么把离场动画吞掉的)。 */
.s{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;
 padding:5.5vh 6.5vw;
 opacity:0;visibility:hidden;
 filter:blur(6px);transform:scale(1.03) translateY(12px);
 transition:opacity .55s var(--ease-out),
            transform .65s var(--ease-out),
            filter .55s var(--ease-out),
            visibility 0s linear .62s}
.s>*{width:100%;max-width:1540px;margin-left:auto;margin-right:auto}
.s.on{opacity:1;visibility:visible;filter:blur(0);transform:none;
 transition:opacity .55s var(--ease-out),
            transform .65s var(--ease-out),
            filter .55s var(--ease-out),
            visibility 0s}

/* 入场错峰: 用 **transition** 而非 animation。
   原因: `animation ... both` 在动画未推进时会把元素停在首帧 (opacity:0) ——
   预览/截图/后台标签页等场景下会整页空白 (本项目在前端也踩过同一个坑)。
   transition 的终态由 CSS 声明值决定, 即使过渡不跑, 元素照样可见。 */
.s>*{opacity:0;transform:translateY(22px);filter:blur(3px);
 transition:opacity .6s var(--ease-out),
            transform .65s var(--ease-out),
            filter .55s var(--ease-out)}
.s.on>*{opacity:1;transform:none;filter:blur(0)}
.s.on>*:nth-child(1){transition-delay:.06s}
.s.on>*:nth-child(2){transition-delay:.14s}
.s.on>*:nth-child(3){transition-delay:.22s}
.s.on>*:nth-child(4){transition-delay:.30s}
.s.on>*:nth-child(5){transition-delay:.38s}
.s.on>*:nth-child(6){transition-delay:.46s}
/* 双曲线数据点弹入: 必须声明 fill-box, 否则 SVG 默认按 view-box 取原点,
   缩放会绕 viewBox 中心而不是圆自身 → 小球会从图表正中飞过来。 */
.curve circle{transform-box:fill-box;transform-origin:center}

/* 标题: 纯色前景。原版的"白→灰蓝渐变文字 + background-clip"是典型的 AI SaaS 语法,
   苹果不这么做 —— 层级靠字重与字号拉, 不靠给文字上渐变。 */
h1{font-size:var(--fs-h1);font-weight:820;letter-spacing:-.045em;margin:.35rem 0 0;
 line-height:.95;color:var(--fg)}
h2{font-size:var(--fs-h2);font-weight:760;letter-spacing:-.028em;margin:0 0 .6rem}
h4{margin:0 0 .38rem;font-size:.97rem;font-weight:650;color:var(--fg);letter-spacing:-.005em}
p{margin:0}
.lead{color:var(--muted);font-size:var(--fs-lead);margin:0 0 1.5rem;
 max-width:68rem;line-height:1.7}
.foot-note{margin-top:1.35rem;font-size:.83rem;color:var(--dim);line-height:1.74;max-width:78rem;
 padding-left:.95rem;border-left:2px solid var(--primary-bd-soft)}

/* ── 封面 ──
   氛围光**不再自己叠一层**: body 的 --amb 就是苹果的"桌面"底, 页面再打自己的
   聚光灯 (原 .mesh 的三个渐变色块 + .grid-bg 的工程网格) 会变成"AI 光斑" ——
   见 docs/frontend.md §4.1b「不要在画布上再叠其它装饰」, 两者均已移除。 */
.cover{padding-left:7.5vw}
.cv{position:relative}
.eyebrow{font-size:.72rem;letter-spacing:.34em;color:var(--accent);font-weight:700;text-transform:uppercase}
/* 品牌图标: 直接用项目资产 icon.svg 的构图 (圆角方块 + 渐变 + 中央枢纽 + 四节点),
   故容器不再自绘底色, 只负责尺寸与投影 */
.logo{width:82px;height:82px;margin-top:1.1rem;display:block;
 border-radius:var(--rl);overflow:hidden;
 box-shadow:var(--elev-2),0 0 0 1px var(--surface-border)}
.logo svg{width:100%;height:100%;display:block}
.claim{font-size:var(--fs-claim);font-weight:700;letter-spacing:-.03em;
 margin-top:1.5rem;line-height:1.32;color:var(--fg)}
.sub{color:var(--muted);font-size:clamp(.92rem,1.3vw,1.12rem);margin-top:1rem;max-width:44rem;line-height:1.65}
.facts{display:flex;gap:2.6rem;margin-top:2.6rem;flex-wrap:wrap}
.f{display:flex;flex-direction:column;gap:.26rem}
/* 数字不用等宽字: 苹果是 SF 的 tabular figures (比例字 + 等宽数字) */
.fv{font-size:1.02rem;font-weight:760;color:var(--fg);
 letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.fl{font-size:.79rem;color:var(--dim)}

/* ── 痛点 ── */
.pain-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:.9rem}
/* 玻璃卡片 (同面板 .pnl / 落地页 .card 的材质): 半透明 + 背景模糊 + 极轻投影 + 发丝线收边 */
.pain{display:flex;gap:.9rem;align-items:flex-start;background:var(--card-glass);
 backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);border-radius:var(--r);box-shadow:var(--elev-1);
 padding:1.05rem 1.15rem;position:relative;overflow:hidden;
 transition:transform .35s var(--ease-out),border-color .35s,box-shadow .35s}
.pain::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--err);
 transition:width .35s var(--ease-out)}
/* 悬停时一道光横扫而过 (父级 overflow:hidden 负责裁切, 不会溢出卡片) */
.pain::after{content:"";position:absolute;top:0;left:-100%;width:60%;height:100%;
 background:linear-gradient(90deg,transparent,var(--err-bg),transparent);
 transition:left .7s var(--ease-out)}
.pain:hover{transform:translateY(-4px) scale(1.012);border-color:var(--err-bd);
 box-shadow:var(--glow-err)}
.pain:hover::before{width:4px}
.pain:hover::after{left:140%}
.pn{flex:none;width:24px;height:24px;border-radius:var(--rs);display:flex;align-items:center;justify-content:center;
 font-size:.75rem;font-weight:700;color:var(--err-fg);background:var(--err-bg);
 border:1px solid var(--err-bd);transition:transform .3s}
.pain:hover .pn{transform:rotate(-8deg) scale(1.08)}
.pain h4{color:var(--fg)}
.pain p{color:var(--muted);font-size:.885rem;line-height:1.66}

/* ── 核心数字 ── */
.num-row{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem}
/* KPI 卡配方同落地页 .kpi: 玻璃卡 + 顶边内高光, 不再用渐变描边/旋转锥形光晕 */
.nm{position:relative;border-radius:var(--rl);padding:1.35rem 1.4rem;overflow:hidden;
 background:var(--card-glass);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);
 box-shadow:var(--inset-hi),var(--elev-1);
 transition:transform .4s var(--ease-out),box-shadow .4s,border-color .4s}
.nm::after{content:"";position:absolute;top:0;left:0;right:0;height:1px;background:var(--primary);opacity:.55}
.nm:hover{transform:translateY(-4px);border-color:var(--primary-bd);box-shadow:var(--elev-2)}
.nmv{font-size:var(--fs-kpi);font-weight:820;letter-spacing:-.035em;color:var(--fg);
 line-height:1;font-variant-numeric:tabular-nums;
 position:relative;z-index:1}
.nmv i{font-style:normal;font-size:.5em;margin-left:.18em;color:var(--accent);font-weight:700}
.nml{margin-top:.6rem;font-size:.82rem;color:var(--muted);line-height:1.55;position:relative;z-index:1}

/* ── 流向 ── */
.flow{display:flex;align-items:center;gap:1rem;flex-wrap:wrap}
.fcol{display:flex;flex-direction:column;gap:.5rem;min-width:10rem}
.fcol-t{font-size:.72rem;color:var(--dim);text-transform:uppercase;letter-spacing:.12em;margin-bottom:.1rem}
.node{background:var(--card-glass);border:1px solid var(--surface-border);border-radius:var(--rm);
 padding:.6rem .9rem;font-size:.89rem;color:var(--muted);transition:border-color .25s,transform .25s}
.node:hover{border-color:var(--primary-bd);transform:translateX(2px)}
.wire{flex:none;width:64px;height:12px}
.wireline{stroke:var(--accent);stroke-width:1.6;fill:none;stroke-dasharray:6 6;opacity:.8;
 animation:flowdash 1.1s linear infinite}
.wirehead{stroke:var(--accent);stroke-width:1.8;fill:none;stroke-linecap:round;stroke-linejoin:round}
@keyframes flowdash{to{stroke-dashoffset:-12}}
.hub{background:var(--card-glass);background-image:var(--tint-primary);
 backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--primary-bd);border-radius:var(--rl);padding:1.3rem 1.9rem;text-align:center;
 box-shadow:var(--inset-hi),var(--elev-1)}
.hub-ico{display:flex;justify-content:center;margin-bottom:.55rem}
.hub-ico svg{width:30px;height:30px;border-radius:var(--rm);display:block;box-shadow:var(--glow-primary)}
.hub-n{font-size:1.35rem;font-weight:780;letter-spacing:-.025em}
.hub-s{font-size:.76rem;color:var(--accent);margin-top:.3rem}

/* ── 决策映射表 ── */
.matrix{width:100%;border-collapse:separate;border-spacing:0;
 background:var(--card-glass);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);border-radius:var(--rl);box-shadow:var(--elev-1);overflow:hidden}
.matrix th{text-align:left;padding:.85rem 1.15rem;font-size:.74rem;color:var(--muted);
 text-transform:uppercase;letter-spacing:.1em;font-weight:650;border-bottom:1px solid var(--border)}
.matrix td{padding:1.02rem 1.15rem;font-size:.92rem;border-bottom:1px solid var(--border-subtle)}
.matrix tr:last-child td{border-bottom:none}
.matrix tbody tr{transition:background .22s}
.matrix tbody tr:hover{background:var(--row-hover)}
.matrix td.ok{color:var(--ok-fg);font-weight:640}

/* ── 协议页 (结论表 + 取舍三行压缩版) ── */
.proto .matrix{margin-bottom:.8rem}
.pf-list{display:grid;grid-template-columns:repeat(3,1fr);gap:.72rem}
.pf{background:var(--card-glass);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);border-radius:var(--r);box-shadow:var(--elev-1);
 padding:.85rem 1rem;
 transition:border-color .25s,transform .25s var(--ease-out)}
.pf:hover{border-color:var(--primary-bd);transform:translateY(-2px)}
.pf-k{font-size:.74rem;color:var(--accent);font-weight:680;margin-bottom:.4rem;letter-spacing:.01em}
.pf-v{color:var(--fg);font-size:.845rem;line-height:1.62}
.pf-o{display:block;margin-top:.42rem;padding-left:.7rem;border-left:2px solid var(--primary-bd-soft);
 color:var(--muted);font-size:.8rem;line-height:1.62}

/* ── 设计取舍 (四段式) ── */
.tr-list{display:grid;gap:.72rem}
.tr{display:grid;grid-template-columns:minmax(8.5rem,10.5rem) 1fr;gap:1.25rem;align-items:start;
 background:var(--card-glass);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);border-radius:var(--r);box-shadow:var(--elev-1);
 padding:1rem 1.2rem;
 position:relative;overflow:hidden;transition:border-color .25s,transform .25s var(--ease-out)}
.tr::before{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--border-s)}
.tr:hover{border-color:var(--primary-bd);transform:translateX(3px)}
/* "我们的做法" 高亮: 左侧主色条 + 主色柔光底, 让结论从对照里跳出来 */
.tr.hi{border-color:var(--primary-bd);background-image:var(--tint-primary)}
.tr.hi::before{background:var(--primary);opacity:1}
.tr.hi .tr-k{color:var(--accent)}
.tr-k{font-size:.82rem;font-weight:680;color:var(--dim);line-height:1.55;letter-spacing:.01em}
.tr-w{color:var(--fg);font-size:.885rem;line-height:1.68}
/* 结果/代价另起一行并加左边框 —— "为什么不行"是这页的重点, 不能和做法混成一句 */
.tr-o{margin-top:.42rem;padding-left:.75rem;border-left:2px solid var(--primary-bd-soft);
 color:var(--muted);font-size:.845rem;line-height:1.66}

/* ── 能力网格 ── */
.grid-2{display:grid;grid-template-columns:repeat(2,1fr);gap:1rem}
.gc{background:var(--card-glass);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);border-radius:var(--rl);box-shadow:var(--elev-1);
 padding:1.2rem 1.3rem;
 transition:transform .35s var(--ease-out),border-color .35s,box-shadow .35s}
.gc:hover{transform:translateY(-5px) scale(1.01);border-color:var(--primary-bd);
 box-shadow:var(--elev-2)}
.gc.core{border-color:var(--primary-bd);background-image:var(--tint-primary)}
.gci{width:34px;height:34px;border-radius:var(--rm);display:flex;align-items:center;justify-content:center;
 font-size:1.05rem;color:var(--accent);background:var(--primary-soft);
 border:1px solid var(--primary-bd-soft);margin-bottom:.8rem;
 transition:transform .4s var(--ease-out),box-shadow .4s}
.gc:hover .gci{transform:scale(1.12) rotate(-6deg);box-shadow:var(--glow-primary)}
.gc p{color:var(--muted);font-size:.87rem;line-height:1.66}

/* ── 剥离判定表 ── */
.dc-list{display:grid;gap:.6rem}
.dc{display:grid;grid-template-columns:minmax(11rem,15rem) minmax(6rem,7.5rem) 1fr;gap:1.1rem;
 align-items:start;background:var(--card-glass);border:1px solid var(--surface-border);
 backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border-radius:var(--r);box-shadow:var(--elev-1);
 padding:.85rem 1.15rem;transition:border-color .25s,transform .25s var(--ease-out)}
.dc:hover{border-color:var(--primary-bd);transform:translateX(3px)}
.dc-c{font-size:.885rem;color:var(--fg);font-weight:620;line-height:1.5}
.dc-v{display:flex;align-items:flex-start}
.bdg2{display:inline-block;font-size:.72rem;font-weight:720;padding:.16rem .5rem;border-radius:var(--rs);
 text-align:center;width:fit-content}
.bd-never{background:var(--err-bg);color:var(--err-fg);border:1px solid var(--err-bd)}
.bd-yes{background:var(--ok-bg);color:var(--ok-fg);border:1px solid var(--ok-bd)}
.bd-maybe{background:var(--warn-bg);color:var(--warn-fg);border:1px solid var(--warn-bd)}
.dc-vt{font-size:.72rem;color:var(--dim)}
.dc-w{font-size:.855rem;color:var(--muted);line-height:1.65}

/* ── 为什么可以剥 (过程 vs 结果) ── */
.why{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1.1rem}
.wy-col{backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border-radius:var(--rl);padding:1.1rem 1.25rem;border:1px solid var(--surface-border);
 background:var(--card-glass);box-shadow:var(--elev-1)}
.wy-col.keep{border-color:var(--ok-bd);background-image:var(--tint-ok)}
.wy-col.drop{border-color:var(--warn-bd);background-image:var(--tint-warn)}
.wy-h{display:flex;align-items:center;gap:.55rem;font-size:.92rem;font-weight:680;
 color:var(--fg);margin-bottom:.75rem}
.wy-b{width:22px;height:22px;border-radius:var(--rs);display:flex;align-items:center;justify-content:center;
 font-size:.8rem;font-weight:800}
.keep .wy-b{background:var(--ok-bg);color:var(--ok-fg);border:1px solid var(--ok-bd)}
.drop .wy-b{background:var(--warn-bg);color:var(--warn-fg);border:1px solid var(--warn-bd)}
.wy-col ul{list-style:none;padding:0;margin:0;display:grid;gap:.55rem}
.wy-col li{position:relative;padding-left:1.1rem;font-size:.865rem;color:var(--muted);line-height:1.6}
.wy-col li::before{content:"";position:absolute;left:0;top:.62em;width:5px;height:5px;border-radius:50%;
 background:currentColor;opacity:.5}
.keep li::before{background:var(--ok-fg)}
.drop li::before{background:var(--warn-fg)}

/* ── 多轮累积: 真实实测双曲线 ── */
.curve-wrap{position:relative;margin-bottom:.5rem}
.curve{width:100%;height:clamp(200px,30vh,290px);display:block;overflow:visible}
.cg{stroke:var(--border-subtle);stroke-width:1}
.ct{fill:var(--dim);font-size:11px;font-variant-numeric:tabular-nums}
.lw{fill:none;stroke:var(--warn);stroke-width:2.4;stroke-linejoin:round;stroke-linecap:round}
.ls{fill:none;stroke:var(--accent);stroke-width:2.4;stroke-linejoin:round;stroke-linecap:round}
.dw{fill:var(--warn)}.ds{fill:var(--accent)}
.clab{position:absolute;right:0;transform:translateY(-50%);display:flex;flex-direction:column;
 gap:.1rem;font-size:.76rem;text-align:right;white-space:nowrap}
.clab b{font-weight:660}
.clab span{color:var(--muted);font-size:.75rem;font-variant-numeric:tabular-nums}
.lw-t{color:var(--warn)}.ls-t{color:var(--accent)}
.cnote{margin-top:.7rem;padding:.9rem 1.15rem;border-radius:var(--r);background:var(--card-glass);
 backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);box-shadow:var(--elev-1);
 color:var(--muted);font-size:.845rem;line-height:1.72}
.cnote .warn{color:var(--warn-fg)}.cnote .ok{color:var(--ok-fg)}

/* ── 省量形状 (真实分布) ── */
.sh-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:.85rem;margin-bottom:.9rem}
.shc{background:var(--card-glass);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);border-radius:var(--rl);padding:1rem 1.1rem;
 box-shadow:var(--inset-hi),var(--elev-1)}
.shl{font-size:.74rem;color:var(--dim);margin-bottom:.4rem}
.shv{font-size:var(--fs-fig);font-weight:800;
 color:var(--fg);line-height:1.05;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.shv i{font-style:normal;font-size:.46em;color:var(--accent);margin-left:.18em;font-weight:700}
.shs{margin-top:.4rem;font-size:.75rem;color:var(--muted);line-height:1.5}
.sh-two{display:grid;grid-template-columns:1.15fr 1fr;gap:.85rem;align-items:start}
.sh-box{background:var(--card-glass);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);border-radius:var(--rl);box-shadow:var(--elev-1);
 padding:1rem 1.15rem}
.sh-bt{font-size:.77rem;color:var(--accent);font-weight:660;margin-bottom:.7rem}
.bar2{height:15px;border-radius:var(--rm);background:var(--fill-4);overflow:hidden}
.b2-tc{height:100%;background:var(--warn);border-radius:var(--rm)}
.sh-lg{display:flex;gap:1.3rem;flex-wrap:wrap;margin-top:.6rem;font-size:.79rem;color:var(--muted)}
.sh-lg span{display:flex;align-items:center;gap:.45rem}
.sh-lg b{color:var(--fg)}
.sh-lg .sw{width:10px;height:10px;border-radius:3px;display:inline-block}
.sh-lg .sw.tc{background:var(--warn)}
.sh-lg .sw.pl{background:var(--accent)}
.sh-note{margin-top:.65rem;font-size:.78rem;color:var(--dim);line-height:1.6}
.fp-list{display:grid;gap:.5rem}
/* 嵌套块: 苹果靠"提亮"分层, 比卡片再亮一档 */
.fp{display:flex;flex-direction:column;gap:.16rem;padding:.5rem .7rem;border-radius:var(--rm);
 background:var(--card-nest);border:1px solid var(--surface-border)}
.fp b{font-size:.78rem;color:var(--fg);font-family:var(--font-mono);font-weight:600}
.fp span{font-size:.73rem;color:var(--muted)}

/* ── 多轮累积 (柱状叠加) ── */
.accum{margin-bottom:.3rem}
.ac-bars{display:flex;align-items:flex-end;gap:1.05rem;height:clamp(170px,28vh,270px)}
.ac-col{flex:1;height:100%;display:flex;align-items:flex-end;justify-content:center;min-width:0}
.ac-bar{width:100%;max-width:104px;display:flex;flex-direction:column;
 border-radius:var(--rm) var(--rm) var(--rs) var(--rs);
 overflow:hidden;transition:transform .28s var(--ease-out),box-shadow .28s;
 box-shadow:var(--elev-1)}
.ac-col:hover .ac-bar{transform:translateY(-5px)}
/* 推理链段用暖色 (与"浪费"的语义一致), 对话段用主色; 渐变两端都走 token, 不写死 rgba */
.ac-hist{background:linear-gradient(180deg,var(--warn),var(--warn-bg));
 border-bottom:1px solid var(--warn-bd)}
.ac-talk{background:linear-gradient(180deg,var(--primary),var(--primary-soft));
 border-top:1px solid var(--surface-border)}
.ac-labels{display:flex;gap:1.05rem;margin-top:.62rem}
.ac-lab{flex:1;text-align:center;font-size:.79rem;color:var(--muted);font-weight:620;min-width:0}
.ac-lab span{display:block;font-size:.69rem;color:var(--dim);font-weight:400;margin-top:.14rem}
.ac-legend{display:flex;gap:1.6rem;flex-wrap:wrap;margin-top:1.05rem;font-size:.8rem;color:var(--muted)}
.ac-legend span{display:flex;align-items:center;gap:.5rem}
.ac-legend .sw{width:11px;height:11px;border-radius:3px;display:inline-block}
.ac-legend .sw.talk{background:var(--primary)}
.ac-legend .sw.hist{background:var(--warn)}
.cap-note{margin-top:.7rem;font-size:.77rem;color:var(--dim);line-height:1.6}

/* ── 剥离前后对照 ── */
.diff-wrap{display:grid;grid-template-columns:1fr auto 1fr;gap:1.2rem;align-items:stretch;margin-bottom:1.1rem}
.df-card{background:var(--card-glass);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);border-radius:var(--rl);box-shadow:var(--elev-1);
 padding:1.05rem 1.25rem}
.df-card.ok{border-color:var(--ok-bd);
 background-image:linear-gradient(150deg,var(--ok-bg),transparent)}
.df-h{font-size:.76rem;color:var(--dim);text-transform:uppercase;letter-spacing:.11em;margin-bottom:.5rem}
.df-h span{text-transform:none;letter-spacing:0;color:var(--muted)}
.df-big{font-size:var(--fs-fig);font-weight:800;
 color:var(--fg);line-height:1;margin-bottom:.8rem;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.df-big i{font-style:normal;font-size:.48em;color:var(--accent);margin-left:.2em}
.df-kv{display:flex;justify-content:space-between;gap:1rem;font-size:.82rem;color:var(--muted);
 padding:.34rem 0;border-top:1px solid var(--border-subtle)}
.df-kv b{color:var(--fg);font-weight:640}
.df-kv b.hl{color:var(--warn-fg)}
.df-mid{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:.2rem;min-width:5.2rem}
.df-arrow{font-size:1.5rem;color:var(--accent);line-height:1}
.df-pct{font-size:1.15rem;font-weight:800;color:var(--ok-fg);font-variant-numeric:tabular-nums}
.df-mid-sub{font-size:.68rem;color:var(--dim)}
.matrix.compact th{padding:.62rem .8rem;font-size:.68rem}
.matrix.compact td{padding:.6rem .8rem;font-size:.83rem}
.matrix.compact td.num{font-variant-numeric:tabular-nums}
.matrix.compact td.ev{color:var(--ok-fg);font-weight:640}

/* ── 省量手段 ── */
.st-row{display:grid;gap:1rem}
.st-row.three{grid-template-columns:repeat(3,1fr)}
.st-item{background:var(--card-glass);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);border-radius:var(--rl);box-shadow:var(--elev-1);
 padding:1.15rem 1.25rem;
 transition:border-color .25s,transform .25s;display:flex;flex-direction:column}
.st-item:hover{border-color:var(--primary-bd);transform:translateY(-2px)}
.st-item p{color:var(--muted);font-size:.865rem;line-height:1.68}
/* 诚实提示条 (警示柔光底; 正文仍走 --fg 保证可读) */
.honest{margin-top:1.2rem;padding:1.05rem 1.25rem;border-radius:var(--r);
 background:var(--warn-bg);border:1px solid var(--warn-bd);color:var(--fg);
 font-size:.875rem;line-height:1.74;max-width:78rem}

/* ── 截图 ── */
.gallery{justify-content:flex-start;padding-top:6vh}
.shots{display:grid;grid-template-columns:1fr 1fr;gap:1.1rem;min-height:0}
.shots figure{margin:0;display:flex;flex-direction:column;min-width:0}
.shots img{width:100%;height:auto;max-height:62vh;object-fit:contain;object-position:top;
 border-radius:var(--r);border:1px solid var(--surface-border);background:var(--bg);
 box-shadow:var(--elev-3);transition:transform .35s var(--ease-out)}
.shots figure:hover img{transform:scale(1.012)}
.shots figcaption{font-size:.79rem;color:var(--muted);margin-top:.5rem;text-align:center}

/* ── 单文件 ── */
.single{display:grid;grid-template-columns:minmax(14rem,19rem) 1fr;gap:1.5rem;align-items:start}
.box{background:var(--card-glass);background-image:var(--tint-primary);
 backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--primary-bd);border-radius:var(--rl);padding:1.2rem 1.3rem;
 box-shadow:var(--inset-hi),var(--elev-1)}
/* 文件名属"代码类内容", 保留等宽 */
.box-t{font-family:var(--font-mono);font-size:.97rem;font-weight:700;color:var(--fg);margin-bottom:.85rem;
 padding-bottom:.7rem;border-bottom:1px solid var(--border)}
.box-rows{display:flex;flex-direction:column;gap:.42rem}
.box-rows span{font-size:.83rem;color:var(--muted);padding-left:1rem;position:relative}
.box-rows span::before{content:"";position:absolute;left:.1rem;top:.62em;width:5px;height:5px;border-radius:50%;
 background:var(--accent);opacity:.85}
.checks{list-style:none;padding:0;margin:0;display:grid;gap:.72rem}
.checks li{position:relative;padding-left:1.7rem;color:var(--muted);font-size:.885rem;line-height:1.7}
.checks li::before{content:"";position:absolute;left:0;top:.5em;width:14px;height:8px;
 border-left:2px solid var(--ok-fg);border-bottom:2px solid var(--ok-fg);transform:rotate(-45deg)}

/* ── 步骤 ── */
.steps{display:grid;gap:.8rem;max-width:62rem}
.step{display:flex;gap:1.05rem;align-items:flex-start;background:var(--card-glass);
 backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);border-radius:var(--r);box-shadow:var(--elev-1);
 padding:1rem 1.15rem;transition:border-color .25s,transform .25s var(--ease-out)}
.step:hover{border-color:var(--primary-bd);transform:translateX(3px)}
/* 步骤序号: 苹果的按钮/序号是**实心填充**, 不是渐变 */
.sn{flex:none;width:29px;height:29px;border-radius:var(--rm);display:flex;align-items:center;justify-content:center;
 font-weight:700;font-size:.87rem;color:var(--primary-fg);background:var(--primary);
 box-shadow:var(--elev-1)}
.st{display:block;font-weight:660;margin-bottom:.2rem}
.sd{display:block;color:var(--muted);font-size:.88rem;line-height:1.64}

/* ── 进度 / 页码 ── */
/* 这条规则里**绝不能**再写一次 position —— 同一规则内后写的声明会覆盖前面的 fixed。
   上一版在末尾补了句 position:relative;overflow:hidden, 结果进度条退回普通文档流,
   被 .deck 的 height:100% 顶到视口外面, 完全看不见 (实测 rectTop === innerHeight)。 */
.bar{position:fixed;left:0;bottom:0;height:3px;background:var(--primary);width:0;
 overflow:hidden;z-index:9;
 transition:width .5s var(--ease-out);
 box-shadow:var(--glow-primary)}
/* 页码: 纯色次要文字。原版用"渐变文字 + background-clip"再叠一道流光 ——
   那套语法与苹果不符, 故进度条只留实心主色, 去掉扫光与渐变字。 */
.pg{position:fixed;right:22px;bottom:18px;font-size:.76rem;font-weight:600;
 z-index:9;letter-spacing:.05em;color:var(--muted);font-variant-numeric:tabular-nums}
.hint{position:fixed;left:22px;bottom:14px;font-size:.72rem;color:var(--muted);z-index:9;
 padding:4px 10px;border-radius:var(--rm);background:var(--card-glass);
 backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);
 border:1px solid var(--surface-border);letter-spacing:.02em}

@media(max-width:1150px){
 .pain-grid,.grid-2,.st-row.three,.shots{grid-template-columns:1fr}
 .num-row{grid-template-columns:repeat(2,1fr)}
 .single{grid-template-columns:1fr}
 .tr{grid-template-columns:1fr;gap:.4rem}
 .diff-wrap{grid-template-columns:1fr;gap:.6rem}
 .accum{gap:.5rem}
 .pf-list{grid-template-columns:1fr}
 .sh-grid{grid-template-columns:repeat(2,1fr)}
 .sh-two{grid-template-columns:1fr}
}
@media(prefers-reduced-motion:reduce){
 .wireline{animation:none!important}
 .pain::after,.gci{transition:none!important}
 /* 关键: 只让"在场"的那一页可见。
    上一版把 opacity:1!important 写在 .s 上, 于是 18 页 (position:absolute;inset:0)
    全部可见并互相重叠, 最后一页盖在最上面, 翻页彻底失效 —— 无障碍场景下的功能性损坏。 */
 .s,.s>*{transition:none!important;transform:none!important;filter:none!important}
 .s.on{opacity:1!important;visibility:visible!important}
 .s.on>*{opacity:1!important}
 /* canvas 强制关闭; .fx-tex 故意**不**关 ——
    它是静态 CSS 纹理, 是 reduced-motion 下唯一还能区分章节的底子。 */
 .fx-bg{display:none!important}
}
@media print{
 html,body{background:var(--bg);background-image:none}
 .deck{height:auto;overflow:visible}
 .s{position:static;opacity:1;visibility:visible;transform:none;filter:none;
  page-break-after:always;height:100vh}
 .s>*{opacity:1!important;transform:none!important;filter:none!important;transition:none!important}
 .bar,.pg,.hint,.fx-bg,.fx-tex{display:none}
}
"""

JS = """
/* ═══ AIGate Deck 渲染引擎 ═══
   四条硬约束, 每条都对应一次实测踩坑, 改动前请先读:
     ① 计数用 setInterval, 不用 requestAnimationFrame —— headless / 后台标签页下
        rAF 在页面不可见时会停摆 (实测只被调用 4 次), 计数器会永远停在 0。
     ② 氛围层 (canvas) 一律 pointer-events:none + 独立 z-index,
        绝不抢翻页交互, 也不遮挡正文。(原第三层"跟随鼠标的光晕"已按 §4.1b 移除)
     ③ prefers-reduced-motion 下必须同时做到 (a) 不启动 canvas 循环 (b) 只显示在场那一页。
        两者统一由 motionOK() 裁决 —— 上一版 CSS 关了但 JS 又把循环拉起来, 各改各的。
     ④ 离场动画不依赖任何额外类, 由 .s 基态承担 (原因见 CSS 区注释)。 */
var ss=[].slice.call(document.querySelectorAll('.s')), i=0, timers=[];

function motionOK(){
  return !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/* ── 6 段章节氛围 (替代早先的"每页一色") ──
   早先给 18 页各配一个色相, 但只改了**颜色**: 光斑锚点/半径/纹理/星尘密度全都不动,
   且 alpha 只有 13% / 9% —— 远低于可感知下限。结果 18 页底子看着一模一样
   (实测反馈"背景一成不变")。教训与项目既有记录一致: 幅度必须高于可感知下限。
   现在每章同时改四件事, 且切换时**逐帧插值**(光源是真的缓缓移过去, 不是瞬间换色):
     ① 光斑锚点 ax/ay、bx/by   ② 半径系数 ra/rb   ③ 透明度 al   ④ 星尘密度 n、漂移速度 spd
   纹理变体交给 CSS 的 html[data-chapter] (静态层, reduced-motion 下依然生效)。
   章节: 1封面 → 2痛点 → 3省量(3/8/9/10) → 4机制(4/5/6/7) → 5判定(11/12/13) → 6上手(14~18)
   ⚠️ 色相收敛到 Apple 氛围光的三色族 (systemBlue / systemPurple / systemTeal, 与 :root 的
      --amb 完全同源), 不再用 red / amber / green 这类高饱和色 —— 依 docs/frontend.md §4.1b
      的判据: "范围大 alpha 中才是环境光, 小范围高 alpha 就是光斑"。原版 al 到 .30 且半径
      只有 R*.55, 读起来是打在画布上的聚光灯, 与苹果的克制语言相悖, 故 al 收到 .17~.22。
      章节之间仍可辨: 靠**锚点 / 半径 / 透明度 / 星尘密度 + CSS 纹理图案**这几处差异,
      而不是靠高饱和换色 (色相只作次要信号, 三色族轮转)。 */
var CHAPTERS=[
  {id:'cover',c1:'#0a84ff',c2:'#bf5af2',ax:.78,ay:.18,bx:.92,by:.62,ra:1.00,rb:.90,al:.20,n:120,spd:1.0},
  {id:'pain', c1:'#bf5af2',c2:'#0a84ff',ax:.22,ay:.78,bx:.86,by:.20,ra:1.10,rb:.85,al:.18,n: 80,spd:1.5},
  {id:'save', c1:'#40c8e0',c2:'#0a84ff',ax:.50,ay:.10,bx:.16,by:.88,ra:1.25,rb:1.05,al:.22,n: 60,spd:0.7},
  {id:'mech', c1:'#bf5af2',c2:'#40c8e0',ax:.50,ay:.44,bx:.50,by:.44,ra:1.45,rb:.55,al:.20,n:100,spd:0.9},
  {id:'judge',c1:'#0a84ff',c2:'#40c8e0',ax:.10,ay:.16,bx:.92,by:.90,ra:1.05,rb:1.05,al:.19,n: 70,spd:1.8},
  {id:'setup',c1:'#40c8e0',c2:'#bf5af2',ax:.50,ay:.98,bx:.88,by:.06,ra:1.35,rb:.85,al:.17,n: 50,spd:0.6}
];
/* 页序 → 章节索引 (共 18 页) */
var PAGE_CH=[0, 1, 2, 3,3,3,3, 2,2,2, 4,4,4, 5,5,5,5,5];

var prevStage=-1, chStars=120;
/* 当前(插值中)/目标 几何与颜色: 翻页只写目标值, 由 fxDraw 逐帧逼近 */
var geoNow={ax:.5,ay:.5,bx:.5,by:.5,ra:1,rb:1,al:.2,spd:1};
var geoTgt={ax:.5,ay:.5,bx:.5,by:.5,ra:1,rb:1,al:.2,spd:1};
var rgb1Now=[10,132,255], rgb1Tgt=[10,132,255];
var rgb2Now=[191,90,242], rgb2Tgt=[191,90,242];

function lerp(a,b,k){ return a+(b-a)*k; }
function hexToRgb(h){
  return [parseInt(h.substr(1,2),16),parseInt(h.substr(3,2),16),parseInt(h.substr(5,2),16)];
}
function rgbaOf(c,a){ return 'rgba('+Math.round(c[0])+','+Math.round(c[1])+','+Math.round(c[2])+','+a+')'; }
function applyChapter(pageIdx){
  if(pageIdx===prevStage) return;
  prevStage=pageIdx;
  var ch=CHAPTERS[PAGE_CH[pageIdx]||0];
  geoTgt={ax:ch.ax,ay:ch.ay,bx:ch.bx,by:ch.by,ra:ch.ra,rb:ch.rb,al:ch.al,spd:ch.spd};
  rgb1Tgt=hexToRgb(ch.c1); rgb2Tgt=hexToRgb(ch.c2);
  chStars=ch.n;
  document.documentElement.setAttribute('data-chapter',ch.id);   // 驱动 CSS 纹理变体
  // 只有 canvas 已初始化 (即非 reduced-motion) 才续帧。
  // 上一版这里无条件 requestAnimationFrame, 把无障碍开关给绕过去了。
  if(fxReady&&!fxRaf) fxRaf=requestAnimationFrame(fxDraw);
}

/* ── 星尘 + 径向光斑 (独立 canvas 层) ── */
var fxCanvas=null, fxStars=[], fxRaf=null, fxReady=false;
function fxResize(){
  if(!fxCanvas) return;
  fxCanvas.width=window.innerWidth;
  fxCanvas.height=window.innerHeight;
}
function initFx(){
  fxCanvas=document.getElementById('fxBg');
  if(!fxCanvas||!motionOK()) return;      // reduced-motion: 连初始化都不做
  fxResize();
  window.addEventListener('resize',fxResize);
  var n=Math.min(200,Math.max(70,Math.floor(window.innerWidth/9)));  // 上限 200, 低端机不掉帧
  // 一次性按上限分配星尘, 每章只画前 chStars 颗来表现"密度不同" ——
  // 若按章节数量重建数组, 翻页时会看到粒子整体跳变。
  for(var k=0;k<n;k++){
    fxStars.push({x:Math.random()*fxCanvas.width, y:Math.random()*fxCanvas.height,
      r:Math.random()*1.4+.3, vx:(Math.random()-.5)*.12, vy:(Math.random()-.5)*.12,
      tw:Math.random()*Math.PI*2, tvs:.008+Math.random()*.012});
  }
  fxReady=true;
  fxRaf=requestAnimationFrame(fxDraw);
}
function fxDraw(){
  if(!fxReady) return;
  var W=fxCanvas.width, H=fxCanvas.height, ctx=fxCanvas.getContext('2d'), R=Math.max(W,H);
  ctx.clearRect(0,0,W,H);
  // 逐帧逼近本章目标几何/颜色: 约 1s 走完大半, 观感是"光源缓缓移过去"而非瞬间换色
  var k=.055;
  geoNow.ax=lerp(geoNow.ax,geoTgt.ax,k); geoNow.ay=lerp(geoNow.ay,geoTgt.ay,k);
  geoNow.bx=lerp(geoNow.bx,geoTgt.bx,k); geoNow.by=lerp(geoNow.by,geoTgt.by,k);
  geoNow.ra=lerp(geoNow.ra,geoTgt.ra,k); geoNow.rb=lerp(geoNow.rb,geoTgt.rb,k);
  geoNow.al=lerp(geoNow.al,geoTgt.al,k); geoNow.spd=lerp(geoNow.spd,geoTgt.spd,k);
  for(var ci=0;ci<3;ci++){
    rgb1Now[ci]=lerp(rgb1Now[ci],rgb1Tgt[ci],k);
    rgb2Now[ci]=lerp(rgb2Now[ci],rgb2Tgt[ci],k);
  }
  var t=Date.now()*.00018;
  // 光斑 1: 锚点 / 半径 / 透明度全部由章节决定 —— 这是"背景在变"的主信号
  var x1=W*geoNow.ax, y1=H*geoNow.ay, r1=R*.55*geoNow.ra;
  var g1=ctx.createRadialGradient(x1,y1,0,x1,y1,r1);
  g1.addColorStop(0,rgbaOf(rgb1Now,geoNow.al));
  g1.addColorStop(.42,rgbaOf(rgb1Now,geoNow.al*.34));
  g1.addColorStop(1,'transparent');
  ctx.fillStyle=g1; ctx.fillRect(0,0,W,H);
  // 光斑 2: 在章节锚点上再叠一层缓慢漂移, 静止时也有呼吸感
  var x2=W*geoNow.bx+Math.sin(t*1.3)*70, y2=H*geoNow.by+Math.cos(t*.9)*58;
  var g2=ctx.createRadialGradient(x2,y2,0,x2,y2,R*.45*geoNow.rb);
  g2.addColorStop(0,rgbaOf(rgb2Now,geoNow.al*.75));
  g2.addColorStop(.5,rgbaOf(rgb2Now,geoNow.al*.22));
  g2.addColorStop(1,'transparent');
  ctx.fillStyle=g2; ctx.fillRect(0,0,W,H);
  // 星尘: 数量与漂移速度随章节变化, 色调跟随本章主色。
  // ⚠️ alpha 从 (.35~.80) 收到 (.10~.28): 原版的星尘在暗底上过亮, 读起来像"星空贴纸";
  //    收敛后只作环境光的颗粒感, 不参与抢视线。
  var cnt=Math.min(fxStars.length,chStars);
  for(var si=0;si<cnt;si++){
    var s=fxStars[si];
    s.x+=s.vx*geoNow.spd; s.y+=s.vy*geoNow.spd; s.tw+=s.tvs;
    if(s.x<0) s.x=W; else if(s.x>W) s.x=0;
    if(s.y<0) s.y=H; else if(s.y>H) s.y=0;
    ctx.beginPath();
    ctx.arc(s.x,s.y,s.r,0,Math.PI*2);
    ctx.fillStyle=rgbaOf(rgb1Now,(.10+.18*Math.abs(Math.sin(s.tw))).toFixed(3));
    ctx.fill();
  }
  fxRaf=requestAnimationFrame(fxDraw);
}

/* ── 计数动画 (setInterval 兜底, 后台标签页 rAF 停摆) ── */
function countUp(el,to){
  // 小数位数按 data-to 原样取: 两位小数不能截成一位 (否则 4.03 → 4.0 丢精度)。
  var raw=el.getAttribute('data-to');
  var dec=(raw.indexOf('.')>=0)? (raw.split('.')[1].length) : 0;
  var t0=Date.now(), dur=1100, done=false;
  function finish(){ if(done) return; done=true; clearInterval(iv); clearTimeout(guard); el.textContent=to.toFixed(dec); }
  var iv=setInterval(function(){
    var p=Math.min(1,(Date.now()-t0)/dur);
    el.textContent=(to*(1-Math.pow(1-p,3))).toFixed(dec);   // easeOutCubic
    if(p>=1) finish();
  },30);
  var guard=setTimeout(finish,dur+700);  // 兜底: 无论如何都落到真值
}

/* ── 双曲线: 描边生长 + 数据点弹入 ──
   两个关键点, 改动前必读:
     ① **必须在本次任务内、首次绘制之前**把路径置为"未绘制"。
        原先把本函数挂在 setTimeout(350ms) 上, 而 .curve-wrap 的入场延迟只有 .22s ——
        于是 0.22s~0.35s 这个窗口里, 图表带着"已经画完"的姿态淡入, 随后才被重置重画。
        观感就是"先闪一下完整图表, 再开始播动效"(第 8 页实测)。故改为由 show() 同步调用。
     ② 想让绘制晚于入场错峰, 用 transition-delay 表达节奏, 而不是延后"隐藏"的时机。
     ③ 排查时的坑: 判断"画到哪了"只能用 getComputedStyle(p).strokeDashoffset。
        inline 的 style.strokeDashoffset 读到的是**过渡目标值** '0', 看起来像"已经画完";
        computed 才是过渡当前值 (0.35s 延迟期内等于路径总长 = 未绘制)。两者语义不同, 别误判。 */
function animateCurves(wrap){
  var reduced=!motionOK();
  var lead=0.35;                            // 与 .s>* 的入场错峰对齐
  [].slice.call(wrap.querySelectorAll('svg.curve path')).forEach(function(p){
    if(reduced){ p.style.strokeDasharray='none'; return; }
    var len=p.getTotalLength();
    p.style.transition='none';
    p.style.strokeDasharray=len+' '+len;
    p.style.strokeDashoffset=len;           // 先藏好 —— 本帧生效, 不会闪
    void p.getBoundingClientRect();         // 强制重排, 否则两次赋值会被合并, 动画不播
    p.style.transition='stroke-dashoffset 1.7s cubic-bezier(.22,.8,.25,1)';   // 1.4s→1.7s: 放慢 20%
    p.style.transitionDelay=lead+'s';
    p.style.strokeDashoffset='0';
  });
  [].slice.call(wrap.querySelectorAll('svg.curve circle')).forEach(function(c,k){
    if(reduced){ c.style.opacity='1'; c.style.transform='none'; return; }
    c.style.transition='none';
    c.style.opacity='0';
    c.style.transform='scale(0)';           // 缩放原点由 CSS .curve circle{transform-box:fill-box} 保证
    void c.getBoundingClientRect();
    c.style.transition='opacity .48s ease,transform .6s cubic-bezier(.34,1.56,.64,1)';  // .4/.5→.48/.6: 放慢 20%
    c.style.transitionDelay=(lead+k*0.06)+'s';
    c.style.opacity='1';
    c.style.transform='scale(1)';
  });
}

/* ── 省量形状条: 从 0 填到目标宽度 ──
   与 animateCurves 同源: 必须同步置 0 宽度, 否则同样会先闪一下"已填满"的条。 */
function animateFill(root){
  [].slice.call(root.querySelectorAll('.bar2 > div')).forEach(function(d){
    var w=d.style.width;                    // 目标宽度 (必须先读, 后面会改写)
    if(!w) return;
    if(!motionOK()){ d.style.width=w; return; }
    d.style.transition='none';
    d.style.width='0%';                     // 先藏好 —— 本帧生效, 不会闪
    void d.getBoundingClientRect();
    d.style.transition='width 1.44s cubic-bezier(.22,.8,.25,1)';   // 1.2s→1.44s: 放慢 20%
    d.style.transitionDelay='0.35s';
    d.style.width=w;
  });
}

/* ── 翻页: 只增删 .on。离场态由 CSS 基态承担 —— 无额外类、无残留、不会卡空白 ── */
function show(n){
  i=Math.max(0,Math.min(ss.length-1,n));
  timers.forEach(clearTimeout); timers=[];
  ss.forEach(function(s,k){
    var on=(k===i);
    s.classList.toggle('on',on);
    if(!on) return;
    applyChapter(k);
    // 数字滚动: 初值已是真实数字, 这里只把它"从 0 数上来"再落回真值。
    // 若 JS 未执行, 页面显示的仍是真值 (不会出现 0)。
    [].slice.call(s.querySelectorAll('.cnt')).forEach(function(el){
      var to=parseFloat(el.getAttribute('data-to'));
      el.textContent='0';
      timers.push(setTimeout(function(){ countUp(el,to); },300));
    });
    // 图表动效: 必须**同步**置为初始态。挂到 setTimeout 上会让该页带着"已完成"的画面
    // 先淡入一小段, 再重置重画 —— 观感就是"闪一下"(第 8 页实测)。
    // "何时开始绘制"的节奏交给各元素自己的 transition-delay, 不再靠延后隐藏时机。
    (function(scope){
      var wrap=scope.querySelector('.curve-wrap');
      if(wrap) animateCurves(wrap);
      var fill=scope.querySelector('.bar2');
      if(fill) animateFill(fill);
    })(s);
  });
  document.getElementById('bar').style.width=((i+1)/ss.length*100)+'%';
  document.getElementById('pg').textContent=(i+1)+' / '+ss.length;
}

document.addEventListener('keydown',function(e){
  if(e.key==='ArrowRight'||e.key===' '||e.key==='PageDown'){show(i+1);e.preventDefault();}
  else if(e.key==='ArrowLeft'||e.key==='PageUp'){show(i-1);e.preventDefault();}
  else if(e.key==='Home'){show(0);} else if(e.key==='End'){show(ss.length-1);}
  else if(e.key==='f'||e.key==='F'){
    if(!document.fullscreenElement){ document.documentElement.requestFullscreen&&document.documentElement.requestFullscreen(); }
    else { document.exitFullscreen&&document.exitFullscreen(); }
  }
  else if(e.key==='p'||e.key==='P'){window.print();}
});
// 触屏划动翻页后浏览器还会补发一次 click, 不拦住就会一次跳两页
var lastTouch=0;
document.addEventListener('click',function(e){
  if(e.target.tagName==='IMG') return;
  if(Date.now()-lastTouch<600) return;
  show(i+1);
});
var tx=null;
document.addEventListener('touchstart',function(e){ tx=e.touches[0].clientX; });
document.addEventListener('touchend',function(e){
  if(tx===null) return;
  var dx=e.changedTouches[0].clientX-tx;
  if(Math.abs(dx)>50){ show(i+(dx<0?1:-1)); lastTouch=Date.now(); }
  tx=null;
});

initFx();
show(0);
"""

HTML = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AIGate · 产品演示</title><style>{CSS}</style></head><body>
<!-- 氛围层: 环境光 canvas + 章节纹理。均 pointer-events:none,
     不抢交互、不遮正文 (z-index 0, 内容层 z-index 2) -->
<canvas class="fx-bg" id="fxBg"></canvas>
<div class="fx-tex" id="fxTex" aria-hidden="true"></div>
<div class="deck" id="deck">
{slides}
</div>
<div class="bar" id="bar"></div><div class="pg" id="pg"></div>
<div class="hint">← → / 空格 翻页 · F 全屏 · P 打印为 PDF · 可滑动切换</div>
<script>{JS}</script></body></html>
"""

# 校验范围只限演示正文: CSS/JS 里的 ** (如注释中的强调写法) 不是 Markdown, 不该报警。
_leftover = re.findall(r"\*\*[^*]{1,80}\*\*", slides)
if _leftover:
    raise SystemExit("仍有未转换的 Markdown 粗体: %s" % _leftover[:6])
io.open(OUT, "w", encoding="utf-8", newline="").write(HTML)
print("已生成: %s" % OUT)
print("  页数 %d  截图 %d  大小 %d KB" % (len(S), sum(1 for v in IMG.values() if v), os.path.getsize(OUT) // 1024))
print("  版式:", [k for k, _ in S])
