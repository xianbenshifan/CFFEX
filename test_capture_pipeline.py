"""抓包管线的本地回归测试。"""
# -*- coding: utf-8 -*-
import json
import os
import tempfile

from capture_pipeline import CapturePipeline
from capture_store import QuizCaptureStore


def fake_resolver(question_text):
    """用固定结果模拟语义搜题，避免测试依赖模型。"""
    return {
        "answer": "A",
        "answer_index": 0,
        "similarity": 0.99,
        "answer_source": "test_fixture",
    }


def main():
    """验证题目归档、答案归档和提交归档。"""
    with tempfile.TemporaryDirectory() as folder:
        store = QuizCaptureStore(os.path.join(folder, "capture.db"))
        pipeline = CapturePipeline(
            store,
            fake_resolver,
            markdown_path=os.path.join(folder, "答题记录.md"),
        )

        question_response = {
            "success": True,
            "data": {
                "quizId": 1,
                "nextQuestion": {
                    "questionId": 123,
                    "questionTitle": "测试题目",
                    "answers": ["选项一", "选项二"],
                    "type": "S",
                    "analysis": "服务器解析占位",
                },
            },
        }
        result = pipeline.ingest(
            {
                "request": {"method": "GET", "url": "https://TARGET/question"},
                "response": {"status_code": 200, "body": question_response},
            }
        )
        assert result["question"]["inferred_answer"] == "A"
        assert store.get_question("123")["server_analysis"] == "服务器解析占位"

        answer_response = {
            "success": True,
            "data": {"correct": True, "analysis": "本题答案解析"},
        }
        pipeline.ingest(
            {
                "request": {
                    "method": "POST",
                    "url": "https://TARGET/answer",
                    "body": {"questionId": 123, "quizId": 1, "answerIndex": 0},
                },
                "response": {"status_code": 200, "body": answer_response},
            }
        )
        assert store.stats()["submissions"] == 1
        assert store.stats()["captures"] == 2
        print(json.dumps(store.get_question("123"), ensure_ascii=False, indent=2))
        print("capture pipeline test passed")


if __name__ == "__main__":
    main()
