"""Gmail 标签层。

核心修复：五个优先级标签互斥。原来的 apply_label 只 addLabelIds，重跑一次
一封邮件就同时挂在 P1 和 P4 上，"五级文件夹"就名存实亡了。
现在一次 modify 调用同时 add 目标标签、remove 其余四个，天然幂等。
"""

from __future__ import annotations

import logging

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
from auth import get_credentials
from utils import is_retryable_http_error, retry

logger = logging.getLogger("aide.label")

PRIORITY_LABELS = {
    "P0": "P0-Action required",
    "P1": "P1-Major & career",
    "P2": "P2-Admin info",
    "P3": "P3-Events",
    "P4": "P4-Noise",
}

# 旧版中文标签名：首次运行时自动改名（保留 label id，已打标签的邮件不受影响）
LEGACY_LABELS = {
    "P0": "P0-必须行动",
    "P1": "P1-专业相关",
    "P2": "P2-行政通知",
    "P3": "P3-文康活动",
    "P4": "P4-噪音",
}

# Gmail 只接受官方调色板里的颜色，写错会 400；创建失败时自动降级为无颜色
PRIORITY_COLORS = {
    "P0": ("#cc3a21", "#ffffff"),  # 红
    "P1": ("#eaa041", "#ffffff"),  # 橙
    "P2": ("#f2c960", "#000000"),  # 黄
    "P3": ("#a4c2f4", "#000000"),  # 蓝
    "P4": ("#cccccc", "#000000"),  # 灰
}


class GmailLabels:
    def __init__(self, service=None, interactive: bool = True):
        if service is None:
            creds = get_credentials(interactive=interactive)
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.service = service
        self._label_ids: dict[str, str] = {}

    # -- 基础 -------------------------------------------------------------
    @retry(attempts=3, retry_on=is_retryable_http_error, description="List Gmail labels")
    def list_labels(self) -> dict[str, str]:
        resp = self.service.users().labels().list(userId=config.GMAIL_USER).execute()
        return {label["name"]: label["id"] for label in resp.get("labels", []) or []}

    def create_label(self, name: str, priority: str | None = None) -> str:
        body: dict = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        color = PRIORITY_COLORS.get(priority or "")
        if color:
            body["color"] = {"backgroundColor": color[0], "textColor": color[1]}

        try:
            label = self.service.users().labels().create(userId=config.GMAIL_USER, body=body).execute()
        except HttpError as exc:
            if not color or getattr(exc, "status_code", None) != 400:
                raise
            logger.debug("Label color rejected, creating without color: %s", name)
            body.pop("color", None)
            label = self.service.users().labels().create(userId=config.GMAIL_USER, body=body).execute()

        logger.info("Created label: %s", name)
        return label["id"]

    def ensure_priority_labels(self) -> dict[str, str]:
        """确保 P0–P4 五个标签存在，返回 {P0: label_id, ...}。"""
        existing = self.list_labels()

        # 旧的中文标签就地改名，而不是新建一套：id 不变，邮件上的标签继续有效
        for priority, old_name in LEGACY_LABELS.items():
            new_name = PRIORITY_LABELS[priority]
            if old_name in existing and new_name not in existing:
                label_id = existing.pop(old_name)
                try:
                    self.service.users().labels().update(
                        userId=config.GMAIL_USER,
                        id=label_id,
                        body={
                            "name": new_name,
                            "labelListVisibility": "labelShow",
                            "messageListVisibility": "show",
                        },
                    ).execute()
                    existing[new_name] = label_id
                    logger.info("Renamed label '%s' -> '%s'", old_name, new_name)
                except HttpError as exc:
                    logger.warning("Could not rename label '%s': %s", old_name, exc)

        for priority, name in PRIORITY_LABELS.items():
            if name in existing:
                self._label_ids[priority] = existing[name]
            else:
                self._label_ids[priority] = self.create_label(name, priority)
        return dict(self._label_ids)

    # -- 打标签 -----------------------------------------------------------
    def label_ids(self) -> dict[str, str]:
        if not self._label_ids:
            self.ensure_priority_labels()
        return self._label_ids

    def apply_priority(self, message_id: str, priority: str) -> bool:
        """把邮件标成指定优先级，并摘掉其他四个优先级标签（幂等）。"""
        if priority not in PRIORITY_LABELS:
            raise ValueError(f"Unknown priority: {priority}")

        ids = self.label_ids()
        target = ids.get(priority)
        if not target:
            logger.warning("Label %s does not exist, skipping %s", priority, message_id)
            return False

        others = [label_id for key, label_id in ids.items() if key != priority and label_id]
        try:
            self.service.users().messages().modify(
                userId=config.GMAIL_USER,
                id=message_id,
                body={"addLabelIds": [target], "removeLabelIds": others},
            ).execute()
            return True
        except HttpError as exc:  # 单封失败不该中断整轮
            logger.error("Failed to label %s as %s: %s", message_id, priority, exc)
            return False

    def apply_priority_batch(self, message_ids: list[str], priority: str) -> bool:
        """批量版（用于重打历史邮件），一次请求处理多封。"""
        target = self.label_ids().get(priority)
        if not target or not message_ids:
            return False
        others = [lid for key, lid in self.label_ids().items() if key != priority and lid]
        try:
            self.service.users().messages().batchModify(
                userId=config.GMAIL_USER,
                body={"ids": message_ids, "addLabelIds": [target], "removeLabelIds": others},
            ).execute()
            return True
        except HttpError as exc:
            logger.error("Batch labeling failed: %s", exc)
            return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    labels = GmailLabels().ensure_priority_labels()
    print("Priority labels:", labels)
