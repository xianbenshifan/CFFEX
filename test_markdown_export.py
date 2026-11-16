"""答题记录 Markdown 导出的回归测试。"""
# -*- coding: utf-8 -*-
import os
import tempfile

from capture_pipeline import CapturePipeline
from capture_store import QuizCaptureStore
from markdown_export import export_markdown, index_to_letter


def fake_resolver(question_text):
    """固定返回 A，附带理由，避免测试依赖模型。"""
    return {
        "answer": "A",
        "answer_index": 0,
        "similarity": 0.87,
        "answer_source": "local_semantic",
        "explanation": "依据题库第 12 题的解析。",
    }


def main():
    """验证推断答案、服务器标准答案与提交记录都进 Markdown。"""
    assert index_to_letter(0) == "A"
    assert index_to_letter("2") == "C"
    assert index_to_letter("c") == "C"
    assert index_to_letter(None) is None

    with tempfile.TemporaryDirectory() as folder:
        store = QuizCaptureStore(os.path.join(folder, "capture.db"))
        markdown_path = os.path.join(folder, "答题记录.md")
        pipeline = CapturePipeline(store, fake_resolver, markdown_path=markdown_path)

        question_response = {
            "success": True,
            "data": {
                "quizId": 7,
                "nextQuestion": {
                    "questionId": 456,
                    "questionTitle": "中金所股指期货最小变动价位是多少",
                    "answers": ["0.2 点", "0.5 点"],
                    "type": "S",
                    "analysis": "服务器待判解析",
                },
            },
        }
        pipeline.ingest(
            {
                "request": {"method": "GET", "url": "https://TARGET/question"},
                "response": {"status_code": 200, "body": question_response},
            }
        )

        answer_response = {
            "success": True,
            "data": {
                "correct": False,
                "analysis": "本题考查合约乘数与报价单位，正确答案为 B。",
                "rightIndex": 1,
            },
        }
        pipeline.ingest(
            {
                "request": {
                    "method": "POST",
                    "url": "https://TARGET/answer",
                    "body": {"questionId": 456, "quizId": 7, "answerIndex": 0},
                },
                "response": {"status_code": 200, "body": answer_response},
            }
        )

        result = export_markdown(store, path=markdown_path)
        assert result["questions"] == 1
        with open(markdown_path, "r", encoding="utf-8") as file:
            document = file.read()

        assert "中金所股指期货最小变动价位是多少" in document
        assert "依据题库第 12 题的解析。" in document
        assert "local_semantic" in document
        assert "0.8700" in document
        assert "标准答案：**B**" in document
        assert "本题考查合约乘数与报价单位" in document
        assert "回答错误" in document
        assert "| A |" in document
        assert "| 回答错误 |" in document
        print(document)
        print("markdown export test passed")


if __name__ == "__main__":
    main()
