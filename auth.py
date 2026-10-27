"""Gmail OAuth 认证。

只申请 gmail.modify：可以读邮件、改标签，但不能发信、不能永久删除
（对应 case proposal 里"限制写 API"的隐私设计）。

首次运行需要浏览器授权，生成 token.json；之后自动用 refresh_token 续期。
在无桌面的机器（OrangePi）上部署时，先把本地生成的 token.json 拷过去。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

import config
from utils import is_retryable_http_error, retry

logger = logging.getLogger("aide.auth")


class AuthError(RuntimeError):
    """认证失败，且无法自动恢复（需要人工介入）。"""


def _write_token(creds: Credentials, path: Path) -> None:
    """原子写 token，避免中途崩溃留下半个损坏文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(creds.to_json(), encoding="utf-8")
    os.replace(tmp, path)
    try:  # 非 Windows 上收紧权限；Windows 上 os.chmod 语义有限，忽略失败
        path.chmod(0o600)
    except OSError:
        pass


def _load_token(path: Path) -> Credentials | None:
    if not path.is_file():
        return None
    try:
        return Credentials.from_authorized_user_file(str(path), config.GMAIL_SCOPES)
    except (ValueError, KeyError) as exc:
        logger.warning("Could not parse token.json (%s), re-authorizing", exc)
        return None


@retry(attempts=3, retry_on=is_retryable_http_error, description="Gmail token refresh")
def _refresh(creds: Credentials) -> None:
    creds.refresh(Request())


def get_credentials(interactive: bool | None = None) -> Credentials:
    """返回可用的 Gmail 凭据。

    Args:
        interactive: 允许打开浏览器走 OAuth 流程。后台定时任务应传 False，
            避免卡在一个永远等不到人点的授权页面上。为 None 时读
            AIDE_NONINTERACTIVE 环境变量。
    """
    if interactive is None:
        interactive = not config.NONINTERACTIVE

    creds = _load_token(config.TOKEN_PATH)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.debug("Token expired — attempting refresh with refresh_token")
        try:
            _refresh(creds)
            _write_token(creds, config.TOKEN_PATH)
            return creds
        except Exception as exc:  # noqa: BLE001 —— 续期失败原因很多，统一转成可读提示
            logger.warning("Token refresh failed: %s: %s", type(exc).__name__, exc)
            creds = None
            refresh_error = exc
    else:
        refresh_error = None

    if not interactive:
        hint = ""
        if refresh_error is not None and "invalid_grant" in str(refresh_error).lower():
            hint = (
                " (refresh token is no longer valid: if the OAuth app is still in Testing mode, "
                "tokens expire every 7 days; publish the app to avoid re-authorizing repeatedly)"
            )
        raise AuthError(
            f"No usable Gmail credentials at {config.TOKEN_PATH}.{hint} "
            "Run `python auth.py` on this machine to authorize, then copy token.json to the deployment host."
        )

    cred_path = config.discover_credentials()
    if not cred_path:
        raise AuthError(
            "OAuth client credentials not found. Download client_secret*.json from Google Cloud "
            "Console into the project root, or set AIDE_CREDENTIALS_PATH."
        )

    logger.info("Browser authorization required — opening Google sign-in… (client: %s)", cred_path.name)
    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), config.GMAIL_SCOPES)
    creds = flow.run_local_server(port=0)

    _write_token(creds, config.TOKEN_PATH)
    logger.info("Authorization complete — token saved to %s", config.TOKEN_PATH)
    return creds


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    c = get_credentials()
    print("Authorized")
    print("scopes:", c.scopes)
