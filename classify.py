"""DeepSeek 分类层。

改进点：
- client 懒加载：没有 API key 时不再在 import 阶段就崩（原来 `python run_pipeline.py`
  连报错都来不及打印就炸在 OpenAI() 构造里）；
- 网络/限流错误指数退避重试；
- `tool_choice="required"` + `reasoning.effort` 这组参数是服务端相关的，某些
  DeepSeek 兼容端点不接受，自动降级到 `auto` 再降级到不带 reasoning 的调用；
- 模型没走工具调用时，从纯文本里兜底解析出 P0–P4，而不是直接丢进"未分类"。
"""

from __future__ import annotations

import json
import logging
import re

from openai import OpenAI

import config
from utils import clean_text, is_retryable_api_error, retry, truncate_middle

logger = logging.getLogger("aide.classify")


class ConfigError(RuntimeError):
    """缺少模型配置（通常是没配 DEEPSEEK_API_KEY）。"""


_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        if not config.DEEPSEEK_API_KEY:
            raise ConfigError(
                "DEEPSEEK_API_KEY is missing. Create .env in the project root "
                "(see .env.example) with DEEPSEEK_API_KEY=sk-... and try again."
            )
        _client = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            timeout=config.MODEL_TIMEOUT_SECONDS,
            max_retries=0,  # 重试交给下面的 retry 装饰器，避免放大成 3×3 次
        )
    return _client


def set_client(client: OpenAI | None) -> None:
    """注入/重置 client（测试用）。"""
    global _client
    _client = client


RUBRIC = """
You are an email triage assistant for a first-year CPEG undergraduate at HKUST.
First work out what each email actually wants from the student (is action required? is there a
deadline?), then classify it.

Who you are classifying for:
- First-year undergraduate in Computer Engineering (CPEG) at HKUST
- Cares about: course enrolment, exams, credits, graduation requirements, CPEG/SENG/Engineering
  courses, internships, job openings, research, competitions, IT security, tuition, visa, housing
- Does not care about: recreational activities, student society recruitment, general-education
  talks, marketing, credit cards, verification codes, bills

Categories:
P0 Action required: add/drop, exam schedules or changes, tuition, visa, housing application, ITSC
   security alerts, course time/venue changes, instructor warnings about failing or attendance,
   anything with a firm deadline only the student can act on
P1 Major/career related: notices from CPEG, SENG, Engineering, ELEC, COMP or the Career Center;
   internships, co-op, graduate schemes, job openings, research opportunities, competitions,
   AI/robotics/programming workshops, hackathons, and career talks or company info sessions
   relevant to engineering or technology — P1 even when attendance is optional
P2 Administrative, no action: library notices, IT or system maintenance, policy reminders that
   need nothing from the student, weekly activity digests and bulk event newsletters
P3 Events and societies: arts, dance, photography, sports, volunteering, student society
   recruitment, general-education or non-technical talks — enjoyable, but not advancing major
   or career
P4 Noise/sensitive: marketing, credit cards, OTP or verification codes, password resets,
   bank statements, anything unrelated to HKUST

Rules:
- OTP, verification codes, password resets, bank statements -> P4.
- Official domain (@ust.hk, @connect.ust.hk) with a deadline or a required action -> at least P1,
  and P0 when the student is blocked without acting.
- A career or technology event run by SENG, Engineering or the Career Center is P1, not P3,
  even if it is optional and even if it is only a talk.
- Exactly one priority per email; pick the one with the biggest impact.
- Write the reason as one short English sentence, at most 25 words.

You must call the apply_priority_label tool to submit the result. Do not answer with plain text.
"""

TOOLS = [{
    "type": "function",
    "name": "apply_priority_label",
    "description": "Submit the priority classification for one Gmail email. Must be called.",
    "parameters": {
        "type": "object",
        "properties": {
            "priority": {
                "type": "string",
                "enum": list(config.PRIORITIES),
                "description": "Priority category",
            },
            "reason": {
                "type": "string",
                "description": "One short English sentence explaining the choice, max 25 words",
            },
        },
        "required": ["priority", "reason"],
        "additionalProperties": False,
    },
    "strict": True,
}]

# (tool_choice, 是否带 reasoning 参数) 的降级顺序
_CALL_VARIANTS = (("required", True), ("auto", True), ("required", False), ("auto", False))


def _build_input(email: dict) -> str:
    body = clean_text(email.get("body") or email.get("snippet") or "")
    body = truncate_middle(body, config.BODY_PROMPT_LIMIT)

    flags = []
    if email.get("is_unread"):
        flags.append("unread")
    if email.get("has_attachment"):
        flags.append("has attachment")
    stamp = (email.get("date") or "")[:19].replace("T", " ")

    return f"""From: {email.get('from_raw') or email.get('from_email', '')}
Subject: {email.get('subject', '(no subject)')}
Date: {stamp or 'unknown'}
Status: {', '.join(flags) or 'read'}

Body:
{body or '(no body)'}
"""


def _extract_call(response) -> tuple[str, str] | None:
    """从 responses API 的 output 里取出工具调用参数。"""
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        if getattr(item, "name", None) != "apply_priority_label":
            continue
        try:
            args = json.loads(item.arguments or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Tool arguments are not valid JSON: %s (%s)", exc, item.arguments[:200])
            continue
        priority = str(args.get("priority", "")).upper()
        if priority in config.PRIORITIES:
            return priority, str(args.get("reason", "")).strip()
    return None


def _parse_free_text(text: str) -> tuple[str, str] | None:
    """工具调用缺失时的兜底：从普通输出里找优先级。"""
    if not text:
        return None
    match = re.search(r"\bP([0-4])\b", text)
    if not match:
        return None
    return f"P{match.group(1)}", text.strip()[:80]


@retry(attempts=3, retry_on=is_retryable_api_error, description="DeepSeek classification")
def _call_model(user_input: str):
    client = get_client()
    last_error: Exception | None = None

    for tool_choice, with_reasoning in _CALL_VARIANTS:
        payload: dict = {
            "model": config.DEEPSEEK_MODEL,
            "instructions": RUBRIC,
            "input": user_input,
            "tools": TOOLS,
            "tool_choice": tool_choice,
        }
        if with_reasoning:
            payload["reasoning"] = {"effort": config.DEEPSEEK_REASONING_EFFORT}

        try:
            return client.responses.create(**payload)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ not in {"BadRequestError", "UnprocessableEntityError", "TypeError"}:
                raise  # 网络错误/限流：交给 retry 装饰器
            last_error = exc
            logger.debug("Call options (%s, reasoning=%s) rejected: %s", tool_choice, with_reasoning, exc)

    raise last_error or RuntimeError("Model call failed")


def _is_auth_error(exc: Exception) -> bool:
    """key 无效 / 无权限：这是配置问题，重试和继续处理其余邮件都没意义。"""
    return type(exc).__name__ in {"AuthenticationError", "PermissionDeniedError"}


def classify_email(email: dict) -> dict:
    """对单封邮件分类。返回 {'priority', 'reason', 'error'}，不抛异常。"""
    try:
        response = _call_model(_build_input(email))
    except ConfigError:
        raise  # 配置问题必须让整轮任务立刻停下并提示用户
    except Exception as exc:  # noqa: BLE001 —— 单封失败要能被上层统计，而不是中断整批
        if _is_auth_error(exc):
            tail = config.DEEPSEEK_API_KEY[-4:] if config.DEEPSEEK_API_KEY else "????"
            raise ConfigError(
                f"DeepSeek rejected the current API key (ends with ****{tail}): {exc}. "
                "Check that DEEPSEEK_API_KEY in .env is valid, and make sure no stale "
                "environment variable of the same name overrides it."
            ) from exc
        logger.error("Classification failed for %s: %s: %s", email.get("id"), type(exc).__name__, exc)
        return {"priority": None, "reason": None, "error": f"{type(exc).__name__}: {exc}"}

    parsed = _extract_call(response)
    if parsed is None:
        parsed = _parse_free_text(getattr(response, "output_text", "") or "")
    if parsed is None:
        logger.warning("Model returned no usable classification: %s", email.get("subject", "")[:40])
        return {"priority": None, "reason": None, "error": "model returned no usable classification"}

    priority, reason = parsed
    logger.info("[%s] %s | %s", priority, (email.get("subject") or "")[:48], reason[:40])
    return {"priority": priority, "reason": reason or "model gave no reason", "error": None}


def classify_and_label(email: dict, labels, apply_fn=None) -> dict:
    """分类 + 打标签的组合入口，返回 {'priority','reason','labeled','error'}。"""
    result = classify_email(email)
    priority = result.get("priority")
    if not priority:
        return {**result, "labeled": False}

    try:
        if apply_fn is not None:
            labeled = bool(apply_fn(email["id"], priority))
        else:
            labeled = bool(labels.apply_priority(email["id"], priority))
    except Exception as exc:  # noqa: BLE001
        logger.error("Labeling failed for %s: %s", email.get("id"), exc)
        return {**result, "labeled": False, "error": f"labeling failed: {exc}"}

    return {**result, "labeled": labeled}
