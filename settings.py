"""配置读写与校验。

刻意和 Tkinter 解耦：校验规则、.env 读写都在这里，GUI 只负责取值和显示，
这样这些逻辑可以离线跑单元测试。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import config

# 没有 .env 时的兜底值。仓库里不放个人邮箱：真实默认值来自 .env 的 GMAIL_ADDRESS
DEFAULT_EMAIL = ""
ENV_FILE = config.PROJECT_ROOT / ".env"

_FIELD_ENV = {"email": "GMAIL_ADDRESS", "key": "DEEPSEEK_API_KEY"}

# 允许的 local-part 字符集（RFC 5321 的常见子集），域名走单独的重复校验
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def validate_email(raw: str) -> tuple[str | None, str | None]:
    """返回 (规范化邮箱, 错误信息)，两者恰有一个为 None。"""
    value = (raw or "").strip()
    if not value:
        return None, "Email cannot be empty"

    if len(value) > 254:
        return None, "Email is too long (over 254 characters)"
    if " " in value:
        return None, "Email cannot contain spaces"
    if value.count("@") != 1:
        return None, "Email must contain exactly one @"

    local, _, domain = value.partition("@")
    if not local:
        return None, "The part before @ cannot be empty"
    if len(local) > 64:
        return None, "Username before @ is too long (over 64 characters)"
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return None, "Username cannot start/end with a dot or contain two dots in a row"
    if not _EMAIL_RE.match(value):
        return None, "Invalid email format, e.g. name@gmail.com"
    if domain.rsplit(".", 1)[-1].isdigit():
        return None, "Invalid email domain"

    return value.lower(), None


def is_gmail(value: str) -> bool:
    return value.rsplit("@", 1)[-1].lower() in _GMAIL_DOMAINS


def validate_api_key(raw: str) -> tuple[str | None, str | None]:
    """DeepSeek key 的基本形态校验（不发网络请求）。"""
    value = (raw or "").strip()
    if not value:
        return None, "API key cannot be empty"
    if any(ch.isspace() for ch in value):
        return None, "API key cannot contain spaces or line breaks"
    if not value.startswith("sk-"):
        return None, "DeepSeek API keys usually start with sk-"
    if len(value) < 20:
        return None, "API key is too short — check that it was copied completely"
    return value, None


def mask_key(key: str) -> str:
    if not key:
        return "not set"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:5]}{'*' * 8}{key[-4:]}"


# --------------------------------------------------------------------------
# .env 读写
# --------------------------------------------------------------------------
def upsert_env(path: Path, updates: dict[str, str]) -> None:
    """只改动指定键，保留原有顺序、注释和其他配置。"""
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    written: set[str] = set()
    result: list[str] = []

    for line in lines:
        match = key_re.match(line)
        if match and match.group(1) in updates:
            name = match.group(1)
            result.append(f"{name}={updates[name]}")
            written.add(name)
        else:
            result.append(line)

    if result and result[-1].strip():
        result.append("")
    for name, value in updates.items():
        if name not in written:
            result.append(f"{name}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(result).rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_defaults() -> dict:
    """读当前生效的配置，作为界面上的"默认值"。"""
    key = config.DEEPSEEK_API_KEY
    return {
        "email": config.GMAIL_ADDRESS or DEFAULT_EMAIL,
        "key": key,
        "key_masked": mask_key(key),
        "has_key": bool(key),
    }


def save(email: str, api_key: str) -> Path:
    """写入 .env，并同步内存里的配置，让当前进程立刻生效。"""
    upsert_env(ENV_FILE, {_FIELD_ENV["email"]: email, _FIELD_ENV["key"]: api_key})
    config.GMAIL_ADDRESS = email
    config.DEEPSEEK_API_KEY = api_key
    return ENV_FILE


# --------------------------------------------------------------------------
# 账号核对
# --------------------------------------------------------------------------
def _fetch_authorized_account() -> str:
    """查当前 token 所属的 Gmail 账号；没有可用凭据时抛 AuthError。"""
    from googleapiclient.discovery import build

    from auth import get_credentials

    creds = get_credentials(interactive=False)  # 只复用 token.json，不弹授权
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service.users().getProfile(userId="me").execute().get("emailAddress", "") or ""


def check_gmail_account(email: str, profile_getter=None) -> dict:
    """核对「输入的邮箱」与「已完成 OAuth 授权的账号」是否一致。

    注意：这不是在验证该地址本身有没有走过 OAuth——本机只持有一个 token，
    只能证明 token 属于哪个账号。所以结果分四种 state：

        match          已授权，且输入与授权账号一致
        mismatch       已授权，但输入是另一个地址
        not_authorized 本机没有可用凭据（没跑过 auth.py，或 refresh token 过期）
        error          有凭据但查询失败（网络/权限等）

    返回 {"state", "actual", "message"}。
    """
    getter = profile_getter or _fetch_authorized_account

    try:
        actual = getter()
    except Exception as exc:  # noqa: BLE001 —— 校验失败不阻塞保存
        from auth import AuthError

        if isinstance(exc, AuthError):
            return {"state": "not_authorized", "actual": "", "message": str(exc)}
        return {
            "state": "error",
            "actual": "",
            "message": f"{type(exc).__name__}: {exc}",
        }

    if not actual:
        return {"state": "error", "actual": "", "message": "Could not read the authorized account"}

    if actual.lower() == email.lower():
        return {
            "state": "match",
            "actual": actual,
            "message": f"matches the authorized account {actual}",
        }
    return {
        "state": "mismatch",
        "actual": actual,
        "message": f"the authorized account is {actual}",
    }
