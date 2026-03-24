#!/usr/bin/env python3
"""
CLI Tools MCP Server — Token 优化重构版
将 GitHub Copilot CLI 和 Gemini CLI 封装为 MCP 工具

核心策略（减少 Claude Token 消耗）：
  1. gemini_research    → 长文本 100% 落地文件，只返回摘要 + 路径
  2. gemini_analyze_file→ 同上
  3. copilot            → 增加 review_mode，强制冷酷格式，大输出自动落地
  4. copilot_review     → 专用 Code Review 工具，预置 cold prompt，极简输出

安装位置：~/.claude/mcp-servers/cli-mcp-server/server.py
"""

import subprocess
import asyncio
import re
import os
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

app = Server("ai-crew")

# ── 输出目录（所有 Gemini 输出强制落地）────────────────────────────────────
RESEARCH_DIR = Path.home() / ".claude" / "research"   # 调研报告
REVIEW_DIR   = Path.home() / ".claude" / "reviews"    # Code Review
DOCS_DIR     = Path.home() / ".claude" / "docs"       # 架构文档 / 产品手册
for _d in (RESEARCH_DIR, REVIEW_DIR, DOCS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 输出超过此行数时自动写文件（Copilot 模式）
AUTO_FILE_THRESHOLD = 80

# ── Gemini OAuth ────────────────────────────────────────────────────────────
GEMINI_CREDS_PATH        = Path.home() / ".gemini" / "oauth_creds.json"
GOOGLE_TOKEN_URL         = "https://oauth2.googleapis.com/token"
GOOGLE_MODELS_URL        = "https://generativelanguage.googleapis.com/v1beta/models"
# Gemini CLI OAuth App 凭证
# 来源：https://github.com/google-gemini/gemini-cli（MIT License，公开凭证）
# 设置方式（二选一）：
#   1. 环境变量：export GEMINI_CLI_CLIENT_ID=... GEMINI_CLI_CLIENT_SECRET=...
#   2. 本地文件：复制 credentials.example.py 为 credentials_local.py 并填入值
try:
    from credentials_local import GEMINI_CLI_CLIENT_ID, GEMINI_CLI_CLIENT_SECRET
except ImportError:
    GEMINI_CLI_CLIENT_ID     = os.environ.get("GEMINI_CLI_CLIENT_ID", "")
    GEMINI_CLI_CLIENT_SECRET = os.environ.get("GEMINI_CLI_CLIENT_SECRET", "")

_model_cache: tuple[list[str], float] | None = None
CACHE_TTL = 3600

FALLBACK_MODELS = [
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
DEFAULT_MODEL = "gemini-3-pro-preview"

# ── Copilot 配置 ─────────────────────────────────────────────────────────────
COPILOT_BIN    = os.path.expanduser("~/.local/bin/copilot")
COPILOT_MODEL  = "gpt-5.4"
COPILOT_EFFORT = "xhigh"

# ── Copilot Code Review 冷酷模式前缀（强制极简输出）────────────────────────
COLD_REVIEW_PREFIX = (
    "You are a ruthless senior code reviewer. "
    "STRICT OUTPUT RULES:\n"
    "- Output ONLY actionable findings\n"
    "- Format: [FILE:LINE] SEVERITY: one-line description\n"
    "- SEVERITY: BUG | SECURITY | PERF | STYLE | SUGGESTION\n"
    "- If fix needed: show compact diff (≤5 lines context)\n"
    "- NO greetings, NO praise, NO filler text\n"
    "- If code is clean: output exactly '✅ LGTM'\n\n"
    "Review the following:\n"
)


# ── Gemini 认证工具函数 ───────────────────────────────────────────────────────

def _http_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _refresh_access_token(creds: dict) -> str | None:
    try:
        data = urllib.parse.urlencode({
            "client_id":     GEMINI_CLI_CLIENT_ID,
            "client_secret": GEMINI_CLI_CLIENT_SECRET,
            "refresh_token": creds["refresh_token"],
            "grant_type":    "refresh_token",
        }).encode()
        req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            new_token = json.loads(resp.read())
        creds["access_token"] = new_token["access_token"]
        creds["expiry_date"]  = int(time.time() * 1000) + new_token.get("expires_in", 3600) * 1000
        with open(GEMINI_CREDS_PATH, "w") as f:
            json.dump(creds, f)
        return creds["access_token"]
    except Exception:
        return None


def _get_valid_token() -> str | None:
    if not GEMINI_CREDS_PATH.exists():
        return None
    try:
        with open(GEMINI_CREDS_PATH) as f:
            creds = json.load(f)
        if int(time.time() * 1000) + 60_000 >= creds.get("expiry_date", 0):
            return _refresh_access_token(creds)
        return creds.get("access_token")
    except Exception:
        return None


def fetch_gemini_models() -> list[str]:
    global _model_cache
    if _model_cache and (time.time() - _model_cache[1]) < CACHE_TTL:
        return _model_cache[0]
    token = _get_valid_token()
    if not token:
        return FALLBACK_MODELS
    try:
        data = _http_get(f"{GOOGLE_MODELS_URL}?pageSize=200", token)
        models = []
        for m in data.get("models", []):
            model_id = m.get("name", "").split("/")[-1]
            supported = [s.get("name", "") for s in m.get("supportedGenerationMethods", [])]
            if model_id.startswith("gemini") and "generateContent" in supported:
                models.append(model_id)
        if not models:
            return FALLBACK_MODELS

        def sort_key(m: str):
            version = 0.0
            for p in m.split("-")[1:]:
                try:
                    version = float(p); break
                except ValueError:
                    continue
            # preview 优先，pro > flash > flash-lite
            tier = 0 if "pro" in m else (2 if "flash-lite" in m else 1)
            return (-version, "preview" not in m, tier, m)

        models.sort(key=sort_key)
        _model_cache = (models, time.time())
        return models
    except Exception:
        return FALLBACK_MODELS


# ── 通用工具函数 ──────────────────────────────────────────────────────────────

STDERR_NOISE_PATTERNS = [
    r"\[DEP\d+\] DeprecationWarning",
    r"Use `node --trace-deprecation",
    r"\(Use `node --trace-deprecation",
    r"^\s*$",
]
_noise_re = re.compile("|".join(STDERR_NOISE_PATTERNS))


def filter_stderr(stderr: str) -> str:
    lines = [ln for ln in stderr.splitlines() if not _noise_re.search(ln)]
    return "\n".join(lines).strip()


def run_command(cmd: list[str], timeout: int = 1800) -> dict[str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "stdout":     result.stdout.strip(),
            "stderr":     filter_stderr(result.stderr),
            "returncode": str(result.returncode),
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"命令执行超时（{timeout}s）", "returncode": "-1"}
    except FileNotFoundError:
        return {"stdout": "", "stderr": f"命令未找到：{cmd[0]}", "returncode": "-1"}
    except Exception as e:
        return {"stdout": "", "stderr": f"执行出错：{str(e)}", "returncode": "-1"}


def _make_filename(prefix: str, topic: str, ext: str = ".md") -> Path:
    """生成带时间戳的安全文件名"""
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "_", topic)[:40].strip("_")
    return Path(f"{prefix}_{ts}_{slug}{ext}")


def _resolve_output_dir(output_dir: str | None, default: Path) -> Path:
    """
    解析输出目录：
      1. 优先使用 Claude 传入的 output_dir（应为当前项目的 docs/ 路径）
      2. 兜底：全局 ~/.claude/research/
    注意：MCP Server 的 CWD 固定在自身安装目录，无法自动探测项目路径，
          必须由 Claude Code 在调用时显式传入。
    """
    if output_dir:
        p = Path(output_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return default


def _write_to_file(directory: Path, filename: Path, content: str) -> Path:
    """写入文件，返回绝对路径"""
    out_path = directory / filename
    out_path.write_text(content, encoding="utf-8")
    return out_path


def _extract_summary(text: str, max_lines: int = 40) -> str:
    """
    提取摘要：优先提取 TL;DR / 结论 / 推荐 等关键段落，
    否则返回前 max_lines 行。
    """
    lines = text.splitlines()

    # 尝试找 TL;DR / 结论 / 推荐方案 段落
    summary_lines: list[str] = []
    in_summary = False
    for i, line in enumerate(lines):
        if re.search(r"TL;DR|结论|总结|推荐方案|recommendation|summary", line, re.I):
            in_summary = True
        if in_summary:
            summary_lines.append(line)
            # 遇到下一个 ## 标题时停止（最多取 30 行）
            if len(summary_lines) > 1 and line.startswith("##") and len(summary_lines) > 3:
                break
            if len(summary_lines) >= 30:
                break

    if summary_lines:
        return "\n".join(summary_lines)
    # 兜底：前 max_lines 行
    return "\n".join(lines[:max_lines])


def _format_file_response(file_path: Path, content: str, tool_name: str) -> str:
    """
    构建返回给 Claude 的标准响应：
    - 文件路径（Claude 可随时按需读取）
    - 摘要（Claude 优先读这部分，节省 Token）
    """
    total_lines = len(content.splitlines())
    summary     = _extract_summary(content)
    return (
        f"[{tool_name}] 完整输出已写入文件（共 {total_lines} 行）：\n"
        f"  📄 {file_path}\n\n"
        f"── 摘要（Claude 读此部分即可）──\n"
        f"{summary}\n\n"
        f"── 如需完整内容，读取上方文件路径 ──"
    )


# ── MCP 工具定义 ─────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    models      = fetch_gemini_models()
    default     = models[0] if models else DEFAULT_MODEL
    model_prop  = {
        "type":        "string",
        "description": f"Gemini 模型（默认 {default}）",
        "default":     default,
        "enum":        models,
    }

    return [
        # ── 1. Copilot 通用工具 ────────────────────────────────────────────
        types.Tool(
            name="copilot",
            description=(
                "使用 GitHub Copilot CLI 执行编程任务：代码解释、重构建议、"
                "技术方案调研、文档生成等。\n"
                "⚠️  Code Review 请优先使用 copilot_review（输出更精简）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type":        "string",
                        "description": "任务描述",
                    },
                    "code": {
                        "type":        "string",
                        "description": "（可选）附带的代码片段",
                    },
                    "review_mode": {
                        "type":        "boolean",
                        "description": "启用冷酷 Review 模式：强制极简输出，去除废话（默认 false）",
                        "default":     False,
                    },
                    "model": {
                        "type":    "string",
                        "default": COPILOT_MODEL,
                    },
                    "effort": {
                        "type":    "string",
                        "enum":    ["low", "medium", "high", "xhigh"],
                        "default": COPILOT_EFFORT,
                    },
                },
                "required": ["prompt"],
            },
        ),

        # ── 2. Copilot 专用 Code Review 工具 ────────────────────────────────
        types.Tool(
            name="copilot_review",
            description=(
                "【Token 优化】专用 Code Review 工具。\n"
                "- 预置冷酷模式 prompt，强制输出 [FILE:LINE] SEVERITY: 格式\n"
                "- 大输出（>80 行）自动写入文件，只返回摘要\n"
                "- 接受：代码片段 或 本地文件路径\n"
                "推荐在 Claude 完成编码后调用此工具做质量把关。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type":        "string",
                        "description": "要审查的代码片段（与 file_path 二选一）",
                    },
                    "file_path": {
                        "type":        "string",
                        "description": "要审查的本地文件路径（会自动读取内容）",
                    },
                    "context": {
                        "type":        "string",
                        "description": "（可选）额外上下文，如功能描述、审查重点",
                    },
                    "model": {
                        "type":    "string",
                        "default": COPILOT_MODEL,
                    },
                    "effort": {
                        "type":    "string",
                        "enum":    ["low", "medium", "high", "xhigh"],
                        "default": COPILOT_EFFORT,
                    },
                },
            },
        ),

        # ── 3. Gemini 调研工具（文件落地版）─────────────────────────────────
        types.Tool(
            name="gemini_research",
            description=(
                "【Token 优化】使用 Gemini CLI 进行技术调研。\n"
                "⚠️  完整报告自动写入 ~/.claude/research/ 文件，\n"
                "    只返回摘要 + 文件路径，避免 Claude Context 膨胀。\n"
                "适合：技术选型、架构对比、最佳实践查询。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type":        "string",
                        "description": "调研主题，越详细越好",
                    },
                    "output_format": {
                        "type":        "string",
                        "description": "要求 Gemini 输出的格式提示（追加到 prompt 末尾）",
                        "default":     "请在开头用3句话写出 TL;DR 结论，再展开详细分析。",
                    },
                    "output_dir": {
                        "type":        "string",
                        "description": (
                            "【必填，除非无项目上下文】当前项目的 docs 目录绝对路径，"
                            "例如 '/Users/yourname/Projects/MyApp/docs'。"
                            "Claude 应根据当前工作项目自动填入，无需用户指定。"
                            "未传入时兜底写入 ~/.claude/research/。"
                        ),
                    },
                    "model": model_prop,
                },
                "required": ["prompt"],
            },
        ),

        # ── 4. Gemini 文件分析工具（文件落地版）──────────────────────────────
        types.Tool(
            name="gemini_analyze_file",
            description=(
                "【Token 优化】使用 Gemini CLI 分析本地文件。\n"
                "⚠️  分析结果自动写入 ~/.claude/research/ 文件，只返回摘要。\n"
                "适合：大文件分析、多文件组合分析、日志解读。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type":        "string",
                        "description": "要分析的文件绝对路径",
                    },
                    "question": {
                        "type":        "string",
                        "description": "对文件的问题或分析要求",
                    },
                    "output_dir": {
                        "type":        "string",
                        "description": (
                            "【必填，除非无项目上下文】当前项目的 docs 目录绝对路径。"
                            "Claude 应根据当前工作项目自动填入。未传入时兜底写入 ~/.claude/research/。"
                        ),
                    },
                    "model": model_prop,
                },
                "required": ["file_path", "question"],
            },
        ),

        # ── 5. Gemini 文档写作工具（落地指定 .md 文件）──────────────────────
        types.Tool(
            name="gemini_write_doc",
            description=(
                "【Token 优化】使用 Gemini 生成或更新文档（架构文档、产品手册、技术规范等）。\n"
                "⚠️  所有输出强制写入指定 .md 文件，Claude 只收到写入确认 + 摘要。\n"
                "适合：架构设计文档、产品手册、API 文档、会议纪要、技术规范书。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type":        "string",
                        "description": "文档写作指令，例如：'根据以下内容更新架构文档...'",
                    },
                    "output_file": {
                        "type":        "string",
                        "description": (
                            "目标文件路径（推荐绝对路径，如项目内 docs/architecture.md）。"
                            "若只填文件名，则写入 ~/.claude/docs/。"
                            "若文件已存在，Gemini 将在 prompt 中附带原内容进行更新。"
                        ),
                    },
                    "append": {
                        "type":        "boolean",
                        "description": "True=追加到文件末尾，False=覆盖（默认 False）",
                        "default":     False,
                    },
                    "model": model_prop,
                },
                "required": ["prompt", "output_file"],
            },
        ),
    ]


# ── MCP 工具执行 ─────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    models        = fetch_gemini_models()
    default_model = models[0] if models else DEFAULT_MODEL

    # ── copilot ────────────────────────────────────────────────────────────
    if name == "copilot":
        prompt      = arguments["prompt"]
        code        = arguments.get("code", "")
        review_mode = arguments.get("review_mode", False)
        model       = arguments.get("model", COPILOT_MODEL)
        effort      = arguments.get("effort", COPILOT_EFFORT)

        if review_mode:
            full_prompt = COLD_REVIEW_PREFIX + (f"{prompt}\n\n```\n{code}\n```" if code else prompt)
        else:
            full_prompt = f"{prompt}\n\n代码：\n{code}" if code else prompt

        result = run_command(
            [COPILOT_BIN, "--model", model, "--effort", effort,
             "--allow-all-tools", "--silent", "-p", full_prompt],
            timeout=1800,
        )
        output = result["stdout"]

        # 大输出自动落地文件
        if output and len(output.splitlines()) > AUTO_FILE_THRESHOLD:
            fname    = _make_filename("copilot", prompt[:30])
            out_path = _write_to_file(REVIEW_DIR, fname, output)
            text     = _format_file_response(out_path, output, "copilot")
        else:
            text = output or f"（空输出）\n{result['stderr']}"

        return [types.TextContent(type="text", text=text)]

    # ── copilot_review ─────────────────────────────────────────────────────
    elif name == "copilot_review":
        code      = arguments.get("code", "")
        file_path = arguments.get("file_path", "")
        context   = arguments.get("context", "")
        model     = arguments.get("model", COPILOT_MODEL)
        effort    = arguments.get("effort", COPILOT_EFFORT)

        # 读取文件内容
        if file_path and not code:
            try:
                code = Path(file_path).expanduser().read_text(encoding="utf-8")
            except Exception as e:
                return [types.TextContent(type="text", text=f"读取文件失败：{e}")]

        if not code:
            return [types.TextContent(type="text", text="请提供 code 或 file_path 参数")]

        target_hint = f"文件：{file_path}\n" if file_path else ""
        context_hint = f"\n审查重点：{context}" if context else ""
        full_prompt = (
            COLD_REVIEW_PREFIX
            + target_hint
            + context_hint
            + f"\n```\n{code}\n```"
        )

        result = run_command(
            [COPILOT_BIN, "--model", model, "--effort", effort,
             "--allow-all-tools", "--silent", "-p", full_prompt],
            timeout=1800,
        )
        output = result["stdout"]

        # 大输出自动落地
        if output and len(output.splitlines()) > AUTO_FILE_THRESHOLD:
            fname    = _make_filename("review", file_path or "code_snippet")
            out_path = _write_to_file(REVIEW_DIR, fname, output)
            text     = _format_file_response(out_path, output, "copilot_review")
        else:
            text = output or f"（空输出）\n{result['stderr']}"

        return [types.TextContent(type="text", text=text)]

    # ── gemini_research ────────────────────────────────────────────────────
    elif name == "gemini_research":
        prompt        = arguments["prompt"]
        output_format = arguments.get("output_format", "请在开头用3句话写出 TL;DR 结论，再展开详细分析。")
        model         = arguments.get("model", default_model)

        full_prompt = f"{prompt}\n\n{output_format}"
        result      = run_command(
            ["gemini", "-m", model, "-p", full_prompt, "--output-format", "text"],
            timeout=1800,
        )

        if result["returncode"] != "0" or not result["stdout"]:
            err = result["stderr"] or "Gemini 返回空内容"
            return [types.TextContent(type="text", text=f"Gemini 调用失败：{err}")]

        # 无论输出长短，一律落地文件（调研报告不应撑大 Context）
        out_dir  = _resolve_output_dir(arguments.get("output_dir"), RESEARCH_DIR)
        fname    = _make_filename("research", prompt[:40])
        out_path = _write_to_file(out_dir, fname, result["stdout"])
        text     = _format_file_response(out_path, result["stdout"], "gemini_research")

        return [types.TextContent(type="text", text=text)]

    # ── gemini_analyze_file ────────────────────────────────────────────────
    elif name == "gemini_analyze_file":
        file_path = arguments["file_path"]
        question  = arguments["question"]
        model     = arguments.get("model", default_model)

        # 将文件引用合并到 -p prompt 中（不能同时用 positional 和 -p）
        full_prompt = f"@{file_path}\n\n{question}\n\n请在开头用3句话写出核心结论："
        result = run_command(
            ["gemini", "-m", model, "-p", full_prompt, "--output-format", "text"],
            timeout=1800,
        )

        if result["returncode"] != "0" or not result["stdout"]:
            err = result["stderr"] or "Gemini 返回空内容"
            return [types.TextContent(type="text", text=f"Gemini 分析失败：{err}")]

        out_dir  = _resolve_output_dir(arguments.get("output_dir"), RESEARCH_DIR)
        fname    = _make_filename("analysis", Path(file_path).name)
        out_path = _write_to_file(out_dir, fname, result["stdout"])
        text     = _format_file_response(out_path, result["stdout"], "gemini_analyze_file")

        return [types.TextContent(type="text", text=text)]

    # ── gemini_write_doc ───────────────────────────────────────────────────
    elif name == "gemini_write_doc":
        prompt      = arguments["prompt"]
        output_file = arguments["output_file"]
        append      = arguments.get("append", False)
        model       = arguments.get("model", default_model)

        # 解析目标路径
        out_path = Path(output_file).expanduser()
        if not out_path.is_absolute():
            out_path = DOCS_DIR / output_file
        out_path = out_path.with_suffix(".md") if out_path.suffix == "" else out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 若文件已存在且是更新模式，把原内容带入 prompt
        full_prompt = prompt
        if out_path.exists() and not append:
            existing = out_path.read_text(encoding="utf-8")
            full_prompt = (
                f"{prompt}\n\n"
                f"以下是需要更新的现有文档内容，请在此基础上修改后输出完整文档：\n"
                f"```\n{existing[:8000]}\n```\n"
                f"（若原文档超过 8000 字符，后续内容已截断，请保持整体结构一致）"
            )

        result = run_command(
            ["gemini", "-m", model, "-p", full_prompt, "--output-format", "text"],
            timeout=1800,
        )

        if result["returncode"] != "0" or not result["stdout"]:
            err = result["stderr"] or "Gemini 返回空内容"
            return [types.TextContent(type="text", text=f"Gemini 文档写作失败：{err}")]

        new_content = result["stdout"]

        if append:
            with open(out_path, "a", encoding="utf-8") as f:
                f.write("\n\n" + new_content)
            action = "追加"
        else:
            out_path.write_text(new_content, encoding="utf-8")
            action = "写入"

        total_lines = len(new_content.splitlines())
        summary     = _extract_summary(new_content)
        text = (
            f"[gemini_write_doc] 文档已{action}（{total_lines} 行）：\n"
            f"  📄 {out_path}\n\n"
            f"── 文档摘要 ──\n{summary}\n\n"
            f"── Claude：直接引用文件路径，无需读取完整内容 ──"
        )
        return [types.TextContent(type="text", text=text)]

    else:
        return [types.TextContent(type="text", text=f"未知工具：{name}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
