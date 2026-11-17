"""
Charles/MCP 抓包统一解析管线。

输入可以是：
1. Charles Mirror 保存的响应 JSON；
2. Charles MCP 发送的 request/response 信封；
3. 旧版 /qa/api/quiz/receive 直接发送的响应 JSON。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, Optional

from capture_store import QuizCaptureStore
from markdown_export import export_markdown

logger = logging.getLogger(__name__)


def _unwrap_text_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Charles 有时把 JSON 请求体包在 text 字段里。

    不拆开的话 questionId 读不到，这道题的对错和标准答案就不会写回题目。
    """
    text = body.get("text")
    if not isinstance(text, str):
        return body
    if "questionId" in body or "answerIndex" in body:
        return body
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return body
    return parsed if isinstance(parsed, dict) else body


def _as_dict(value: Any) -> Dict[str, Any]:
    """把字符串或对象转换成字典。"""
    if isinstance(value, dict):
        return _unwrap_text_body(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return _unwrap_text_body(parsed) if isinstance(parsed, dict) else {}
    return {}


def _options_text(options: Any) -> list[str]:
    """统一题目选项格式。"""
    if isinstance(options, dict):
        return [str(options[key]) for key in sorted(options.keys())]
    if isinstance(options, list):
        return [
            str(item.get("content", item.get("text", "")))
            if isinstance(item, dict)
            else str(item)
            for item in options
        ]
    return []


def _answer_index(answer: Any, options: Any) -> Optional[int]:
    """把题库答案字母转换为当前题目选项的 0-based 索引。"""
    answer_text = str(answer or "").strip().upper()
    if len(answer_text) == 1 and "A" <= answer_text <= "Z":
        index = ord(answer_text) - ord("A")
        if isinstance(options, list) and 0 <= index < len(options):
            return index
        if isinstance(options, dict):
            labels = sorted(options.keys())
            return labels.index(answer_text) if answer_text in labels else None
    return None


def _extract_response_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    """从 MCP/旧接口/裸响应中提取响应 JSON。"""
    response = payload.get("response")
    if isinstance(response, dict):
        body = response.get("body", response.get("json", response))
        body_dict = _as_dict(body)
        if body_dict:
            return body_dict
    body = payload.get("body")
    body_dict = _as_dict(body)
    if body_dict:
        return body_dict
    return payload


def _extract_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """提取请求信息，用于记录提交答案和抓包元数据。"""
    request = payload.get("request")
    return request if isinstance(request, dict) else {}


class CapturePipeline:
    """负责去重、解析、检索和持久化。"""

    def __init__(
        self,
        store: QuizCaptureStore,
        answer_resolver: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
        markdown_path: Optional[str] = None,
        llm_resolver: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
    ) -> None:
        self.store = store
        self.answer_resolver = answer_resolver
        self.llm_resolver = llm_resolver
        self.markdown_path = markdown_path

    def ingest(self, payload: Dict[str, Any], source: str = "charles_mcp") -> Dict[str, Any]:
        """接收一条抓包并返回本次解析结果。"""
        payload = payload if isinstance(payload, dict) else {}
        response_body = _extract_response_body(payload)
        request = _extract_request(payload)
        capture_id = self.store.record_capture(payload, source=source)

        data = response_body.get("data")
        data = data if isinstance(data, dict) else {}
        next_question = data.get("nextQuestion")
        if not isinstance(next_question, dict):
            # 兼容某些接口直接把题目放在 data 下。
            next_question = data if data.get("questionId") or data.get("questionTitle") else None

        question_result = None
        if next_question:
            question_result = self._save_question(
                next_question=next_question,
                response_data=data,
                quiz_id=data.get("quizId"),
                capture_id=capture_id,
            )

        submission_result = self._save_submission(
            request=request,
            response_data=data,
            capture_id=capture_id,
        )

        # 题目和服务器判定都已入库，再整体刷新答题记录。
        self._export_markdown()

        return {
            "capture_id": capture_id,
            "question": question_result,
            "submission": submission_result,
            "stats": self.store.stats(),
        }

    def _export_markdown(self) -> None:
        """把当前归档写成 Markdown；失败不影响抓包主流程。"""
        try:
            export_markdown(self.store, path=self.markdown_path)
        except OSError:
            logger.exception("答题记录 Markdown 写入失败")

    def _save_question(
        self,
        next_question: Dict[str, Any],
        response_data: Dict[str, Any],
        quiz_id: Any,
        capture_id: int,
    ) -> Dict[str, Any]:
        """提取题目、服务器解析并补充本地题库答案。"""
        question_id = str(next_question.get("questionId") or next_question.get("id") or "")
        title = str(next_question.get("questionTitle") or next_question.get("question") or "")
        options = next_question.get("answers") or next_question.get("options") or []
        option_texts = _options_text(options)
        question_text = title
        if option_texts:
            question_text += " " + " ".join(
                f"{chr(65 + index)}. {text}" for index, text in enumerate(option_texts)
            )

        # 答案接口 data 上的 analysis / rightIndex 属于刚提交的那一题，
        # 不能写到 data.nextQuestion。否则下一题会继承上一题的标准答案。
        server_analysis = next_question.get("analysis") or next_question.get("remark")

        answer = None
        similarity = None
        answer_index = None
        answer_source = None
        answer_explanation = None
        bank_answer = None
        bank_index = None
        bank_source = None
        bank_similarity = None
        bank_explanation = None
        bank_matched_question = None
        bank_matched_number = None
        bank_matched_options = None
        llm_answer = None
        llm_index = None
        llm_explanation = None
        llm_confidence = None
        llm_elapsed = None

        if self.answer_resolver and question_text:
            try:
                try:
                    match = self.answer_resolver(question_text, options)
                except TypeError:
                    match = self.answer_resolver(question_text)
            except Exception:
                logger.exception("题目答案解析失败: question_id=%s", question_id)
                match = None

            if isinstance(match, dict) and "bank" in match:
                # 双来源结果：题库与大模型并列
                bank = match.get("bank") or {}
                llm = match.get("llm") or {}
                if bank:
                    bank_answer = bank.get("answer")
                    bank_index = bank.get("answer_index")
                    if bank_index is None:
                        bank_index = _answer_index(bank_answer, options)
                    bank_source = bank.get("answer_source", "local_database")
                    bank_similarity = bank.get("similarity")
                    bank_explanation = bank.get("explanation")
                    # 保留题库里匹配到的那道题，便于对照题干与选项差异
                    bank_matched_question = bank.get("matched_question")
                    bank_matched_number = bank.get("matched_number")
                    bank_matched_options = bank.get("matched_options")
                    if bank_matched_options is None:
                        bank_matched_options = bank.get("options")
                if llm:
                    llm_answer = llm.get("answer")
                    llm_index = llm.get("answer_index")
                    if llm_index is None:
                        llm_index = _answer_index(llm_answer, options)
                    llm_explanation = llm.get("explanation")
                    llm_confidence = llm.get("similarity")
                    llm_elapsed = llm.get("elapsed")
                # 兼容旧字段：优先填题库侧，没有题库结果才用大模型侧
                if bank_answer:
                    answer, answer_index = bank_answer, bank_index
                    answer_source = bank_source
                    similarity = bank_similarity
                    answer_explanation = bank_explanation
                elif llm_answer:
                    answer, answer_index = llm_answer, llm_index
                    answer_source = "llm_api"
                    similarity = llm_confidence
                    answer_explanation = llm_explanation
            elif match:
                answer = match.get("answer")
                similarity = match.get("similarity")
                answer_index = match.get("answer_index")
                if answer_index is None:
                    answer_index = _answer_index(answer, options)
                answer_source = match.get("answer_source", "local_database")
                answer_explanation = match.get("explanation")
                bank_answer, bank_index = answer, answer_index
                bank_source, bank_similarity = answer_source, similarity
                bank_explanation = answer_explanation

        # 服务返回的 index 可能是用户上次选择的索引，单独保存，避免误当作正确答案。
        result = {
            "question_id": question_id,
            "quiz_id": quiz_id,
            "type": next_question.get("type"),
            "question_title": title,
            "question_text": question_text,
            "options": options,
            "server_analysis": server_analysis,
            "answer_explanation": answer_explanation,
            "server_correct_index": next_question.get("answerIndex"),
            "server_right_index": next_question.get("rightIndex"),
            "inferred_answer": answer,
            "inferred_answer_index": answer_index,
            "similarity": similarity,
            "answer_source": answer_source,
            "bank_answer": bank_answer,
            "bank_answer_index": bank_index,
            "bank_source": bank_source,
            "bank_similarity": bank_similarity,
            "bank_explanation": bank_explanation,
            "bank_matched_question": bank_matched_question,
            "bank_matched_number": bank_matched_number,
            "bank_matched_options": bank_matched_options,
            "llm_answer": llm_answer,
            "llm_answer_index": llm_index,
            "llm_explanation": llm_explanation,
            "llm_confidence": llm_confidence,
            "llm_elapsed": llm_elapsed,
            "llm_status": None,
            "raw": next_question,
        }
        prior = self.store.get_question(question_id) if question_id else None
        if prior and prior.get("llm_answer"):
            result["llm_answer"] = prior.get("llm_answer")
            result["llm_answer_index"] = prior.get("llm_answer_index")
            result["llm_explanation"] = prior.get("llm_explanation")
            result["llm_confidence"] = prior.get("llm_confidence")
            result["llm_elapsed"] = prior.get("llm_elapsed")
            result["llm_status"] = prior.get("llm_status") or "ready"
        elif prior and prior.get("llm_status") == "pending":
            result["llm_status"] = "pending"
        elif self.llm_resolver and question_id and not result.get("llm_answer"):
            result["llm_status"] = "pending"
        elif result.get("llm_answer"):
            result["llm_status"] = "ready"
        self.store.upsert_question(result, capture_id=capture_id)
        if result.get("llm_status") == "pending" and not (
            prior and prior.get("llm_status") == "pending"
        ):
            self.store.set_llm_status(question_id, "pending")
            self._schedule_llm(question_id, question_text, options)
        return result

    def _schedule_llm(self, question_id: str, question_text: str, options: Any) -> None:
        """题目先入库。大模型返回后再补上答案，不挡住面板。"""

        def run() -> None:
            try:
                found = self.llm_resolver(question_text, options)
            except Exception:
                logger.exception("大模型补答失败: question_id=%s", question_id)
                found = None
            if found and found.get("answer"):
                self.store.update_llm(question_id, found, status="ready")
            else:
                self.store.update_llm(question_id, None, status="failed")
            self._export_markdown()

        threading.Thread(
            target=run, daemon=True, name=f"llm-{question_id}"
        ).start()

    def _save_submission(
        self,
        request: Dict[str, Any],
        response_data: Dict[str, Any],
        capture_id: int,
    ) -> Optional[Dict[str, Any]]:
        """提取 POST 答案提交及服务器判定。"""
        body = request.get("body") or request.get("json") or {}
        body = _as_dict(body)
        question_id = body.get("questionId")
        if question_id is None and not any(
            key in response_data for key in ("correct", "rightIndex", "analysis")
        ):
            return None

        submission = {
            "question_id": question_id,
            "quiz_id": body.get("quizId"),
            "submitted_answer": body.get("answerIndex", body.get("answer")),
            "server_correct": response_data.get("correct"),
            "server_analysis": response_data.get("analysis"),
            "raw": {
                "request": request,
                "response": response_data,
            },
        }
        self.store.record_submission(submission, capture_id=capture_id)
        # 提交接口的判定属于上一道题，单独回写，避免误绑定到 nextQuestion。
        self.store.update_server_result(
            question_id=question_id,
            correct=response_data.get("correct"),
            answer_index=(
                response_data.get("answerIndex")
                or response_data.get("correctAnswerIndex")
                or response_data.get("rightAnswerIndex")
            ),
            analysis=response_data.get("analysis"),
            right_index=response_data.get("rightIndex"),
        )
        return submission
