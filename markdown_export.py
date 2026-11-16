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

import os
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional

from capture_store import QuizCaptureStore


def default_markdown_path() -> str:
    """返回答题记录的 Markdown 落盘路径。

    优先读环境变量 ANSWER_MD_PATH；否则放在 capture_data 下，
    与 SQLite 归档同目录（该目录已被 .gitignore 忽略）。
    """
    configured = os.getenv("ANSWER_MD_PATH")
    if configured:
        return os.path.abspath(configured)
    root = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "capture_data", "答题记录.md")


def index_to_letter(value: Any) -> Optional[str]:
    """把 0-based 索引或字母统一成大写选项字母。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return chr(ord("A") + value) if 0 <= value < 26 else None
    text = str(value).strip().upper()
    if not text:
        return None
    if len(text) == 1 and "A" <= text <= "Z":
        return text
    if text.isdigit():
        index = int(text)
        return chr(ord("A") + index) if 0 <= index < 26 else None
    return None


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

    server_correct_index 是题目接口带回的 answerIndex，可能是用户上次的选择，
    不能当作正确答案；标准答案只认提交响应里的正确答案字段。
    """
    for field in ("server_answer_index", "server_right_index"):
        letter = index_to_letter(question.get(field))
        if letter:
            return letter
    return None


def _verdict_text(question: Dict[str, Any]) -> str:
    """把服务器判定结果写成可读结论。"""
    correct = question.get("server_correct")
    if correct is None:
        return "未知（服务器尚未返回判定）"
    if correct in (1, "1", "true", "True", True):
        return "回答正确"
    if correct in (0, "0", "false", "False", False):
        return "回答错误"
    return str(correct)


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

    inferred = index_to_letter(question.get("answer_index")) or index_to_letter(
        question.get("answer")
    )
    source = question.get("answer_source") or "未记录来源"
    similarity = question.get("similarity")

    lines.append("### 我们的推断")
    lines.append("")
    if inferred:
        detail = f"**{inferred}**（来源：{source}"
        if similarity is not None:
            try:
                detail += f"，相似度 {float(similarity):.4f}"
            except (TypeError, ValueError):
                detail += f"，相似度 {similarity}"
        detail += "）"
        lines.append(detail)
    else:
        lines.append(f"未能给出答案（来源：{source}）")
    explanation = question.get("answer_explanation")
    if explanation:
        lines.extend(["", f"理由：{explanation}"])
    lines.append("")

    lines.append("### 服务器标准答案")
    lines.append("")
    standard = _server_standard_answer(question)
    lines.append(f"标准答案：**{standard}**" if standard else "标准答案：服务器未返回")
    lines.append("")
    lines.append(f"提交判定：{_verdict_text(question)}")
    if question.get("server_analysis"):
        lines.extend(["", f"服务器解析：{question.get('server_analysis')}"])
    lines.append("")

    if submissions:
        lines.append("### 提交记录")
        lines.append("")
        lines.append("| 提交时间 | 提交的答案 | 服务器判定 |")
        lines.append("|---|---|---|")
        for item in submissions:
            submitted = index_to_letter(item.get("submitted_answer")) or str(
                item.get("submitted_answer") or ""
            )
            lines.append(
                f"| {item.get('captured_at') or ''} | {submitted or '—'} | "
                f"{_verdict_text(item)} |"
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
        "每题包含两条答案线：**我们的推断**（题库 / 语义检索 / 大模型的结论与理由）",
        "与**服务器标准答案**（提交后由服务器返回）。",
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
