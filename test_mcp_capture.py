"""MCP 收题模块的回归测试。"""
# -*- coding: utf-8 -*-
from mcp_capture import entry_to_envelope, is_quiz_answer, is_quiz_question


def main():
    """验证条目转换与题目/答案判定。"""

    print("=== 1. 题目条目应被识别 ===")
    question_entry = {
        "method": "GET",
        "host": "quiz-activity.eastmoney.com",
        "path": "/qa/api/quiz/Mzpvia",
        "status": 200,
        "response": {
            "status": 200,
            "body": {"success": True, "data": {"nextQuestion": {"questionId": 1}}},
        },
        "entry_id": "e1",
    }
    env = entry_to_envelope(question_entry)
    assert env is not None
    assert is_quiz_question(env) is True
    assert is_quiz_answer(env) is False
    assert env["request"]["host"] == "quiz-activity.eastmoney.com"
    assert env["metadata"]["entry_id"] == "e1"
    print("   ok:", env["request"]["method"], env["request"]["path"])

    print("=== 2. 答案提交条目应被识别 ===")
    answer_entry = {
        "method": "POST",
        "host": "quiz-activity.eastmoney.com",
        "path": "/qa/api/quiz/answer",
        "requestBody": {"questionId": 1, "answerIndex": 1},
        "response": {"status": 200, "body": {"data": {"correct": True}}},
    }
    env2 = entry_to_envelope(answer_entry)
    assert env2 is not None
    assert is_quiz_answer(env2) is True
    assert is_quiz_question(env2) is False
    assert env2["request"]["body"]["answerIndex"] == 1
    print("   ok:", env2["request"]["path"], "body=", env2["request"]["body"])

    print("=== 3. 无关域名不应被识别 ===")
    other = {
        "method": "GET",
        "host": "example.com",
        "path": "/api/other",
        "response": {"status": 200, "body": {"a": 1}},
    }
    env3 = entry_to_envelope(other)
    assert env3 is not None
    assert is_quiz_question(env3) is False
    assert is_quiz_answer(env3) is False
    print("   ok: 已排除")

    print("=== 4. 空条目返回 None ===")
    assert entry_to_envelope({}) is None
    print("   ok")

    print("=== 5. 响应体为 JSON 字符串时应被解析 ===")
    str_entry = {
        "method": "GET",
        "host": "quiz-activity.eastmoney.com",
        "path": "/qa/api/quiz/X",
        "response": {"status": 200, "body": '{"data": {"nextQuestion": {"questionId": 2}}}'},
    }
    env4 = entry_to_envelope(str_entry)
    assert isinstance(env4["response"]["body"], dict), env4["response"]["body"]
    assert env4["response"]["body"]["data"]["nextQuestion"]["questionId"] == 2
    print("   ok:", env4["response"]["body"])

    print("mcp capture test passed")


if __name__ == "__main__":
    main()
