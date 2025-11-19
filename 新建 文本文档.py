import re
import json
import os
from answer_key import ANSWERS_DICT  # 导入答案字典

# --- 配置 ---
SOURCE_FILE = '第十届题库和答案.txt'  # 原始题库文本
OUTPUT_FILE = 'database.json'

# --- 正则表达式 ---
# 匹配题型
RE_SECTION_SINGLE = re.compile(r'单选题')
RE_SECTION_MULTI = re.compile(r'多选题')
RE_SECTION_JUDGE = re.compile(r'判断题')
# 匹配题号和题目开头，例如: 1. xxxx
RE_QUESTION = re.compile(r'^\s*(\d+)\.\s*(.*)')
# 匹配选项，例如: A. xxxx
RE_OPTION = re.compile(r'^\s*([A-Z])\.\s*(.*)')
# 匹配判断题的选项
RE_JUDGE_OPTION_A = re.compile(r'A\.\s*正确')
RE_JUDGE_OPTION_B = re.compile(r'B\.\s*错误')

# 忽略包含这些关键词的行 (例如页眉、页脚、水印)
IGNORE_TEXTS = [
    '微信', '公众号', '中金所杯', '答疑平台', 'The following table', 'PAGE', 'http', 'com'
]


def _clean_line(line: str) -> str:
    """
    归一化行内容，去掉页眉、页脚常见的控制字符。
    """
    return (
        line.replace('\ufeff', '')
        .replace('\x0c', '')  # 处理 PDF 转文本后的分页符
        .strip()
    )


def parse_questions():
    """
    解析原始题库文本，提取所有题目信息。
    """
    if not os.path.exists(SOURCE_FILE):
        print(f"错误: 未找到源文件 '{SOURCE_FILE}'")
        print("请确认题目文本已经导出并放在脚本同目录。")
        return None

    database = []
    current_question = None
    current_type = None
    last_option_key = None
    last_question_number = 0

    print(f"正在从 '{SOURCE_FILE}' 读取源文本...")

    with open(SOURCE_FILE, 'r', encoding='utf-8') as f:
        for raw_line in f:
            text = _clean_line(raw_line)
            if not text:
                continue

            # 检查是否为应忽略的行
            if any(keyword in text for keyword in IGNORE_TEXTS):
                continue

            # --- 1. 检查题型变化 ---
            if RE_SECTION_SINGLE.search(text):
                current_type = 'single'
                print("\n...切换到 [单选题]...")
                last_option_key = None
                continue
            if RE_SECTION_MULTI.search(text):
                current_type = 'multi'
                print("\n...切换到 [多选题]...")
                last_option_key = None
                continue
            if RE_SECTION_JUDGE.search(text):
                current_type = 'judgment'
                print("\n...切换到 [判断题]...")
                last_option_key = None
                continue

            # --- 2. 检查是否为新题目 ---
            question_match = RE_QUESTION.match(text)
            if question_match:
                # 如果有正在处理的题目，先保存
                q_num = question_match.group(1)
                q_text = question_match.group(2)
                q_num_int = int(q_num)

                # 若题号倒退，通常是题干中的小数（如 8.02%），把整行当作补充文本
                if current_question and q_num_int <= last_question_number:
                    pass
                else:
                    if current_question:
                        database.append(current_question)

                    # 针对判断题，选项是固定的
                    options = {}
                    if current_type == 'judgment':
                        options = {"A": "正确", "B": "错误"}

                    current_question = {
                        "number": q_num,
                        "question": q_text,
                        "options": options,
                        "type": current_type or 'unknown',
                        "answer": ANSWERS_DICT.get(q_num, "答案未找到")  # 直接从字典合并答案
                    }
                    last_option_key = None
                    last_question_number = q_num_int
                    print(f"正在解析: 题目 {q_num}")
                    continue

            # --- 3. 检查是否为选项 ---
            option_match = RE_OPTION.match(text)
            if option_match and current_question:
                # 判断题的选项已经预设，跳过
                if current_type == 'judgment':
                    last_option_key = None
                    continue

                opt_key = option_match.group(1)
                opt_val = option_match.group(2)
                current_question["options"][opt_key] = opt_val.strip()
                last_option_key = opt_key
                continue

            # --- 4. 如果不是以上任何一种，则认为是题目的补充说明 ---
            if current_question and text and not RE_JUDGE_OPTION_A.match(text) and not RE_JUDGE_OPTION_B.match(text):
                if last_option_key:
                    current_question["options"][last_option_key] += " " + text
                else:
                    # 过滤掉一些括号 "()"，它们通常是单独一行
                    if text == "()。" or text == "()":
                        current_question["question"] += " ()"  # 合并到题目中
                    else:
                        current_question["question"] += " " + text  # 合并多行题目

    # 添加最后一题
    if current_question:
        database.append(current_question)

    print(f"\n...解析完成！共提取 {len(database)} 道题目。")
    return database


def save_database(database):
    """
    将提取的数据保存为 JSON 文件。
    """
    if not database:
        print("数据库为空，不保存。")
        return

    try:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(database, f, ensure_ascii=False, indent=2)
        print(f"✅ 成功！数据库已保存到 '{OUTPUT_FILE}'")
    except Exception as e:
        print(f"保存失败: {e}")


# --- 主程序 ---
if __name__ == "__main__":
    print("--- 阶段一：数据准备脚本 ---")

    # 1. 检查答案字典是否已填充
    # if len(ANSWERS_DICT) < 1000:
    #     print(f"警告: 'answer_key.py' 中的答案不完整 (仅 {len(ANSWERS_DICT)}/1000)。")
    #     print("请先手动补充完整 'ANSWERS_DICT' 字典。")
        # 即使不完整，也继续运行，方便调试

    # 2. 解析题目
    db_data = parse_questions()

    # 3. 保存
    if db_data:
        save_database(db_data)