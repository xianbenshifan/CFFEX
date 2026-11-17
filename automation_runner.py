"""
自动答题编排器：
抓包题目 -> 本地匹配 -> 大模型兜底 -> 提交答案 -> 归档服务器结果。

提交通道优先使用 Charles MCP bridge；未配置 bridge 时可使用直接 HTTP
提交器。目标地址、MCP 工具名和开关都通过环境变量控制。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests

import config
from capture_pipeline import CapturePipeline
from capture_store import QuizCaptureStore
from hybrid_answer import HybridAnswerEngine
from progress_tracker import ProgressTracker, get_progress_tracker

logger = logging.getLogger(__name__)


class AnswerSubmitter:
    """把答案提交到目标 API。

    MCP 只用于收题：`charles-mcp` 暴露的工具里没有任何能凭空发出新请求的能力
    （`reverse_replay_entry` 只能重放已存在的抓包条目），因此提交一律直连。
    """

    def __init__(self) -> None:
        self.answer_url = config.get(
            "QUIZ_ANSWER_URL",
            "https://quiz-activity.eastmoney.com/qa/api/quiz/answer",
        )
        self.allow_direct = config.get_int("ALLOW_DIRECT_SUBMIT", 1) == 1
        # 提交序号基数：服务器把正确答案编码成 MD5(1..N)，说明内部序号从 1 开始，
        # 因此默认按 1 基提交。若实测显示该接口接受 0 基，把 SUBMIT_INDEX_BASE 设为 0。
        self.submit_index_base = config.get_int("SUBMIT_INDEX_BASE", 1)
        if self.submit_index_base not in (0, 1):
            self.submit_index_base = 1

    def _submit_index(self, answer: Dict[str, Any]) -> Any:
        """把本地 0 基推理索引转换为提交序号。"""
        index = answer.get("answer_index")
        if index is None or isinstance(index, bool):
            # 没有可靠索引时退回答案字母，由服务端自行解释，避免凭空猜偏移。
            return answer.get("answer")
        try:
            return int(index) + self.submit_index_base
        except (TypeError, ValueError):
            return answer.get("answer")

    def submit(
        self,
        question: Dict[str, Any],
        answer: Dict[str, Any],
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """提交答案并返回服务器原始响应。"""
        body = {
            "questionId": question.get("question_id"),
            "quizId": question.get("quiz_id"),
            "answerIndex": self._submit_index(answer),
        }

        if not self.allow_direct:
            return {
                "transport": "disabled",
                "body": {},
                "error": "未启用直接 HTTP 提交",
            }

        headers = dict((request_context or {}).get("headers") or {})
        headers.pop("Host", None)
        headers.pop("Content-Length", None)
        # 默认直连目标站点；需要走抓包代理时显式设置 SUBMIT_PROXY_URL。
        submit_proxy = config.get("SUBMIT_PROXY_URL")
        proxies = {"http": submit_proxy, "https": submit_proxy} if submit_proxy else {}
        try:
            response = requests.post(
                self.answer_url,
                json=body,
                headers=headers,
                timeout=config.get_float("SUBMIT_TIMEOUT", 15.0),
                verify=config.get_int("VERIFY_TLS", 0) == 1,
                proxies=proxies or None,
            )
        except requests.RequestException as exc:
            # 网络或代理不可用时只报告提交通道失败，不能让整条抓包归档跟着失败。
            logger.warning("直接提交失败: %s", exc)
            return {
                "transport": "direct_http",
                "body": {},
                "error": f"{type(exc).__name__}: {exc}",
            }
        try:
            response_body = response.json()
        except ValueError:
            response_body = {"text": response.text}
        return {
            "transport": "direct_http",
            "status_code": response.status_code,
            "body": response_body,
        }


class QuizAutomation:
    """处理一条抓包并完成答案编排。"""

    def __init__(
        self,
        store: QuizCaptureStore,
        local_resolver=None,
        progress: Optional[ProgressTracker] = None,
    ) -> None:
        # 双来源：题库侧（精确 + 语义）与大模型侧并列给出，不自动提交。
        self.engine = HybridAnswerEngine(local_resolver=local_resolver)
        self.pipeline = CapturePipeline(
            store=store, answer_resolver=self.engine.resolve_both
        )
        self.submitter = AnswerSubmitter()
        self.store = store
        self.progress = progress or get_progress_tracker()

    def handle_capture(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """解析抓包并给出双来源答案，不做任何提交。

        是否提交答案由人决定；本方法只把题目归档、把题库侧与大模型侧的结论
        并列呈现。
        """
        self.progress.start_run()
        self.progress.update("capture", "已收到抓包，正在解析题目")
        current = self.pipeline.ingest(envelope, source="charles_mcp_automation")
        first_result = current

        question = current.get("question")
        steps: List[Dict[str, Any]] = []
        if not question:
            self.progress.update("idle", "抓包中没有可处理题目", status="idle")
        else:
            question_id = question.get("question_id")
            self.progress.update(
                "question_parsed",
                f"已提取题目 {question_id}",
                current=1,
                question_id=question_id,
            )

            bank = {
                "answer": question.get("bank_answer"),
                "answer_index": question.get("bank_answer_index"),
                "source": question.get("bank_source"),
                "similarity": question.get("bank_similarity"),
                "explanation": question.get("bank_explanation"),
            }
            llm = {
                "answer": question.get("llm_answer"),
                "answer_index": question.get("llm_answer_index"),
                "confidence": question.get("llm_confidence"),
                "explanation": question.get("llm_explanation"),
            }
            agree = None
            if bank["answer"] and llm["answer"]:
                agree = (
                    str(bank["answer"]).strip().upper()
                    == str(llm["answer"]).strip().upper()
                )

            step = {
                "question_id": question_id,
                "bank": bank,
                "llm": llm,
                "agree": agree,
                "submitted": False,
            }
            steps.append(step)

            if not bank["answer"] and not llm["answer"]:
                self.progress.fail("两个来源都没有给出答案", question_id=question_id)
                current["automation"] = {"status": "no_answer", **step}
            else:
                self.progress.update(
                    "answer_ready",
                    f"答案已就绪：题库={bank['answer'] or '无'} / 大模型={llm['answer'] or '无'}"
                    + ("（一致）" if agree else "（不一致，需人工判断）" if agree is False else ""),
                    current=1,
                    question_id=question_id,
                    answer_source=bank["source"] or ("llm_api" if llm["answer"] else None),
                    similarity=bank["similarity"] or llm["confidence"],
                )
                self.progress.finish("已给出答案，等待人工选择（不会自动提交）", current=1)
                current["automation"] = {"status": "ready_for_manual_choice", **step}

        first_result["automation_steps"] = steps
        return first_result


def main() -> None:
    """命令行模式：从 stdin 读取 JSON 抓包信封，每行处理一条。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = QuizCaptureStore()
    automation = QuizAutomation(store=store)
    for line in os.sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            print(json.dumps(automation.handle_capture(json.loads(line)), ensure_ascii=False))
        except Exception as exc:
            logger.exception("自动化处理失败: %s", exc)


if __name__ == "__main__":
    main()
