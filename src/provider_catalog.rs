//! 内置「供应商 host → 协议」目录 —— 由脚本生成, **请勿手工编辑**.
//!
//! 用途: `probe_protocols` 之前的第一道免费判据。协议信息在模型级并不存在
//! (models.dev 模型字段无协议; 且同名模型跨服务方协议会不同 —— 例如
//! claude-opus-4-8 在 anthropic 官方走 anthropic 协议, 在 snowflake/302ai 等转售商
//! 走 openai 协议), 故只能按**服务方 host** 记录。
//!
//! 数据来源: https://models.dev/api.json 的 `provider.npm` (AI SDK 包名 → 协议)。
//! 生成: `python .openbitfun/tmp/gen_catalog.py` — 更新 models.dev 后重跑即可。
//! 覆盖不到的服务方 (小众中转 / 自建网关) 由运行时探测兜底, **不影响正确性**。
//!
//! 收录 178 条 (生成于 2026-09-28)。

/// (host 后缀, 协议) 表。按 host **后缀**匹配 —— 这样 `api.deepseek.com` 与
/// `deepseek.com` 都能命中, 且用户填的带路径/端口的 URL 也能归一到 host。
pub const HOST_PROTOCOLS: &[(&str, &str)] = &[
    ("agentrouter.org", "openai"),
    ("ai-gateway.helicone.ai", "openai"),
    ("ai.zenifra.com", "openai"),
    ("aki.io", "openai"),
    ("api-gw.klok.ipaas.se", "openai"),
    ("api-inference.modelscope.cn", "openai"),
    ("api-sherlock.cloudferro.com", "openai"),
    ("api.302.ai", "openai"),
    ("api.abliteration.ai", "openai"),
    ("api.above.dev", "openai"),
    ("api.ai-router.dev", "openai"),
    ("api.ai21.com", "openai"),
    ("api.aiand.com", "openai"),
    ("api.aixy-gateway.com", "openai"),
    ("api.ambient.xyz", "openai"),
    ("api.anyapi.ai", "openai"),
    ("api.arcee.ai", "openai"),
    ("api.auriko.ai", "openai"),
    ("api.berget.ai", "openai"),
    ("api.clarifai.com", "openai"),
    ("api.claudin.io", "openai"),
    ("api.cline.bot", "openai"),
    ("api.cloudflare.com", "openai"),
    ("api.code.umans.ai", "openai"),
    ("api.cortecs.ai", "openai"),
    ("api.crossmodel.ai", "openai"),
    ("api.deepseek.com", "openai"),
    ("api.dinference.com", "openai"),
    ("api.edenai.run", "openai"),
    ("api.empiriolabs.ai", "openai"),
    ("api.fireworks.ai", "openai"),
    ("api.friendli.ai", "openai"),
    ("api.getlilac.com", "openai"),
    ("api.githubcopilot.com", "openai"),
    ("api.gmi-serving.com", "openai"),
    ("api.greenpt.ai", "openai"),
    ("api.hpc-ai.com", "openai"),
    ("api.impossibl.com", "openai"),
    ("api.inceptionlabs.ai", "openai"),
    ("api.inceptron.io", "openai"),
    ("api.inco.ai", "openai"),
    ("api.inference.crusoecloud.com", "openai"),
    ("api.inference.wandb.ai", "openai"),
    ("api.infomaniak.com", "openai"),
    ("api.intelligence.io.solutions", "openai"),
    ("api.iteracompute.com", "openai"),
    ("api.jalapeno-cloud.ai", "openai"),
    ("api.jiekou.ai", "openai"),
    ("api.kilo.ai", "openai"),
    ("api.kimi.ai", "openai"),
    ("api.kimi.com", "openai"),
    ("api.koscompute.com", "openai"),
    ("api.lab.vispark.in", "openai"),
    ("api.lkeap.cloud.tencent.com", "openai"),
    ("api.llama.com", "openai"),
    ("api.llmgateway.io", "openai"),
    ("api.llmtech.eu", "openai"),
    ("api.longcat.chat", "openai"),
    ("api.lucidquery.com", "openai"),
    ("api.meganova.ai", "openai"),
    ("api.melious.ai", "openai"),
    ("api.meta.ai", "openai"),
    ("api.minimax.cn", "anthropic"),
    ("api.minimax.io", "anthropic"),
    ("api.modeloracle.com", "openai"),
    ("api.moonshot.ai", "openai"),
    ("api.moonshot.cn", "openai"),
    ("api.morphllm.com", "openai"),
    ("api.nan.builders", "openai"),
    ("api.neuralwatt.com", "openai"),
    ("api.nova.amazon.com", "openai"),
    ("api.novita.ai", "openai"),
    ("api.ofox.ai", "openai"),
    ("api.openai-compat.model-serving.eu01.onstackit.cloud", "openai"),
    ("api.openreason.app", "openai"),
    ("api.opper.ai", "openai"),
    ("api.orcarouter.ai", "openai"),
    ("api.pendra.ai", "openai"),
    ("api.perplexity.ai", "openai"),
    ("api.pioneer.ai", "openai"),
    ("api.poe.com", "openai"),
    ("api.qhaigc.net", "openai"),
    ("api.qnaigc.com", "openai"),
    ("api.regolo.ai", "openai"),
    ("api.routing.run", "openai"),
    ("api.runinfra.ai", "openai"),
    ("api.sakana.ai", "openai"),
    ("api.sarvam.ai", "openai"),
    ("api.scaleway.ai", "openai"),
    ("api.scnet.cn", "openai"),
    ("api.scx.ai", "openai"),
    ("api.siliconflow.cn", "openai"),
    ("api.siliconflow.com", "openai"),
    ("api.stdcmpt.com", "openai"),
    ("api.stepfun.ai", "openai"),
    ("api.stepfun.com", "openai"),
    ("api.subconscious.dev", "anthropic"),
    ("api.synthetic.new", "openai"),
    ("api.tbox.cn", "openai"),
    ("api.temprhq.io", "openai"),
    ("api.tensorx.ai", "openai"),
    ("api.thegrid.ai", "openai"),
    ("api.tokenfactory.nebius.com", "openai"),
    ("api.tokengo.com", "openai"),
    ("api.tokenrouter.com", "openai"),
    ("api.trustedrouter.com", "openai"),
    ("api.unorouter.com", "openai"),
    ("api.upstage.ai", "openai"),
    ("api.vivgrid.com", "openai"),
    ("api.vultrinference.com", "openai"),
    ("api.wallabytoken.com", "openai"),
    ("api.xiaomimimo.com", "openai"),
    ("api.z.ai", "openai"),
    ("api.zeldoc.ai", "openai"),
    ("apihub.agnes-ai.com", "openai"),
    ("apis.iflow.cn", "openai"),
    ("app.frogbot.ai", "openai"),
    ("ark.cn-beijing.volces.com", "openai"),
    ("cc.freemodel.dev", "anthropic"),
    ("chat.d.run", "openai"),
    ("cloud-api.near.ai", "openai"),
    ("coding-intl.dashscope.aliyuncs.com", "openai"),
    ("coding-plan-endpoint.kuaecloud.net", "openai"),
    ("coding.dashscope.aliyuncs.com", "openai"),
    ("crof.ai", "openai"),
    ("daoxe.com", "openai"),
    ("dashscope-intl.aliyuncs.com", "openai"),
    ("dashscope.aliyuncs.com", "openai"),
    ("developer.amd.com.cn", "openai"),
    ("echo.tracerml.ai", "openai"),
    ("go.fastrouter.ai", "openai"),
    ("hyper.charm.land", "openai"),
    ("infer.flow7.org", "openai"),
    ("inference.baseten.co", "openai"),
    ("inference.coralbricks.ai", "openai"),
    ("inference.do-ai.run", "openai"),
    ("inference.generativeai.us-chicago-1.oci.oraclecloud.com", "openai"),
    ("inference.hetzner.com", "openai"),
    ("inference.net", "openai"),
    ("inference.poolside.ai", "openai"),
    ("inference.tinfoil.sh", "openai"),
    ("inference.us-west.modal.direct", "openai"),
    ("integrate.api.nvidia.com", "openai"),
    ("kenari.id", "openai"),
    ("llm.chutes.ai", "openai"),
    ("llm.submodel.ai", "openai"),
    ("llmtr.com", "openai"),
    ("maas-api.ebcloud.com", "openai"),
    ("microquickjs.com", "openai"),
    ("moark.com", "openai"),
    ("model.inferx.net", "openai"),
    ("modelishub.com", "openai"),
    ("models.mixlayer.ai", "openai"),
    ("models.think.evroc.com", "openai"),
    ("nano-gpt.com", "openai"),
    ("oai.endpoints.kepler.ai.cloud.ovh.net", "openai"),
    ("ollama.com", "openai"),
    ("open.bigmodel.cn", "openai"),
    ("openai.blueclaw.network", "openai"),
    ("openai.bothub.ru", "openai"),
    ("opencode.ai", "openai"),
    ("openrouter.ai", "openai"),
    ("pass.wafer.ai", "openai"),
    ("routellm.abacus.ai", "openai"),
    ("router.huggingface.co", "openai"),
    ("router.neosmith.ai", "openai"),
    ("router.requesty.ai", "openai"),
    ("tinker.thinkingmachines.dev", "anthropic"),
    ("token-plan-ams.xiaomimimo.com", "openai"),
    ("token-plan-cn.xiaomimimo.com", "openai"),
    ("token-plan-sgp.xiaomimimo.com", "openai"),
    ("token-plan.ap-southeast-1.maas.aliyuncs.com", "openai"),
    ("token-plan.cn-beijing.maas.aliyuncs.com", "openai"),
    ("token.sensenova.cn", "openai"),
    ("tokenhub.tencentmaas.com", "openai"),
    ("vancine.com", "openai"),
    ("www.xpersona.co", "openai"),
    ("zenmux.ai", "openai"),
];

/// 按 host 查协议: 逐级剥子域 + 常见子域补全 (两个方向都试)。
///
/// 为什么两个方向都要试: 目录里多为 `api.xxx.com` 形式, 而用户可能只填裸域名
/// (`https://deepseek.com/v1`); 反之目录里也有裸域名而用户填了 `api.` 前缀。
/// 单方向匹配必然漏一半, 故双向尝试 (先剥后补, 都是廉价字符串操作)。
pub fn protocol_for_host(host: &str) -> Option<&'static str> {
    let h = host.trim().trim_end_matches('.').to_ascii_lowercase();
    if h.is_empty() {
        return None;
    }
    fn exact(h: &str) -> Option<&'static str> {
        HOST_PROTOCOLS.iter().find(|(k, _)| *k == h).map(|(_, p)| *p)
    }
    // 方向 1: 逐级剥子域 (api.deepseek.com → deepseek.com)
    let mut cur = h.as_str();
    loop {
        if let Some(p) = exact(cur) {
            return Some(p);
        }
        match cur.split_once('.') {
            Some((_, rest)) => cur = rest,
            None => break,
        }
    }
    // 方向 2: 补常见子域 (deepseek.com → api.deepseek.com)
    //  只补有限几个前缀, 避免退化成任意后缀匹配而误命中无关域名。
    for pfx in ["api", "www", "api-v1", "openai"] {
        if let Some(proto) = exact(&format!("{pfx}.{h}")) {
            return Some(proto);
        }
    }
    None
}
