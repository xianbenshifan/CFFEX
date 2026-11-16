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
from typing import Any, Dict, Optional

import requests

from capture_pipeline import CapturePipeline
from capture_store import QuizCaptureStore
from charles_mcp_adapter import CharlesMcpClient
from hybrid_answer import HybridAnswerEngine
from progress_tracker import ProgressTracker, get_progress_tracker

logger = logging.getLogger(__name__)


class AnswerSubmitter:
    """把答案提交到 Charles MCP bridge 或目标 API。"""

    def __init__(self, mcp: Optional[CharlesMcpClient] = None) -> None:
        self.mcp = mcp
        self.answer_url = os.getenv(
            "QUIZ_ANSWER_URL",
            "https://quiz-activity.eastmoney.com/qa/api/quiz/answer",
        )
        self.prefer_mcp = os.getenv("PREFER_CHARLES_MCP", "1") == "1"
        self.allow_direct = os.getenv("ALLOW_DIRECT_SUBMIT", "1") == "1"
        # 提交序号基数：服务器把正确答案编码成 MD5(1..N)，说明内部序号从 1 开始，
        # 因此默认按 1 基提交。若实测显示该接口接受 0 基，把 SUBMIT_INDEX_BASE 设为 0。
        self.submit_index_base = 1
        if os.getenv("SUBMIT_INDEX_BASE", "").strip() == "0":
            self.submit_index_base = 0

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

        if self.prefer_mcp and self.mcp:
            try:
                tool = os.getenv("CHARLES_MCP_SUBMIT_TOOL", "send_request")
                result = self.mcp.call_tool(
                    tool,
                    {
                        "method": "POST",
                        "url": self.answer_url,
                        "headers": (request_context or {}).get("headers", {}),
                        "body": body,
                    },
                )
                return {"transport": "charles_mcp", "body": result}
            except Exception as exc:
                logger.warning("Charles MCP 提交失败，准备按配置回退: %s", exc)

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
        submit_proxy = os.getenv("SUBMIT_PROXY_URL")
        proxies = {"http": submit_proxy, "https": submit_proxy} if submit_proxy else {}
        try:
            response = requests.post(
                self.answer_url,
                json=body,
                headers=headers,
                timeout=float(os.getenv("SUBMIT_TIMEOUT", "15")),
                verify=os.getenv("VERIFY_TLS", "0") == "1",
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
    """处理一条 Charles MCP 抓包并完成答案编排。"""

    def __init__(
        self,
        store: QuizCaptureStore,
        local_resolver=None,
        mcp: Optional[CharlesMcpClient] = None,
        progress: Optional[ProgressTracker] = None,
    ) -> None:
        # 答案路由：精确题库 -> 本地语义检索 -> LLM API。
        self.engine = HybridAnswerEngine(local_resolver=local_resolver)
        self.pipeline = CapturePipeline(store=store, answer_resolver=self.engine.resolve)
        self.submitter = AnswerSubmitter(mcp=mcp)
        self.store = store
        self.progress = progress or get_progress_tracker()

    def handle_capture(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """循环处理当前题目和服务器返回的 nextQuestion。"""
        self.progress.start_run()
        self.progress.update("capture", "已收到 Charles MCP 抓包")
        current = self.pipeline.ingest(envelope, source="charles_mcp_automation")
        first_result = current
        steps = []
        max_steps = max(1, int(os.getenv("MAX_AUTO_QUESTIONS", "100")))

        for _ in range(max_steps):
            question = current.get("question")
            if not question:
                self.progress.update("idle", "抓包中没有可处理题目", status="idle")
                break

            self.progress.update(
                "question_parsed",
                f"已提取题目 {question.get('question_id')}",
                current=len(steps) + 1,
                question_id=question.get("question_id"),
            )
            answer = {
                "answer": question.get("inferred_answer"),
                "answer_index": question.get("inferred_answer_index"),
                "similarity": question.get("similarity"),
                "answer_source": question.get("answer_source"),
            }
            if not answer["answer"]:
                self.progress.fail(
                    "没有找到可用答案",
                    question_id=question.get("question_id"),
                )
                current["automation"] = {"status": "no_answer"}
                break

            self.progress.update(
                "answer_ready",
                f"答案已生成：{answer.get('answer_source')}",
                current=len(steps) + 1,
                question_id=question.get("question_id"),
                answer_source=answer.get("answer_source"),
                similarity=answer.get("similarity"),
            )
            request_context = envelope.get("request") or {}
            self.progress.update(
                "submitting",
                "正在提交答案",
                current=len(steps) + 1,
                question_id=question.get("question_id"),
                answer_source=answer.get("answer_source"),
            )
            submit_result = self.submitter.submit(question, answer, request_context)
            step = {
                "question_id": question.get("question_id"),
                "answer": answer,
                "submit_result": submit_result,
            }
            steps.append(step)
            self.progress.update(
                "submitted",
                f"答案已提交，通道：{submit_result.get('transport')}",
                current=len(steps),
                question_id=question.get("question_id"),
                transport=submit_result.get("transport"),
            )

            # 没有提交结果时停止，避免重复提交同一道题。
            if not submit_result.get("body"):
                current["automation"] = {
                    "status": submit_result.get("transport"),
                    "answer": answer,
                    "submit_result": submit_result,
                }
                self.progress.fail(
                    "提交未返回服务器响应",
                    question_id=question.get("question_id"),
                    transport=submit_result.get("transport"),
                )
                break

            # 把服务器返回的 correct / analysis / nextQuestion 再送回管线。
            current = self.pipeline.ingest(
                {
                    "request": {
                        "method": "POST",
                        "url": self.submitter.answer_url,
                        "headers": request_context.get("headers", {}),
                        "body": {
                            "questionId": question.get("question_id"),
                            "quizId": question.get("quiz_id"),
                            # 归档要与真实提交一致，用转换后的序号。
                            "answerIndex": self.submitter._submit_index(answer),
                        },
                    },
                    "response": {
                        "status_code": submit_result.get("status_code", 200),
                        "body": submit_result.get("body"),
                    },
                },
                source="charles_mcp_submit_result",
            )
            self.progress.update(
                "response_saved",
                "服务器响应已保存，准备处理下一题",
                current=len(steps),
                question_id=question.get("question_id"),
                transport=submit_result.get("transport"),
            )
            envelope = {
                "request": {
                    "method": "POST",
                    "url": self.submitter.answer_url,
                    "headers": request_context.get("headers", {}),
                }
            }

        first_result["automation_steps"] = steps
        if current is not first_result:
            first_result["follow_up"] = current
        if steps and current.get("question") is None:
            self.progress.finish("本轮自动答题完成", current=len(steps))
        return first_result


def main() -> None:
    """命令行模式：从 stdin 读取 JSON 抓包信封，每行处理一条。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = QuizCaptureStore()
    mcp = CharlesMcpClient() if os.getenv("CHARLES_MCP_URL") else None
    automation = QuizAutomation(store=store, mcp=mcp)
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
