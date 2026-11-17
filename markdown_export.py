"""
把答题记录导出为单个 Markdown 文件。

每道题同时保留两条答案线：
1. 本地推断的答案（题库精确、本地语义或大模型）及其理由；
2. 服务器返回的标准答案、判定结果与解析。

Markdown 是数据库的投影：每次调用都按当前数据库内容整体重写，
所以同一个问题不会出现两份互相矛盾的记录。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime
from itertools import combinations
from typing import Any, Dict, List, Optional

from capture_store import QuizCaptureStore
import config


def default_markdown_path() -> str:
    """返回答题记录的 Markdown 落盘路径。

    优先读配置项 ANSWER_MD_PATH；否则放在 capture_data 下，
    与 SQLite 归档同目录（该目录已被 .gitignore 忽略）。
    """
    configured = config.get("ANSWER_MD_PATH")
    if configured:
        return os.path.abspath(configured)
    root = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "capture_data", "答题记录.md")


def index_to_letter(value: Any) -> Optional[str]:
    """把本地推断的 0-based 索引或字母统一成大写选项字母。

    多选题答案是连写字母（如 ABD）。只认单个字母会把整道多选显示成未命中。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return chr(ord("A") + value) if 0 <= value < 26 else None
    text = str(value).strip().upper()
    if not text:
        return None
    compact = "".join(ch for ch in text if ch not in " \t,，、;；")
    if compact and all("A" <= ch <= "Z" for ch in compact):
        return "".join(sorted(set(compact)))
    if text.isdigit():
        index = int(text)
        return chr(ord("A") + index) if 0 <= index < 26 else None
    return None


def _letters_from_server_token(token: str) -> Optional[str]:
    """把服务器的 1 基序号串还原成选项字母。

    单选是 "2"，多选是 "1,2,4"。0 不对应任何选项。
    """
    parts = [part.strip() for part in token.split(",") if part.strip()]
    if not parts or any(not part.isdigit() for part in parts):
        return None
    letters: List[str] = []
    for part in parts:
        index = int(part)
        if not 1 <= index <= 26:
            return None
        letters.append(chr(ord("A") + index - 1))
    return "".join(letters)


def _server_digest_map() -> Dict[str, str]:
    """MD5(1 基序号串) → 选项字母。

    单选是 MD5("2")，多选是 MD5("1,2,4") 这种升序逗号串，不是逐个选项各一个 MD5。
    """
    found: Dict[str, str] = {}
    indexes = range(1, 11)
    for size in range(1, len(indexes) + 1):
        for combo in combinations(indexes, size):
            token = ",".join(str(index) for index in combo)
            letters = _letters_from_server_token(token)
            if not letters:
                continue
            digest = hashlib.md5(token.encode()).hexdigest().upper()
            found[digest] = letters
    return found


_SERVER_INDEX_DIGESTS = _server_digest_map()


def server_index_to_letter(value: Any) -> Optional[str]:
    """把服务器返回的标准答案解码成选项字母。

    服务器字段有两种形态：
    - rightIndex：MD5(1 基序号串)，单选 MD5("1") 为 A，多选 MD5("1,2,4") 为 ABD；
    - 明文序号：2 表示 B，1,2 表示 AB。

    序号从 1 起，所以 0 与空值一律返回 None。
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if text.isalpha() and all("A" <= ch <= "Z" for ch in text):
        return "".join(sorted(set(text)))
    hashed = _SERVER_INDEX_DIGESTS.get(text)
    if hashed:
        return hashed
    return _letters_from_server_token(text)


def _option_lines(options: Any) -> List[str]:
    """把任意选项结构渲染成 Markdown 列表。"""
    if isinstance(options, dict):
        return [f"- {key}. {options[key]}" for key in sorted(options.keys())]
    if isinstance(options, list):
        lines = []
        for index, item in enumerate(options):
            label = chr(ord("A") + index)
            if isinstance(item, dict):
                label = str(item.get("label") or label)
                text = item.get("content", item.get("text", ""))
            else:
                text = item
            lines.append(f"- {label}. {text}")
        return lines
    return []


def _server_standard_answer(question: Dict[str, Any]) -> Optional[str]:
    """取服务器给出的标准答案字母。

    只看提交响应回写的两个字段：answerIndex / correctAnswerIndex / rightAnswerIndex
    落到 server_answer_index，rightIndex 落到 server_right_index。
    题目接口带回的 server_correct_index 是用户上次的选择，不能当作标准答案。
    """
    for field in ("server_answer_index", "server_right_index"):
        letter = server_index_to_letter(question.get(field))
        if letter:
            return letter
    return None


def _submitted_letter(value: Any) -> Optional[str]:
    """把提交记录里的序号还原成选项字母。

    提交序号按 1 基写入（服务器内部序号从 1 开始），0 基提交时
    SUBMIT_INDEX_BASE=0，此时 0 表示 A。
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    compact = "".join(ch for ch in text if ch not in " \t,，、;；")
    if compact and all("A" <= ch <= "Z" for ch in compact):
        return "".join(sorted(set(compact)))
    parts = [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]
    if parts and all(part.isdigit() for part in parts):
        base = config.get_int("SUBMIT_INDEX_BASE", 1)
        letters: List[str] = []
        for part in parts:
            index = int(part) - base if base == 1 else int(part)
            if not 0 <= index < 26:
                return None
            letters.append(chr(ord("A") + index))
        return "".join(letters)
    return None


def _render_two_sources(question: Dict[str, Any]) -> List[str]:
    """并排渲染题库侧与大模型侧两份答案，供人工选择。"""
    # 字母答案优先于序号。模型自报的 answer_index 经常和字母不一致，
    # 先看序号会把正确的 C 显示成 A。
    bank_letter = index_to_letter(question.get("bank_answer")) or index_to_letter(
        question.get("bank_answer_index")
    )
    llm_letter = index_to_letter(question.get("llm_answer")) or index_to_letter(
        question.get("llm_answer_index")
    )
    bank_source = question.get("bank_source")
    bank_sim = question.get("bank_similarity")
    llm_conf = question.get("llm_confidence")

    # 旧数据没有双来源字段时，退回单条推断结果
    if bank_letter is None and llm_letter is None:
        fallback = index_to_letter(question.get("answer_index")) or index_to_letter(
            question.get("answer")
        )
        source = question.get("answer_source") or "未记录来源"
        if fallback:
            return [f"**{fallback}**（来源：{source}）"]
        return [f"未能给出答案（来源：{source}）"]

    lines = ["| 来源 | 答案 | 依据 |", "|---|---|---|"]

    if bank_letter:
        basis = bank_source or "题库"
        if bank_sim is not None:
            try:
                basis += f"，相似度 {float(bank_sim):.4f}"
            except (TypeError, ValueError):
                basis += f"，相似度 {bank_sim}"
        lines.append(f"| 题库 | **{bank_letter}** | {basis} |")
    else:
        lines.append("| 题库 | 未命中 | — |")

    if question.get("llm_status") == "pending" and not llm_letter:
        lines.append("| 大模型 | 搜索中 | 正在生成 |")
    elif llm_letter:
        basis = "大模型"
        if llm_conf is not None:
            try:
                basis += f"，置信度 {float(llm_conf):.4f}"
            except (TypeError, ValueError):
                basis += f"，置信度 {llm_conf}"
        lines.append(f"| 大模型 | **{llm_letter}** | {basis} |")
    else:
        lines.append("| 大模型 | 未返回 | — |")

    if bank_letter and llm_letter:
        if bank_letter == llm_letter:
            lines.extend(["", f"结论：两个来源一致，均为 **{bank_letter}**。"])
        else:
            lines.extend(
                ["", f"结论：两个来源**不一致**（题库 {bank_letter} / 大模型 {llm_letter}），请人工判断。"]
            )
    elif bank_letter:
        lines.extend(["", f"结论：只有题库给出答案 **{bank_letter}**（大模型未返回）。"])
    elif llm_letter:
        lines.extend(["", f"结论：只有大模型给出答案 **{llm_letter}**（题库未命中）。"])
    else:
        lines.extend(["", "结论：两个来源都没有给出答案。"])

    bank_explanation = question.get("bank_explanation")
    if bank_explanation:
        lines.extend(["", f"题库理由：{bank_explanation}"])
    llm_explanation = question.get("llm_explanation")
    if llm_explanation:
        lines.extend(["", f"大模型理由：{llm_explanation}"])
    return lines


def _verdict_text(verdict: Any) -> str:
    """把服务器判定结果写成可读结论。"""
    if verdict is None:
        return "未知（服务器尚未返回判定）"
    if verdict in (1, "1", "true", "True", True):
        return "回答正确"
    if verdict in (0, "0", "false", "False", False):
        return "回答错误"
    return str(verdict)


def render_question(question: Dict[str, Any], submissions: List[Dict[str, Any]]) -> str:
    """渲染单道题的完整记录。"""
    question_id = str(question.get("question_id") or "").strip() or "未知"
    title = str(question.get("question_title") or "").strip() or "（题干缺失）"

    lines = [f"## #{question_id} {title}", ""]

    meta = []
    quiz_id = question.get("quiz_id")
    if quiz_id:
        meta.append(f"quizId {quiz_id}")
    if question.get("type"):
        meta.append(f"题型 {question.get('type')}")
    if question.get("first_seen"):
        meta.append(f"首次记录 {question.get('first_seen')}")
    if question.get("last_seen"):
        meta.append(f"最近更新 {question.get('last_seen')}")
    if meta:
        lines.extend(["> " + " ｜ ".join(meta), ""])

    options = _option_lines(question.get("options"))
    if options:
        lines.append("**选项**")
        lines.append("")
        lines.extend(options)
        lines.append("")

    lines.append("### 我们的推断")
    lines.append("")
    lines.extend(_render_two_sources(question))
    lines.append("")

    lines.append("### 服务器标准答案")
    lines.append("")
    standard = _server_standard_answer(question)
    lines.append(f"标准答案：**{standard}**" if standard else "标准答案：服务器未返回")
    lines.append("")
    lines.append(f"提交判定：{_verdict_text(question.get('server_correct'))}")
    if question.get("server_analysis"):
        lines.extend(["", f"服务器解析：{question.get('server_analysis')}"])
    lines.append("")

    if submissions:
        lines.append("### 提交记录")
        lines.append("")
        lines.append("| 提交时间 | 提交的答案 | 服务器判定 |")
        lines.append("|---|---|---|")
        for item in submissions:
            submitted = _submitted_letter(item.get("submitted_answer"))
            lines.append(
                f"| {item.get('captured_at') or ''} | {submitted or '—'} | "
                f"{_verdict_text(item.get('server_correct'))} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_document(
    questions: List[Dict[str, Any]],
    submissions_by_question: Dict[str, List[Dict[str, Any]]],
) -> str:
    """渲染整份答题记录文档。"""
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = [
        "# CFFEX 答题记录",
        "",
        f"> 共 {len(questions)} 道题 ｜ 生成时间 {generated}",
        "",
        "每题同时给出**题库**与**大模型**两个来源的答案供人工选择（不会自动提交），",
        "以及服务器返回的标准答案。",
        "",
        "---",
        "",
    ]
    body = [
        render_question(item, submissions_by_question.get(str(item.get("question_id")), []))
        for item in questions
    ]
    return "\n".join(header) + "\n" + "\n".join(body)


def export_markdown(
    store: QuizCaptureStore,
    path: Optional[str] = None,
    limit: int = 5000,
) -> Dict[str, Any]:
    """按数据库当前内容整体重写答题记录，返回写入统计。"""
    target = path or default_markdown_path()
    questions = store.list_questions(limit=limit, offset=0)
    submissions_by_question = {
        str(item.get("question_id")): store.list_submissions(str(item.get("question_id")))
        for item in questions
        if item.get("question_id")
    }
    document = render_document(questions, submissions_by_question)

    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # 先写临时文件再替换，避免写入中途崩溃留下半份记录。
    handle, temp_path = tempfile.mkstemp(dir=directory or ".", suffix=".md.tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(document)
        os.replace(temp_path, target)
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise

    return {
        "path": target,
        "questions": len(questions),
        "bytes": len(document.encode("utf-8")),
    }
