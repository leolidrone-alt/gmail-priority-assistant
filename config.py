"""集中配置：路径、环境变量与常量。

其他模块只从这里取配置，避免到处硬编码绝对路径（之前 D:\\大学\\助手 被写死在
auth.py / store.py / classify.py 三处，换机器或部署到 OrangePi 就全线失效）。

所有路径都可以用环境变量覆盖，默认值相对于本文件所在目录。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

# .env 是唯一配置来源：必须 override=True。
# 否则系统里残留的旧 DEEPSEEK_API_KEY 环境变量会静默盖掉 .env，报 401 却查不出原因
# （Windows 用户级环境变量尤其容易踩，重装/换 key 后旧值还在）。
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _env_path(name: str, default: Path) -> Path:
    """读取路径类环境变量，空字符串视为未设置。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return Path(raw).expanduser().resolve()


DATA_DIR = _env_path("AIDE_DATA_DIR", PROJECT_ROOT)

DB_PATH = _env_path("AIDE_DB_PATH", DATA_DIR / "mail.db")
TOKEN_PATH = _env_path("AIDE_TOKEN_PATH", DATA_DIR / "token.json")


def discover_credentials() -> Path | None:
    """定位 OAuth 客户端凭据文件，找不到返回 None。"""
    explicit = os.getenv("AIDE_CREDENTIALS_PATH", "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        return path if path.is_file() else None

    for pattern in ("client_secret*.json", "credentials.json"):
        matches = sorted(PROJECT_ROOT.glob(pattern))
        if matches:
            return matches[0]
    return None


# --- Gmail ---------------------------------------------------------------
# gmail.modify 允许读邮件 + 改标签，但仍不能发信 / 永久删除，符合 proposal 的写保护要求
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
GMAIL_USER = "me"

# --- 账号 ----------------------------------------------------------------
# 仅用于展示与配置记录；Gmail API 仍以 "me" 调用，不依赖这个值
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()

# --- 模型 ----------------------------------------------------------------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
# 关闭思考模式才能配合 tool_choice="required"；可用 none / low / medium / high 覆盖
DEEPSEEK_REASONING_EFFORT = os.getenv("AIDE_REASONING_EFFORT", "none").strip()
MODEL_TIMEOUT_SECONDS = float(os.getenv("AIDE_MODEL_TIMEOUT", "90"))

# --- 业务常量 -------------------------------------------------------------
PRIORITIES = ("P0", "P1", "P2", "P3", "P4")

# 存库与送模型的正文上限：库里留长一些，喂模型时再截断
BODY_STORE_LIMIT = 50_000
BODY_PROMPT_LIMIT = 6_000

# Gmail 单次 messages.list 上限，超过则翻页
GMAIL_PAGE_SIZE = 100

LOG_LEVEL = os.getenv("AIDE_LOG_LEVEL", "INFO").upper()

# 无桌面环境（OrangePi / cron）里禁止弹出浏览器授权页，否则进程会一直挂着
NONINTERACTIVE = os.getenv("AIDE_NONINTERACTIVE", "").strip().lower() in {"1", "true", "yes", "on"}
