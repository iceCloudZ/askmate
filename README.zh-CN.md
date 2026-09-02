# askmate

**答一次,分身替你答一辈子。**

askmate 是一套两人私享问答系统:**AI agent 负责提问和回答,人只在知识库接不住时出场**。它来自一个很常见的处境:两个工程师,总在互相打断对方问问题。

```
对方的 agent ── ask(自包含上下文打包)──▶ 数据层
                                        │ 知识库强命中 → 分身代答(秒回)
对方的 agent ◀── 多轮追问/反馈 ────────  │ 接近命中 → 候选
你的 agent   ◀── inbox(导材料给agent)──┘ 未命中 → 真人收件箱
                                        └ 每条真人回复自动沉淀进知识库
```

[English](README.md)

## 为什么做这个

三个想法,没有一个是什么高深技术——所以现有工具恰好都没把它们拼在一起:

**1. Agent 提问,人只兜底。** 现有 human-in-the-loop 工具(如 [LangChain Agent Inbox](https://github.com/langchain-ai/agent-inbox))解决的是"**自己的** agent 暂停下来问**自己人**"。askmate 是另一个方向:**我的** agent 去问**你的** agent,你的分身用你教过的知识作答,只有知识库落空时才把**你**拉进来。全程异步,谁也不阻塞谁。

**2. 自包含上下文,由 skill 强制执行。** 提问侧的 SKILL.md 给 agent 写死了提问纪律:目标、报错原文(逐字)、相关代码、环境、已尝试过什么——整理成 `context.md`,截图和日志原件做附件。一次发全,禁止审问式来回追问。事后看,这是整个系统最大的质量杠杆。

**3. 回复即沉淀。** 任何问题的**首条真人回复**自动蒸馏成知识库条目。下一个问同类问题的人(或 agent)立刻拿到**分身代答**,零人力介入。"没帮助"的反馈、追问、转人工都会把条目打成 `NEEDS_REVIEW`,**分身对该条目停用**,直到人工修正并重新激活。知识库完全沿着"真实被问过什么"生长——不用先喂语料,不用搭 RAG。

## 功能

- **多轮对话线程**——追问让已解决线程重开,上下文不丢
- **自动查重**——回答已有条目的问题时提示合并,不产生碎条目
- **附件**——截图 ≤5MB、日志/代码 ≤2MB,以 capability 链接内嵌,进知识库索引前自动剥离
- **反馈闭环**——强制流程(CLI 催到你反馈为止),持续校准分身
- **一套 CLI,两种后端**:
  - **GitHub 原生**——数据 = 一个*私有 GitHub 仓库*,每条命令一次 commit,问题历史即 git log。零服务器零数据库,备份 = `git clone`,鉴权 = fine-grained PAT,根本没有密码体系
  - **自托管服务器**——单文件纯标准库 Python 服务 + SQLite(`server/server.py`),systemd + 反代上 TLS
- **CLI 自升级**——`askme upgrade` 每日检测、展示变更日志、原子自替换(自托管后端可选开启分发通道)

## 快速开始 —— GitHub 后端(5 分钟,无需服务器)

1. 建一个**私有**仓库(如 `askmate-data`),把对方加为协作者
2. 各自生成 fine-grained PAT:GitHub → Settings → Developer settings → **Fine-grained tokens** → 只授权 `askmate-data`,权限 **Contents: Read and write**
3. 各自安装 CLI(见 [skills/](skills/),把 `cli/` 下两个 py 文件放到 SKILL.md 旁)并登录:

   ```bash
   askme login --backend github --gh-token <PAT> --gh-repo <owner>/askmate-data
   ```

4. 跨端提问:

   ```bash
   askme ask <对方GitHub登录名> "线程池一直超时怎么排查" \
       --file context.md --file app.log --img error.png
   ```

仓库首登自动初始化。搞定——你们的 agent 从此共享一个大脑。

## 快速开始 —— 自托管服务器

```bash
git clone https://github.com/<you>/askmate && cd askmate
./server/deploy.sh <服务器> <ssh用户>   # 探测 → 上传 → systemd → 健康检查
# 按输出清单收尾: adduser ×2、DNS、TLS(见 server/reverse-proxy.md)
```

两端各跑 `askme login --user <名> --password <密码>`(服务器默认 `http://127.0.0.1:8730`,`--server` / `ASKME_SERVER` 可覆盖)。

## CLI 速查

```
askme login            # --backend github --gh-token … --gh-repo …  |  --user … --password …
askme whoami
askme ask <用户> "问题" [--img …] [--file …] [--json]   # 分身/候选/收件箱自动路由
askme inbox [--notify]                # --notify: 有新问题时桌面通知(适合定时任务)
askme inbox show <id> [--save-attachments DIR]       # 导出材料目录给本地 agent
askme reply <id> "…"                  # 回答(被问方)/追问(提问方),身份自动判
askme sent                            # 我的问题 + 未反馈催办
askme feedback <id> helpful|not       # 强制的最后一步
askme kb list|search|show|push|edit|rm
askme kb search "关键词" --owner <用户>   # 检索对方知识库(与分身代答同一暴露面)
askme upgrade [--check]
```

## 一个问题的流转(状态机)

```
ask ──▶ 知识库强命中? ── 是 ─▶ AUTO_ANSWERED(分身作答, hits+1)
              │                     │ 追问 / 转人工 / feedback:not
              否                    ▼
              └──▶ OPEN(真人收件箱) ─ reply ─▶ RESOLVED(首答沉淀 KB)
                        ▲                       │ 提问方追问
                        └───────────────────────┘
NEEDS_REVIEW:条目对分身停用,直到 kb edit --status ACTIVE
```

## 仓库结构

```
skills/ask-partner/SKILL.md      提问方 agent 指令(上下文打包纪律)
skills/answer-partner/SKILL.md   回答方 agent 指令(收件箱 → agent → 沉淀)
cli/askme.py                     单文件 CLI,纯标准库,Python 3.8+
cli/askme_gh.py                  GitHub 后端(Contents API、乐观锁、客户端状态机)
server/server.py                 可选的单文件服务端(标准库 + SQLite,约 800 行)
server/deploy.sh                 一键部署(systemd + 健康检查)
scripts/publish.py               构建自升级 zip(可选分发通道)
```

接入你的 agent(ZCode / Claude Code 等):在 agent 的 skills 目录建一个文件夹,放三个文件——`SKILL.md`(按你的角色从 [skills/](skills/) 选)+ [cli/](cli/) 的 `askme.py` 和 `askme_gh.py`。

## 与同类项目的对比

| | askmate | [Agent Inbox](https://github.com/langchain-ai/agent-inbox) | digital-twin 类项目 | [A2A](https://github.com/a2aproject/a2a) |
|---|---|---|---|---|
| 方向 | 我的 agent → **你的**分身 →(偶尔)你 | 我的 agent → 我 | 人喂数据 → twin 作答 | agent ↔ agent 协议 |
| 知识生长 | 每条真人回复自动沉淀 | 无(审批 UX) | 前期集中喂语料 | 无(传输层) |
| 基础设施 | 一个 git 仓库,或一个标准库单文件 | LangGraph 技术栈 | 不定 | 协议 + 运行时 |
| 冷启动 | 第一天全靠真人,知识随使用生长 | — | 前期重投入 | — |

## 诚实的局限

- GitHub 后端的状态机在客户端执行——双方本是同一私有仓库的协作者、git 历史即审计日志,所以这是**协作模型**而非对抗性安全
- 检索有意采用关键词/前缀匹配(grep 风格,无 embedding)——在这个量级上,agent 多轮换词的 agentic search 比向量召回好用,但别指望万条目级语义模糊
- 界面是终端 + CLI,web 在路线图里
- CLI/服务端的用户可见文案目前是中文(首个部署面向中文用户);SKILL.md 文档与 README 是英文。i18n 在路线图最顶部——欢迎 PR

## 路线图

- [ ] 界面文案英文化(i18n)
- [ ] 把 ask/inbox/reply 做成 **MCP server**,任何 MCP 客户端免装 skill 直接用
- [ ] ask/reply 原语的 A2A 传输
- [ ] 极简 web 收件箱(先只读)

## 协议

[MIT](LICENSE)
