# 邮件优先级助手

[English](README.md) | **中文**

把 Gmail 收件箱按 P0–P4 五级自动分类并打标签，是 case proposal 里「五级文件夹 + 邮件代理」
的最小可运行内核。

```
Gmail API ──拉取/增量同步──> SQLite (mail.db) ──DeepSeek 判定──> Gmail 标签 (P0–P4)
                                    │
                                    └── 结构化字段，后续可接向量库 / chatbot / 日程
```

## 五级分类

| 标签 | 含义 | 例子 |
|---|---|---|
| `P0-Action required` | 必须行动，通常有截止时间 | 选课、考试变更、缴费、签证、宿舍、ITSC 告警 |
| `P1-Major & career` | 与专业/职业相关 | CPEG/SENG/COMP 通知、实习、co-op、科研、竞赛、技术讲座 |
| `P2-Admin info` | 学校行政，无需行动 | 图书馆、系统维护、政策提醒、每周汇总 |
| `P3-Events` | 可参加但不影响学业/职业 | 艺术、体育、志愿、社团招新、通识讲座 |
| `P4-Noise` | 推广或敏感噪音 | 信用卡、验证码、银行账单、无关商家 |

一封邮件只挂一个标签，重跑幂等，不会叠加。

## 快速开始

需要 Python 3.11+。

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

copy .env.example .env          # 填入 DEEPSEEK_API_KEY
python auth.py                  # 会打开浏览器，生成 token.json

python run_pipeline.py --limit 10 -v
```

所有路径默认相对项目根目录，可用 `AIDE_DB_PATH`、`AIDE_TOKEN_PATH`、
`AIDE_CREDENTIALS_PATH`、`AIDE_DATA_DIR` 覆盖。

## 初始配置窗口

双击 `setup.bat`（或 `python setup_gui.py`）打开：

![配置窗口](docs/setup_window.png)

- 两栏都留空、直接回车 = 使用 `.env` 里现有的值。
- 邮箱做三层检查：① 格式；② 是否 Gmail 域名（非 Gmail 会二次确认）；③ 用已授权 token
  调 `getProfile` 核对账号——一致给绿字，不一致弹窗，**本机没有 token 时明确提示
  「尚未完成 OAuth 授权」**，不会静默当作已验证。
- API Key 检查 `sk-` 前缀、长度、有无空白；保存时只改 `.env` 里对应的两行，注释和其他
  配置原样保留。

## 常用命令

| 命令 | 作用 |
|---|---|
| `python run_pipeline.py` | 增量同步 + 分类待处理邮件 |
| `python run_pipeline.py --limit 5 --dry-run` | 只分类、不打标签、不写库（调 rubric 用） |
| `python run_pipeline.py --full` | 忽略 historyId，重扫 inbox |
| `python run_pipeline.py --reset` | 清空分类结果后重新判定 |
| `python run_pipeline.py --non-interactive` | cron / 无桌面环境（禁止弹浏览器） |
| `python -m unittest discover -s tests -v` | 离线测试（不联网、不碰真账号） |

## 实现要点

- **拉取**：遍历 MIME，优先 `text/plain`，HTML 转纯文本（丢弃 script/style、展开链接），
  跳过附件，按 `charset → utf-8 → gb18030` 兜底解码；`batchGet` 把 N 次请求压成 1 次；
  首次之后用 `historyId` 增量同步。
- **存储**：单表 `emails` + `sync_state`，启动时自动 `ALTER TABLE` 迁移；写入用 `UPSERT`，
  重抓只刷新元数据、不覆盖已有分类；失败累加 `error_count`，三次后不再重试。
- **分类**：DeepSeek Responses API + strict tool schema；`tool_choice="required"` 被拒时
  自动降级到 `auto`；模型只输出文字时从文本里兜底解析；认证错误直接中止整批。
- **打标签**：一次 `modify` 同时加目标标签、摘掉其余四个，保证五级互斥。

## 模块

| 文件 | 职责 |
|---|---|
| `config.py` | 路径、环境变量、常量 |
| `utils.py` | 日志、指数退避重试、文本清洗 |
| `auth.py` | OAuth（`gmail.modify`：可读可打标签，不能发信/永久删除） |
| `gmail_reader.py` | MIME 解析、HTML→文本、批量拉取、增量同步 |
| `store.py` | SQLite 迁移、UPSERT、失败计数、同步状态 |
| `classify.py` | DeepSeek 调用、工具调用解析、rubric |
| `label.py` | P0–P4 标签，互斥打标签 |
| `settings.py` | 邮箱/Key 校验、`.env` 原地更新、账号核对 |
| `setup_gui.py` | Tkinter 初始配置窗口 |
| `run_pipeline.py` | 命令行入口 |

## 测试

31 个离线测试：MIME 解析、HTML 提取、数据库迁移、UPSERT 语义、标签互斥、模型响应解析与
降级，以及一次用假 Gmail 服务 + 假模型 client 跑通 `main()`。

```bash
python -m unittest discover -s tests -v
```

## 隐私

- 只申请 `gmail.modify`：读邮件、改标签，不能发信、不能永久删除。
- `.env`、`token.json`、`client_secret*.json`、`mail.db` 均已 git-ignore；除发给模型 API 的
  内容外，数据不出本机。
- 模型每次只看到发件人、主题、时间和最多 6000 字正文。

## 后续

proposal 里还有更大的一套系统，本仓库只覆盖「收件箱分级」这一层。尚未实现：chatbot 前端、
向量检索、日程抽取、午间报告、私有邮箱中转。

## 许可

[MIT](LICENSE)
