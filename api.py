# -*- coding: utf-8 -*-
"""API 配置（单个文件）与 DeepSeek 调用封装。P2 / P3 共用。

配置文件：项目根下的 `api.txt`（已被 .gitignore 排除）。
格式故意做成**手填不易错**的 KEY=VALUE，而不是 JSON：

    # 注释行
    api_key=sk-xxxxxxxxxxxxxxxx
    model=deepseek-flash
    base_url=https://api.deepseek.com

容错规则：
  * 允许 UTF-8 BOM（PowerShell 写文件会带 BOM）
  * `#` 或 `;` 开头为注释；空行忽略
  * 行内 `key=value`、`key: value` 均可
  * **若某行没有分隔符但看起来像密钥（sk- 开头），直接当作 api_key**
    —— 便于只粘贴一串 key 就完事
  * 也兼容 `api.txt` 不存在时退回 `config.local.json`

优先级：环境变量 > api.txt > config.local.json。
"""

import json
import os
import re

from . import config

API_FILE = os.path.join(config.ROOT, "api.txt")
# 也支持放在包目录内。用户对"单个文件"的自然预期是放在工具文件夹里，
# 但按 Python 的调用约定（必须从父目录运行），配置放项目根更自然 —— 两处都认。
API_FILE_IN_PKG = os.path.join(config.PKG_DIR, "api.txt")


def candidate_files():
    """按优先级返回 api.txt 的候选路径。"""
    return [os.path.join(config.ROOT, "api.txt"),
            os.path.join(config.PKG_DIR, "api.txt")]


def find_api_file() -> str:
    """返回实际存在的 api.txt；都不存在时返回项目根下的默认路径（便于提示用户创建）。"""
    for p in candidate_files():
        if os.path.exists(p):
            return p
    return API_FILE

ENV_API_KEY = "DEEPSEEK_API_KEY"
ENV_BASE_URL = "DEEPSEEK_BASE_URL"
ENV_MODEL = "DEEPSEEK_MODEL"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"          # DeepSeek-V4.1-Flash，便宜档
# 开思考模式时的输出上限。必须给足：max_tokens 含思维链 token，
# 实测 8000 会被思维链吃掉大半导致 JSON 截断（模型最大输出 384K）。
DEFAULT_MAX_TOKENS = 32000

# 本机常用的其它 OpenAI 兼容中转，仅用于提示，不自动生效
_KEY_RE = re.compile(r"^(sk|sess|ak)-[A-Za-z0-9_\-]{8,}$")


def parse_api_file(path: str = None) -> dict:
    """解析 api.txt，返回 {api_key, model, base_url}（缺失的键不出现）。"""
    path = path or find_api_file()
    out = {}
    if not os.path.exists(path):
        return out
    try:
        # utf-8-sig：兼容 PowerShell 写入的 BOM
        with open(path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
    except Exception:                                              # noqa: BLE001
        return out

    for raw in lines:
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        # 去掉行尾注释（仅当 # 前面有空白时，避免截断含 # 的值）
        line = re.split(r"\s+[#;]", line, 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.+)$", line)
        if m:
            k, v = m.group(1).lower(), m.group(2).strip().strip('"').strip("'")
            if k in ("api_key", "apikey", "key", "token"):
                out["api_key"] = v
            elif k in ("model", "model_name"):
                out["model"] = v
            elif k in ("base_url", "baseurl", "url", "endpoint", "host"):
                out["base_url"] = v.rstrip("/")
            continue
        # 裸密钥行
        if _KEY_RE.match(line):
            out.setdefault("api_key", line)
    return out


def load_api() -> dict:
    """合并三层来源，返回 {api_key, model, base_url}。"""
    check_example_leak()
    cfg = config.load_local() or {}
    f = parse_api_file()
    api_key = (os.environ.get(ENV_API_KEY) or f.get("api_key")
               or cfg.get("api_key") or "")
    model = (os.environ.get(ENV_MODEL) or f.get("model")
             or cfg.get("model") or DEFAULT_MODEL)
    base_url = (os.environ.get(ENV_BASE_URL) or f.get("base_url")
                or cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    return {"api_key": api_key.strip(), "model": model.strip(),
            "base_url": base_url}


def example_file() -> str:
    return os.path.join(config.PKG_DIR, "api.example.txt")


def check_example_leak(verbose: bool = True) -> bool:
    """防呆：`api.example.txt` 是**要提交到 git 的模板**，绝不该放真密钥。

    实测确实发生过：用户把密钥填进 api.example.txt，既没生效（代码只读
    api.txt）又存在随模板泄露的风险。这里主动检测并告警。
    返回 True 表示发现问题。
    """
    p = example_file()
    if not os.path.exists(p):
        return False
    if not parse_api_file(p).get("api_key"):
        return False
    if verbose:
        import sys
        print("[安全警告] %s 里存在非空 api_key。" % p, file=sys.stderr)
        print("           该文件是模板，设计上会被提交到 git —— 密钥会因此泄露。", file=sys.stderr)
        print("           请把密钥移到 %s，并把模板恢复为空。"
              % candidate_files()[0], file=sys.stderr)
    return True


def source_of() -> str:
    """返回 api_key 的实际来源，便于自检。"""
    if os.environ.get(ENV_API_KEY):
        return "环境变量 %s" % ENV_API_KEY
    if parse_api_file().get("api_key"):
        return find_api_file()
    if (config.load_local() or {}).get("api_key"):
        return config.LOCAL_CONFIG
    return "(未配置)"


def mask(key: str) -> str:
    if not key:
        return "(空)"
    if len(key) <= 12:
        return key[:3] + "***"
    return "%s...%s (%d 字符)" % (key[:7], key[-4:], len(key))


def client(cfg: dict = None, timeout: float = 180.0):
    """构造 OpenAI 兼容客户端（指向 DeepSeek）。"""
    cfg = cfg or load_api()
    if not cfg["api_key"]:
        raise RuntimeError(
            "未配置 API key。请任选其一写入一行：\n"
            "    api_key=sk-你的密钥\n"
            "  -> %s\n"
            "  -> %s\n"
            "或设置环境变量 %s。可用 `python -m wxautochat api --test` 自检。"
            % (candidate_files()[0], candidate_files()[1], ENV_API_KEY))
    from openai import OpenAI
    return OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                  timeout=timeout, max_retries=2)


def chat(messages, cfg: dict = None, model: str = None, json_mode: bool = False,
         thinking: bool = False, reasoning_effort: str = None,
         temperature: float = None, max_tokens: int = None,
         frequency_penalty: float = None, presence_penalty: float = None,
         verbose: bool = False):
    """调用 Chat Completions，返回 (content, usage_dict, raw_response)。

    thinking=False 时通过 extra_body 关闭思考模式 —— 官方文档明确：
        思考模式下 temperature / presence_penalty / frequency_penalty **全部无效**
        （传了不报错但不生效），故拟语气类请求必须关思考。
        反过来说，**只在非思考模式下才能用 frequency_penalty 压制重复**。

    ⚠ 重要：`max_tokens` **包含思考（reasoning）token**。开思考且 effort=high 时，
        实测 8000 的上限会被思维链吃掉大半，导致输出 JSON 中途截断
        （`finish_reason == "length"`）。所以默认给足 32000；模型最大输出 384K。
    """
    cfg = cfg or load_api()
    cli = client(cfg)
    kwargs = {"model": model or cfg["model"], "messages": messages}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if thinking:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
    else:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if frequency_penalty is not None:
            kwargs["frequency_penalty"] = frequency_penalty
        if presence_penalty is not None:
            kwargs["presence_penalty"] = presence_penalty
    kwargs["max_tokens"] = max_tokens or (DEFAULT_MAX_TOKENS if thinking
                                          else 8192)

    if verbose:
        print("  [api] model=%s json=%s thinking=%s max_tokens=%s"
              % (kwargs["model"], json_mode, thinking, kwargs["max_tokens"]),
              flush=True)
    resp = cli.chat.completions.create(**kwargs)
    content = (resp.choices[0].message.content or "") if resp.choices else ""
    finish = (getattr(resp.choices[0], "finish_reason", "") or "") if resp.choices else ""
    u = getattr(resp, "usage", None)
    usage = {}
    if u is not None:
        for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                  "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            v = getattr(u, k, None)
            if v is not None:
                usage[k] = v
        # 思考 token 数（部分 OpenAI 兼容实现放在 completion_tokens_details）
        det = getattr(u, "completion_tokens_details", None)
        rt = getattr(det, "reasoning_tokens", None) if det else None
        if rt is not None:
            usage["reasoning_tokens"] = rt
    usage["finish_reason"] = finish
    return content, usage, resp


# ---------------------------------------------------------------- 费用估算

# 官方价（元 / 百万 token，实测抓取自 api-docs.deepseek.com）
PRICES = {
    "deepseek-flash": {
        "hit_off": 0.02, "hit_peak": 0.04,
        "miss_off": 1.0, "miss_peak": 2.0,
        "out_off": 4.0, "out_peak": 8.0,
    },
    "deepseek-v4-pro": {
        "hit_off": 0.15, "hit_peak": 0.30,
        "miss_off": 4.5, "miss_peak": 9.0,
        "out_off": 13.5, "out_peak": 27.0,
    },
}


def cost(usage: dict, model: str = None) -> float:
    """按 usage 估算费用（元）。

    高峰时段 = 北京时间周一至周五 9:00-12:00 与 14:00-18:00；其余半价。
    """
    model = model or load_api()["model"]
    p = PRICES.get(model, PRICES["deepseek-flash"])
    import datetime
    now = datetime.datetime.now()
    peak = (now.weekday() < 5 and
            (9 <= now.hour < 12 or 14 <= now.hour < 18))
    hit = usage.get("prompt_cache_hit_tokens", 0) or 0
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(0, (usage.get("prompt_tokens", 0) or 0) - hit)
    out = usage.get("completion_tokens", 0) or 0
    r = (p["hit_peak"] if peak else p["hit_off"]) / 1e6 * hit
    r += (p["miss_peak"] if peak else p["miss_off"]) / 1e6 * miss
    r += (p["out_peak"] if peak else p["out_off"]) / 1e6 * out
    return r


def est_tokens(text: str) -> int:
    """粗略估算 token 数。中文约 1 字 1 token（保守上限）。"""
    if not text:
        return 0
    return len(text)
