"""双来源答案的回归测试。"""
# -*- coding: utf-8 -*-
import json
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

    print("=== 4. 改写语序且选项相同的题要匹配上题库 ===")
    eng = HybridAnswerEngine(database_path="database.json")
    live = (
        "成立于1985年的（　　），主要致力于促进场外衍生品市场的高效、稳健的发展，"
        "建立了强大的金融监管框架，目的在于降低交易对手信用风险，增加市场透明度。"
    )
    opts = ["国际掉期与衍生品协会", "芝加哥商业交易所", "国际清算银行", "经济合作与发展组织"]
    text = live + " " + " ".join(f"{chr(65 + i)}. {opt}" for i, opt in enumerate(opts))
    hit = eng._resolve_bank(text, opts)
    assert hit and hit["answer"] == "A", hit
    assert hit["answer_source"] == "local_semantic", hit["answer_source"]
    different = "以下关于债券久期的说法，正确的是（　　）。"
    different_opts = [
        "久期是管理债券组合风险的重要工具",
        "修正久期越大，债券价格对利率变化越不敏感",
        "久期是市场收益率变化1bp引起的债券价格变动幅度",
        "收益率变化较大时利用修正久期计算的价格变动有较大误差",
    ]
    different_text = different + " " + " ".join(
        f"{chr(65 + i)}. {opt}" for i, opt in enumerate(different_opts)
    )
    low = eng._resolve_bank(different_text, different_opts)
    assert low and low["answer_source"] == "local_semantic_low", low
    print(f"   同题={hit['answer']}  选项不同仅参考={low['matched_number']}")

    print("=== 5. 同题干不同选项必须按选项区分答案 ===")
    eng = HybridAnswerEngine(database_path="database.json")

    def with_options(number):
        item = next(q for q in json.load(open("database.json", encoding="utf-8")) if str(q["number"]) == number)
        opts = " ".join(f"{k}. {v}" for k, v in item["options"].items())
        return f"{item['question']} {opts}", item["answer"]

    text_856, ans_856 = with_options("856")
    text_890, ans_890 = with_options("890")
    hit_856 = eng._pick_exact(text_856)
    hit_890 = eng._pick_exact(text_890)
    assert hit_856 and hit_856["answer"] == ans_856 == "C", hit_856
    assert hit_890 and hit_890["answer"] == ans_890 == "B", hit_890
    assert eng._pick_exact("关于结构化产品说法正确的是（ ）。") is None
    print(f"   856→{hit_856['answer']}  890→{hit_890['answer']}  无选项不猜")

    print("=== 5. 题干一字之差不能当成同一题 ===")
    eng = HybridAnswerEngine(database_path="database.json")
    same = "中证1000股指期货合约的合约乘数在仿真交易中是多少"
    other = "中证1000股指期权合约的合约乘数在仿真交易中是多少"
    assert eng._titles_close(same, same) is True
    assert eng._titles_close(same, other) is False
    assert eng._titles_close(same, same + " A. 200点 B. 300点") is True
    print("   期货/期权长题干已分开")

    print("=== 6. 真实引擎的 resolve_both 结构 ===")
    out = eng.resolve_both("完全不存在的题目内容 A. 甲 B. 乙", ["甲", "乙"])
    assert "bank" in out and "llm" in out and "agree" in out
    print(f"   bank={out['bank']}  llm有值={out['llm'] is not None}  agree={out['agree']}")

    print("=== 7. 匹配到的题库原题与选项要完整入库 ===")
    with tempfile.TemporaryDirectory() as folder:
        store = QuizCaptureStore(os.path.join(folder, "c.db"))
        engine = FakeEngine(
            {
                "answer": "ABD",
                "answer_index": None,  # 多选题没有单一索引
                "answer_source": "local_exact",
                "similarity": 1.0,
                "matched_question": "题库里的原题",
                "matched_number": "11",
                "matched_options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
            },
            {"answer": "D", "answer_index": 3, "similarity": 0.7},
        )
        pipeline = CapturePipeline(store, engine.resolve_both, os.path.join(folder, "a.md"))
        pipeline.ingest(
            {
                "request": {"method": "GET", "url": "u", "path": "/qa/api/quiz/X",
                            "host": "quiz-activity.eastmoney.com"},
                "response": {"status_code": 200, "body": {"data": {"nextQuestion": {
                    "questionId": 5, "questionTitle": "多选题",
                    "answers": ["甲", "乙", "丙", "丁"], "type": "M"}}}},
            }
        )
        back = store.get_question("5")
        assert back["bank_matched_question"] == "题库里的原题", back.get("bank_matched_question")
        assert back["bank_matched_number"] == "11"
        assert back["bank_matched_options"] == {"A": "甲", "B": "乙", "C": "丙", "D": "丁"}
        assert back["bank_answer"] == "ABD"
        assert back["llm_answer"] == "D"
        export_markdown(store, path=os.path.join(folder, "a.md"))
        doc = open(os.path.join(folder, "a.md"), encoding="utf-8").read()
        assert "| 题库 | **ABD** |" in doc, doc
        assert "| 大模型 | **D** |" in doc, doc
        print(f"   匹配题={back['bank_matched_question']} 题号={back['bank_matched_number']}")
        print(f"   匹配选项={back['bank_matched_options']}")
        print(f"   题库答案={back['bank_answer']} 大模型={back['llm_answer']}")

    print("multi-source test passed")

if __name__ == "__main__":
    main()
