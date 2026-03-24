# ai-crew — MCP Server for Claude Code

将 **Gemini CLI** 和 **GitHub Copilot CLI** 封装为 Claude Code 可调用的 MCP 工具。

核心理念：三个模型各司其职，Token 消耗最优化。

```
Gemini   → 技术调研 / 文档生成（长上下文，输出落地文件）
Copilot  → Code Review（垂直代码训练，冷酷模式极简输出）
Claude   → 决策 + 编码 + 调度（只做高价值事）
```

---

## 工具列表

| 工具 | 说明 |
|------|------|
| `copilot` | 通用编程任务：代码解释、重构建议、技术调研 |
| `copilot_review` | 专用 Code Review，预置冷酷 prompt，强制 `[FILE:LINE] SEVERITY:` 格式 |
| `gemini_research` | 技术调研，完整报告落地 `docs/`，只返回摘要给 Claude |
| `gemini_analyze_file` | 分析本地文件，结果落地文件 |
| `gemini_write_doc` | 生成或更新 `.md` 文档，支持覆盖 / 追加 |

---

## 安装

### 前置条件

- [Claude Code](https://claude.ai/code) 已安装
- [GitHub Copilot CLI](https://githubnext.com/projects/copilot-cli) 已安装并认证
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) 已安装并认证

### 步骤

```bash
# 1. 克隆到 Claude MCP 目录
git clone https://github.com/wangtao9090/ai-crew.git ~/.claude/mcp-servers/ai-crew

# 2. 创建虚拟环境并安装依赖
cd ~/.claude/mcp-servers/ai-crew
python3.11 -m venv .venv
.venv/bin/pip install mcp

# 3. 添加到 Claude Code 全局配置（~/.claude.json）
```

在 `~/.claude.json` 的 `mcpServers` 中添加：

```json
"ai-crew": {
  "command": "/Users/你的用户名/.claude/mcp-servers/ai-crew/.venv/bin/python3.11",
  "args": ["/Users/你的用户名/.claude/mcp-servers/ai-crew/server.py"],
  "env": {
    "PATH": "/Users/你的用户名/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  }
}
```

---

## Token 优化策略

**Gemini 输出强制落地文件**：所有 `gemini_*` 工具的完整输出写入本地 `.md` 文件，Claude 只收到摘要（≤40 行）+ 文件路径，避免长文本撑大 Context Window。

**Copilot 冷酷模式**：`copilot_review` 预置严格 prompt，禁止客套话，只输出可操作的修改意见，大幅减少 Claude 的阅读成本。

**推荐工作流**：

```
需求 → gemini_research（output_dir 指向项目 docs/）
     → Claude 读摘要，开始编码
     → copilot_review（审查）
     → Claude 修复
     → 完成
```

---

## 配置

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `GEMINI_CLI_CLIENT_ID` | 自定义 OAuth App Client ID | Gemini CLI 官方公开值 |
| `GEMINI_CLI_CLIENT_SECRET` | 自定义 OAuth App Client Secret | Gemini CLI 官方公开值 |

Gemini 认证凭证从 `~/.gemini/oauth_creds.json` 读取（由 `gemini auth login` 生成）。

---

## License

MIT
