"""Gmail 读取层。

相比最初的版本，这里做了四件事：
1. HTML 邮件转纯文本（原文直接喂给模型，前 3000 字全是 <style> 和表格标签）；
2. 跳过附件正文，避免把 PDF/图片的 base64 当正文塞进数据库；
3. 批量拉取（batchGet），15 封邮件从 15 次请求变成 1 次；
4. 支持基于 historyId 的增量同步，不再每次重扫整个 inbox。
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
from auth import get_credentials
from utils import clean_text, is_retryable_http_error, retry

logger = logging.getLogger("aide.gmail")


class HistoryExpired(Exception):
    """historyId 过期：Gmail 只保留有限窗口的变更记录，只能退化为全量扫描。"""


# --------------------------------------------------------------------------
# HTML → 纯文本
# --------------------------------------------------------------------------
class _HTMLTextExtractor(HTMLParser):
    """比正则去标签靠谱：会跳过 script/style，保留段落与列表结构，把链接展开成正文。"""

    _SKIP = {"script", "style", "head", "title", "noscript", "template", "svg"}
    _BLOCK = {
        "p", "div", "br", "tr", "td", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "section", "article", "header", "footer", "blockquote", "hr",
        "ul", "ol", "pre", "form",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._links: list[str | None] = []

    # -- 标签 -------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        if self._skip_depth:
            return

        attr = dict(attrs)
        if tag in self._BLOCK:
            self._parts.append("\n")
        elif tag == "li":
            self._parts.append("\n- ")
        elif tag == "img":
            alt = (attr.get("alt") or "").strip()
            if alt:
                self._parts.append(f"[image: {alt}]")
        elif tag == "a":
            href = (attr.get("href") or "").strip()
            self._links.append(href if href.lower().startswith(("http://", "https://")) else None)

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return

        if tag == "a" and self._links:
            href = self._links.pop()
            if href:
                self._parts.append(f" <{href}>")
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    """HTML → 纯文本；解析失败时退化为粗暴去标签。"""
    if not html:
        return ""
    if "<" not in html:  # 纯文本伪装成 text/html 的情况很常见
        return html

    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
        parser.close()
        text = parser.get_text()
    except Exception as exc:  # noqa: BLE001 —— HTMLParser 对畸形邮件偶尔会抛
        logger.debug("HTML parse failed, falling back to tag stripping: %s", exc)
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)

    return clean_text(text)


# --------------------------------------------------------------------------
# 正文解码
# --------------------------------------------------------------------------
def _decode_b64(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _part_charset(part: dict) -> str | None:
    for header in part.get("headers", []) or []:
        if header.get("name", "").lower() == "content-type":
            match = re.search(r'charset="?([\w\-]+)', header.get("value", ""), re.I)
            if match:
                return match.group(1)
    return None


def _decode_bytes(raw: bytes, charset: str | None) -> str:
    """按声明 charset → utf-8 → gb18030 的顺序解码，中文邮件才不会变乱码。"""
    for encoding in filter(None, (charset, "utf-8", "gb18030")):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _walk(part: dict, plain: list[str], html: list[str], attachments: list[str]) -> None:
    mime = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""

    if filename:  # 附件：只记名字，不解析内容
        attachments.append(filename)
        return

    if mime.startswith("multipart/"):
        for sub in part.get("parts", []) or []:
            _walk(sub, plain, html, attachments)
        return

    data = part.get("body", {}).get("data")
    if not data:
        return

    try:
        raw = _decode_b64(data)
    except (ValueError, TypeError) as exc:  # base64 损坏的邮件不算罕见
        logger.debug("Body base64 decoding failed: %s", exc)
        return

    if mime == "text/plain":
        plain.append(_decode_bytes(raw, _part_charset(part)))
    elif mime == "text/html":
        html.append(_decode_bytes(raw, _part_charset(part)))


def _strip_quoted(text: str) -> str:
    """砍掉 "On ... wrote:" 之前的历史引用，只保留本次回复内容。"""
    if len(text) < 800:
        return text
    patterns = (
        r"\n-{2,}\s*Original Message\s*-{2,}",
        r"\nOn .{0,200}wrote:\s*\n",
        r"\n发件人[:：].{0,200}\n发送时间",
        r"\n在.{0,80}写道[:：]",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match and match.start() > 200:  # 别把整封短邮件都砍没了
            return text[: match.start()]
    return text


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------
class GmailReader:
    def __init__(self, service=None, interactive: bool = True):
        if service is None:
            creds = get_credentials(interactive=interactive)
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.service = service

    # -- 底层 -------------------------------------------------------------
    @retry(attempts=3, retry_on=is_retryable_http_error, description="Gmail list")
    def list_messages(self, query: str = "", max_results: int = 20, page_token: str | None = None) -> dict:
        params: dict = {"userId": config.GMAIL_USER, "maxResults": min(max_results, config.GMAIL_PAGE_SIZE)}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        return self.service.users().messages().list(**params).execute()

    @retry(attempts=3, retry_on=is_retryable_http_error, description="Gmail get")
    def get_message(self, msg_id: str, fmt: str = "full") -> dict:
        return self.service.users().messages().get(
            userId=config.GMAIL_USER, id=msg_id, format=fmt
        ).execute()

    def get_profile(self) -> dict:
        return self.service.users().getProfile(userId=config.GMAIL_USER).execute()

    # -- 解析 -------------------------------------------------------------
    @staticmethod
    def _get_headers(payload: dict) -> dict[str, str]:
        return {h["name"].lower(): h["value"] for h in payload.get("headers", []) or []}

    @staticmethod
    def _extract_email(header_value: str) -> str:
        if not header_value:
            return ""
        match = re.search(r"<([^>]+)>", header_value)
        return (match.group(1) if match else header_value).strip().lower()

    @staticmethod
    def _parse_date(raw_date: str | None, internal_ms: str | None) -> str | None:
        if internal_ms:
            try:
                return datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc).isoformat()
            except (ValueError, TypeError, OSError):
                pass
        if not raw_date:
            return None
        try:
            return parsedate_to_datetime(raw_date).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            return raw_date

    @classmethod
    def parse_message(cls, msg: dict, include_body: bool = True) -> dict:
        payload = msg.get("payload", {}) or {}
        headers = cls._get_headers(payload)
        label_ids = msg.get("labelIds", []) or []

        plain: list[str] = []
        html: list[str] = []
        attachments: list[str] = []
        _walk(payload, plain, html, attachments)

        body = clean_text("\n".join(plain))
        if len(body) < 30 and html:  # 有 HTML 版时优先用它（纯文本常常只有一句 "view online"）
            body = html_to_text("\n".join(html))
        elif html:
            body = clean_text(body)

        result = {
            "id": msg.get("id"),
            "thread_id": msg.get("threadId"),
            "label_ids": label_ids,
            "snippet": msg.get("snippet", ""),
            "subject": clean_text(headers.get("subject", "")),
            "from_raw": headers.get("from", ""),
            "from_email": cls._extract_email(headers.get("from", "")),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "date": cls._parse_date(headers.get("date"), msg.get("internalDate")),
            "message_id": headers.get("message-id", ""),
            "is_unread": "UNREAD" in label_ids,
            "is_inbox": "INBOX" in label_ids,
            "is_important": "IMPORTANT" in label_ids,
            "has_attachment": bool(attachments),
            "attachment_names": attachments,
            "size": int(msg.get("sizeEstimate", 0) or 0),
            "web_link": f"https://mail.google.com/mail/u/0/#inbox/{msg.get('id')}",
        }
        if include_body:
            result["body"] = _strip_quoted(body)[: config.BODY_STORE_LIMIT]
        return result

    # -- 批量 ------------------------------------------------------------
    def batch_get(self, msg_ids: list[str]) -> dict[str, dict]:
        """一次 HTTP 批量请求取回多封邮件；失败的那几封再单独补拉。"""
        if not msg_ids:
            return {}

        results: dict[str, dict] = {}
        callback_errors: list[str] = []

        def _callback(request_id, response, exception):
            if exception is not None:
                callback_errors.append(request_id)
                logger.debug("Batch fetch failed for %s: %s", request_id, exception)
                return
            results[request_id] = response

        try:
            batch = self.service.new_batch_http_request(callback=_callback)
            for msg_id in msg_ids:
                batch.add(
                    self.service.users().messages().get(
                        userId=config.GMAIL_USER, id=msg_id, format="full"
                    ),
                    request_id=msg_id,
                )
            batch.execute()
        except Exception as exc:  # noqa: BLE001 —— 批量通道整体不可用时退化即可
            logger.warning("Batch fetch unavailable (%s), falling back to one-by-one", exc)
            results.clear()

        for msg_id in msg_ids:
            if msg_id in results:
                continue
            try:
                results[msg_id] = self.get_message(msg_id)
            except Exception as exc:  # noqa: BLE001 —— 单封失败不应拖垮整批
                logger.error("Could not fetch message %s: %s", msg_id, exc)
        return results

    # -- 对外 ------------------------------------------------------------
    def fetch_recent(self, count: int = 20, query: str = "in:inbox") -> list[dict]:
        """按条件拉最近 count 封（自动翻页，最多 100/页）。"""
        ids: list[str] = []
        page_token: str | None = None

        while len(ids) < count:
            resp = self.list_messages(query=query, max_results=min(config.GMAIL_PAGE_SIZE, count - len(ids)), page_token=page_token)
            ids.extend(m["id"] for m in resp.get("messages", []) or [])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        raw = self.batch_get(ids[:count])
        return [self.parse_message(raw[i]) for i in ids[:count] if i in raw]

    def list_history(self, start_history_id: str) -> tuple[list[str], str | None]:
        """返回 (新增邮件 id 列表, 最新 historyId)。"""
        ids: list[str] = []
        page_token: str | None = None
        latest: str | None = None

        while True:
            params = {
                "userId": config.GMAIL_USER,
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
                "labelId": "INBOX",
                "maxResults": 500,
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                resp = self.service.users().history().list(**params).execute()
            except HttpError as exc:
                if getattr(exc, "status_code", None) == 404:
                    raise HistoryExpired(f"historyId {start_history_id} has expired") from exc
                raise

            for record in resp.get("history", []) or []:
                for added in record.get("messagesAdded", []) or []:
                    msg_id = added.get("message", {}).get("id")
                    if msg_id and msg_id not in ids:
                        ids.append(msg_id)

            latest = resp.get("historyId") or latest
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return ids, latest

    def fetch_incremental(self, start_history_id: str) -> tuple[list[dict], str]:
        """增量同步：只取上次之后新进 inbox 的邮件。"""
        ids, latest = self.list_history(start_history_id)
        logger.info("Incremental sync: last historyId=%s, %d new message(s)", start_history_id, len(ids))

        # 即便没有新邮件，也要把 historyId 推到最新，否则下次还得扫同一段
        profile = self.get_profile()
        new_history_id = latest or profile.get("historyId") or start_history_id

        if not ids:
            return [], new_history_id

        raw = self.batch_get(ids)
        return [self.parse_message(raw[i]) for i in ids if i in raw], new_history_id


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    reader = GmailReader()
    for mail in reader.fetch_recent(5):
        stamp = (mail["date"] or "")[:19]
        print(f"[{stamp}] {mail['from_email']} | {mail['subject']}")
        print(f"   body {len(mail['body'])} chars | attachments {mail['attachment_names']}")
