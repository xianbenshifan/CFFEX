"""双来源答案的回归测试。"""
# -*- coding: utf-8 -*-
import os
import tempfile

from capture_pipeline import CapturePipeline
from capture_store import QuizCaptureStore
from hybrid_answer import HybridAnswerEngine
from markdown_export import export_markdown


class FakeEngine:
    """用固定结果模拟题库侧与大模型侧。"""

    def __init__(self, bank, llm):
        self._bank = bank
        self._llm = llm

    def resolve_both(self, question_text, options=None):
        return {
            "question_text": question_text,
            "options": options,
            "bank": self._bank,
            "llm": self._llm,
            "agree": None,
        }


def main():
    """验证两个来源分别入库与渲染。"""

    print("=== 1. 题库与大模型都命中且一致 ===")
    with tempfile.TemporaryDirectory() as folder:
        store = QuizCaptureStore(os.path.join(folder, "c.db"))
        engine = FakeEngine(
            {"answer": "B", "answer_index": 1, "answer_source": "local_exact", "similarity": 1.0},
            {"answer": "B", "answer_index": 1, "similarity": 0.95, "explanation": "模型也选 B"},
        )
        pipeline = CapturePipeline(store, engine.resolve_both, os.path.join(folder, "a.md"))
        res = pipeline.ingest(
            {
                "request": {"method": "GET", "url": "u", "path": "/qa/api/quiz/X",
                            "host": "quiz-activity.eastmoney.com"},
                "response": {"status_code": 200, "body": {"data": {"nextQuestion": {
                    "questionId": 1, "questionTitle": "测试题一",
                    "answers": ["甲", "乙", "丙"], "type": "S"}}}},
            }
        )
        q = res["question"]
        assert q["bank_answer"] == "B", q
        assert q["llm_answer"] == "B", q
        assert q["bank_source"] == "local_exact"
        print(f"   题库={q['bank_answer']} 大模型={q['llm_answer']}")

        export_markdown(store, path=os.path.join(folder, "a.md"))
        doc = open(os.path.join(folder, "a.md"), encoding="utf-8").read()
        assert "| 题库 | **B** |" in doc, "题库行缺失"
        assert "| 大模型 | **B** |" in doc, "大模型行缺失"
        assert "两个来源一致" in doc
        print("   MD 渲染正确（含一致性结论）")

    print("=== 2. 两个来源不一致时应明确提示 ===")
    with tempfile.TemporaryDirectory() as folder:
        store = QuizCaptureStore(os.path.join(folder, "c.db"))
        engine = FakeEngine(
            {"answer": "A", "answer_index": 0, "answer_source": "local_semantic", "similarity": 0.85},
            {"answer": "C", "answer_index": 2, "similarity": 0.7},
        )
        pipeline = CapturePipeline(store, engine.resolve_both, os.path.join(folder, "a.md"))
        pipeline.ingest(
            {
                "request": {"method": "GET", "url": "u", "path": "/qa/api/quiz/X",
                            "host": "quiz-activity.eastmoney.com"},
                "response": {"status_code": 200, "body": {"data": {"nextQuestion": {
                    "questionId": 2, "questionTitle": "测试题二",
                    "answers": ["甲", "乙", "丙"], "type": "S"}}}},
            }
        )
        export_markdown(store, path=os.path.join(folder, "a.md"))
        doc = open(os.path.join(folder, "a.md"), encoding="utf-8").read()
        assert "不一致" in doc, "未提示不一致"
        assert "题库 A / 大模型 C" in doc, doc[:600]
        print("   一致标记与差异内容正确")

    print("=== 3. 只有题库命中时大模型侧显示未返回 ===")
    with tempfile.TemporaryDirectory() as folder:
        store = QuizCaptureStore(os.path.join(folder, "c.db"))
        engine = FakeEngine(
            {"answer": "D", "answer_index": 3, "answer_source": "local_exact", "similarity": 1.0},
            None,
        )
        pipeline = CapturePipeline(store, engine.resolve_both, os.path.join(folder, "a.md"))
        pipeline.ingest(
            {
                "request": {"method": "GET", "url": "u", "path": "/qa/api/quiz/X",
                            "host": "quiz-activity.eastmoney.com"},
                "response": {"status_code": 200, "body": {"data": {"nextQuestion": {
                    "questionId": 3, "questionTitle": "测试题三",
                    "answers": ["甲", "乙", "丙", "丁"], "type": "S"}}}},
            }
        )
        export_markdown(store, path=os.path.join(folder, "a.md"))
        doc = open(os.path.join(folder, "a.md"), encoding="utf-8").read()
        assert "| 大模型 | 未返回 |" in doc
        assert "只有题库给出答案" in doc
        print("   单来源降级正确")

    print("=== 4. 真实引擎的 resolve_both 结构 ===")
    eng = HybridAnswerEngine(database_path="database.json")
    out = eng.resolve_both("完全不存在的题目内容 A. 甲 B. 乙", ["甲", "乙"])
    assert "bank" in out and "llm" in out and "agree" in out
    print(f"   bank={out['bank']}  llm有值={out['llm'] is not None}  agree={out['agree']}")

    print("multi-source test passed")


if __name__ == "__main__":
    main()
