"""主流程：同步邮件 → 分类 → 打标签 → 汇总。

用法示例：
    python run_pipeline.py                  # 增量同步 + 分类待处理邮件
    python run_pipeline.py --limit 5 -v     # 只处理 5 封，输出调试日志
    python run_pipeline.py --full           # 忽略 historyId，重扫 inbox
    python run_pipeline.py --dry-run        # 只分类不打标签、不写库（调 rubric 用）
    python run_pipeline.py --reset          # 清空分类结果后重新判定
"""

from __future__ import annotations

import argparse
import logging
import sys

import config
import store
from auth import AuthError
from classify import ConfigError, classify_and_label, classify_email
from gmail_reader import GmailReader, HistoryExpired
from label import GmailLabels
from utils import setup_logging

logger = logging.getLogger("aide.pipeline")

HISTORY_KEY = "gmail_history_id"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gmail priority classification assistant")
    parser.add_argument("--limit", type=int, default=20, help="max messages to fetch/process (default 20)")
    parser.add_argument("--query", default="in:inbox", help='Gmail search query (default "in:inbox")')
    parser.add_argument("--full", action="store_true", help="ignore historyId and rescan the inbox")
    parser.add_argument("--dry-run", action="store_true", help="classify only: no labels, no DB writes")
    parser.add_argument("--reset", action="store_true", help="clear existing classifications and re-run")
    parser.add_argument("--max-attempts", type=int, default=3, help="max attempts per message (default 3)")
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="never open a browser for OAuth (required for cron / headless hosts)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    return parser.parse_args(argv)


def sync_emails(conn, reader: GmailReader, args) -> list[dict]:
    """把新邮件落库，返回本次新入库的邮件。"""
    since = None if args.full else store.get_state(conn, HISTORY_KEY)

    if since:
        try:
            emails, new_history_id = reader.fetch_incremental(since)
        except HistoryExpired:
            logger.warning("historyId expired — falling back to a full inbox scan")
            emails = reader.fetch_recent(count=args.limit, query=args.query)
            new_history_id = str(reader.get_profile().get("historyId", "") or "")
    else:
        emails = reader.fetch_recent(count=args.limit, query=args.query)
        new_history_id = str(reader.get_profile().get("historyId", "") or "")

    if not args.dry_run:
        changed = store.save_emails(conn, emails)
        logger.info("Stored %d message(s) (%d row(s) inserted/updated)", len(emails), changed)
        if new_history_id:
            store.set_state(conn, HISTORY_KEY, new_history_id)

    return emails


def relabel_pending(conn, labels: GmailLabels, args) -> int:
    """补打上次失败的标签，不调用模型。"""
    pending = store.get_unlabeled(conn, limit=args.limit, max_attempts=args.max_attempts)
    if not pending:
        return 0

    logger.info("%d classified message(s) are missing labels — fixing those first", len(pending))
    fixed = 0
    for row in pending:
        if labels.apply_priority(row["id"], row["priority"]):
            if not args.dry_run:
                store.update_classification(conn, row["id"], row["priority"], None, True)
            fixed += 1
        else:
            store.mark_failed(conn, row["id"], "labeling retry failed")
    return fixed


def classify_pending(conn, labels: GmailLabels, args, emails: list[dict] | None = None) -> tuple[int, int]:
    """分类待处理邮件，返回 (成功数, 失败数)。

    emails 不为 None 时直接处理这批（dry-run 用，不去读库）。
    """
    todo = emails if emails is not None else store.get_unprocessed(
        conn, limit=args.limit, max_attempts=args.max_attempts
    )
    logger.info("%d message(s) pending classification", len(todo))

    ok = failed = 0
    for index, email in enumerate(todo, start=1):
        subject = (email.get("subject") or "(no subject)")[:48]
        logger.info("(%d/%d) %s", index, len(todo), subject)

        try:
            if args.dry_run:
                result = classify_email(email)
            else:
                result = classify_and_label(email, labels, apply_fn=labels.apply_priority)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单封异常不能中断整批
            logger.error("Error while processing %s: %s: %s", email["id"], type(exc).__name__, exc)
            if not args.dry_run:
                store.mark_failed(conn, email["id"], f"{type(exc).__name__}: {exc}")
            failed += 1
            continue

        if args.dry_run:
            logger.info("  → %s | %s", result.get("priority") or "unclassified", result.get("reason") or "")
            ok += 1 if result.get("priority") else 0
            continue

        if result.get("priority"):
            store.update_classification(
                conn, email["id"], result["priority"], result.get("reason"), result.get("labeled", False)
            )
            ok += 1
        else:
            store.mark_failed(conn, email["id"], result.get("error") or "unclassified")
            failed += 1

    return ok, failed


def print_summary(conn, args) -> None:
    if args.dry_run:
        logger.info("dry-run: nothing was written to the database")
        return

    summary = store.stats(conn)
    by_priority = summary["by_priority"]
    detail = "  ".join(f"{p}:{by_priority.get(p, 0)}" for p in config.PRIORITIES)
    logger.info(
        "stored %d | classified %d | pending %d | failed %d | %s",
        summary["total"],
        sum(by_priority.values()),
        summary["pending"],
        summary["failed"],
        detail,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    conn = store.init_db()
    try:
        key_tail = config.DEEPSEEK_API_KEY[-4:] if config.DEEPSEEK_API_KEY else "not set"
        logger.info(
            "model %s @ %s (key ****%s)", config.DEEPSEEK_MODEL, config.DEEPSEEK_BASE_URL, key_tail
        )

        if args.reset and not args.dry_run:
            count = store.reset_classifications(conn)
            logger.info("Cleared classifications for %d message(s)", count)

        reader = GmailReader(interactive=not args.non_interactive)
        emails = sync_emails(conn, reader, args)

        if args.dry_run:
            logger.info("dry-run: synced %d message(s) (nothing written)", len(emails))
            classify_pending(conn, labels=None, args=args, emails=emails)
            return 0

        labels = GmailLabels(interactive=not args.non_interactive)
        labels.ensure_priority_labels()

        relabeled = relabel_pending(conn, labels, args)
        if relabeled:
            logger.info("Re-applied labels to %d message(s)", relabeled)

        ok, failed = classify_pending(conn, labels, args)
        logger.info("This run: %d classified, %d failed", ok, failed)

        print_summary(conn, args)
        return 0 if ok or not failed else 2

    except ConfigError as exc:
        logger.error("%s", exc)
        return 1
    except AuthError as exc:
        logger.error("Gmail authentication failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
