"""
监听Charles Mirror保存的文件，自动搜题
"""
# -*- coding: utf-8 -*-
import json
import os
import sys
import time
import logging
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 修复Windows控制台编码问题
if sys.platform == 'win32':
    try:
        import subprocess
        subprocess.run(['chcp', '65001'], shell=True, capture_output=True, check=False)
    except:
        pass
    
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except:
            pass
    if hasattr(sys.stderr, 'reconfigure'):
        try:
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except:
            pass
    
    os.environ['PYTHONIOENCODING'] = 'utf-8'

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# 确保日志处理器使用UTF-8编码
for handler in logger.handlers:
    if hasattr(handler, 'stream') and hasattr(handler.stream, 'reconfigure'):
        try:
            handler.stream.reconfigure(encoding='utf-8', errors='replace')
        except:
            pass

# 配置
MIRROR_FOLDER = os.path.join(os.path.dirname(__file__), '123321')
FLASK_URL = "http://localhost:8889/qa/api/quiz/receive"
FLASK_ANSWER_URL = "http://localhost:8889/qa/api/quiz/answer"

# 已处理的文件集合（避免重复处理）
_processed_files = set()


def extract_question_from_response(response_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从响应数据中提取题目信息"""
    try:
        # 格式：{"success":true,"data":{"nextQuestion":{...}}}
        data = response_data.get("data", {})
        
        # 检查是否有nextQuestion（新题目）
        next_question = data.get("nextQuestion")
        if next_question:
            question_id = next_question.get("questionId")
            question_title = next_question.get("questionTitle", "")
            answers = next_question.get("answers", [])
            
            # 构建题目文本
            question_text = question_title
            if answers:
                option_texts = []
                for i, ans in enumerate(answers):
                    option_texts.append(f"{chr(65+i)}. {ans}")
                question_text += " " + " ".join(option_texts)
            
            return {
                "questionId": question_id,
                "questionTitle": question_title,
                "answers": answers,
                "questionText": question_text,
                "type": next_question.get("type", ""),
                "fullData": next_question  # 保存完整数据用于后续处理
            }
        
        return None
    except Exception as e:
        logger.error(f"提取题目信息失败: {e}")
        return None


def extract_answer_from_response(response_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从答案响应中提取信息"""
    try:
        data = response_data.get("data", {})
        
        # 检查是否有下一题（答案响应后通常会返回下一题）
        next_question = data.get("nextQuestion")
        if next_question:
            question_id = next_question.get("questionId")
            question_title = next_question.get("questionTitle", "")
            answers = next_question.get("answers", [])
            answer_index = next_question.get("index")  # 用户选择的答案索引
            
            question_text = question_title
            if answers:
                option_texts = []
                for i, ans in enumerate(answers):
                    option_texts.append(f"{chr(65+i)}. {ans}")
                question_text += " " + " ".join(option_texts)
            
            return {
                "questionId": question_id,
                "questionTitle": question_title,
                "answers": answers,
                "questionText": question_text,
                "userAnswerIndex": answer_index,
                "correct": data.get("correct"),
                "fullData": next_question
            }
        
        return None
    except Exception as e:
        logger.error(f"提取答案信息失败: {e}")
        return None


def send_to_flask(data: Dict[str, Any], is_answer: bool = False) -> bool:
    """发送数据到Flask服务"""
    try:
        url = FLASK_ANSWER_URL if is_answer else FLASK_URL
        
        response = requests.post(
            url,
            json=data,
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        
        if response.status_code == 200:
            logger.info(f"✓ 已发送到Flask服务")
            return True
        else:
            logger.warning(f"✗ Flask服务返回状态码: {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        logger.error("✗ 无法连接到Flask服务，请确保 proxy_server.py 正在运行")
        return False
    except Exception as e:
        logger.error(f"✗ 发送失败: {e}")
        return False


def convert_to_flask_format(response_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """将Charles保存的格式转换为Flask服务期望的格式"""
    try:
        data = response_data.get("data", {})
        next_question = data.get("nextQuestion")
        
        if not next_question:
            return None
        
        # 转换为Flask期望的格式
        question_id = next_question.get("questionId")
        question_title = next_question.get("questionTitle", "")
        answers = next_question.get("answers", [])
        
        if not question_id or not question_title:
            return None
        
        # 构建Flask期望的格式
        flask_data = {
            "data": {
                "questionId": question_id,
                "question": question_title,
                "options": answers,  # 直接使用answers数组
                "type": next_question.get("type", ""),
                "quizId": data.get("quizId"),
                "total": data.get("total"),
                "timeLimit": data.get("timeLimit")
            }
        }
        
        return flask_data
    except Exception as e:
        logger.error(f"转换数据格式失败: {e}")
        return None


def process_question_file(file_path: str) -> bool:
    """处理题目文件"""
    try:
        # 读取文件内容
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        
        if not content:
            return False
        
        # 解析JSON
        try:
            response_data = json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning(f"文件不是有效的JSON: {file_path}, 错误: {e}")
            return False
        
        # 提取题目信息（用于日志显示）
        question_info = extract_question_from_response(response_data)
        
        if not question_info:
            logger.debug(f"文件中没有题目信息: {file_path}")
            return False
        
        question_id = question_info.get("questionId")
        question_text = question_info.get("questionText", "")
        
        if not question_id or not question_text:
            return False
        
        logger.info("=" * 60)
        logger.info(f"[检测到新题目] 文件: {os.path.basename(file_path)}")
        logger.info(f"题目ID: {question_id}")
        logger.info(f"题目: {question_text[:80]}...")
        logger.info("=" * 60)
        
        # 转换为Flask期望的格式并发送
        flask_data = convert_to_flask_format(response_data)
        if flask_data:
            send_to_flask(flask_data, is_answer=False)
        else:
            logger.warning("无法转换数据格式，尝试发送原始数据")
            send_to_flask(response_data, is_answer=False)
        
        return True
        
    except Exception as e:
        logger.error(f"处理题目文件失败 {file_path}: {e}", exc_info=True)
        return False


def process_answer_file(file_path: str) -> bool:
    """处理答案文件 - 只提取下一题并搜题，不管答案提交"""
    try:
        # 读取文件内容
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        
        if not content:
            return False
        
        # 解析JSON
        try:
            response_data = json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning(f"文件不是有效的JSON: {file_path}, 错误: {e}")
            return False
        
        # 只提取下一题（nextQuestion），不管答案提交
        data = response_data.get("data", {})
        next_question = data.get("nextQuestion")
        
        if not next_question:
            logger.debug(f"答案文件中没有下一题: {file_path}")
            return False
        
        # 提取题目信息
        question_id = next_question.get("questionId")
        question_title = next_question.get("questionTitle", "")
        answers = next_question.get("answers", [])
        
        if not question_id or not question_title:
            return False
        
        # 构建题目文本（用于日志显示）
        question_text = question_title
        if answers:
            option_texts = []
            for i, ans in enumerate(answers):
                option_texts.append(f"{chr(65+i)}. {ans}")
            question_text += " " + " ".join(option_texts)
        
        logger.info("=" * 60)
        logger.info(f"[从答案文件提取题目] 文件: {os.path.basename(file_path)}")
        logger.info(f"题目ID: {question_id}")
        logger.info(f"题目: {question_text[:80]}...")
        logger.info("=" * 60)
        
        # 转换为Flask格式并发送题目数据（只搜题，不发送答案提交）
        flask_data = convert_to_flask_format(response_data)
        if flask_data:
            send_to_flask(flask_data, is_answer=False)
        else:
            logger.warning("无法转换数据格式，尝试发送原始数据")
            send_to_flask(response_data, is_answer=False)
        
        return True
        
    except Exception as e:
        logger.error(f"处理答案文件失败 {file_path}: {e}", exc_info=True)
        return False


def is_question_file(filename: str) -> bool:
    """判断是否是题目文件（不是answer开头的）"""
    # URL解码文件名
    try:
        decoded = urllib.parse.unquote(filename)
        # 如果文件名包含answer，则是答案文件
        if 'answer' in decoded.lower():
            return False
        # 否则是题目文件
        return True
    except:
        return True  # 默认当作题目文件处理


class CharlesMirrorHandler(FileSystemEventHandler):
    """处理Charles Mirror文件变化"""
    
    def __init__(self):
        super().__init__()
        self.last_processed_time = {}
    
    def on_created(self, event):
        """文件创建事件 - 只处理新创建的文件"""
        if event.is_directory:
            return
        
        file_path = event.src_path
        filename = os.path.basename(file_path)
        
        # 检查文件是否在quiz目录下
        if 'quiz' not in file_path.replace('\\', '/'):
            return
        
        # 等待文件写入完成（Charles可能还在写入）
        time.sleep(0.5)
        
        # 检查文件是否存在（可能写入失败）
        if not os.path.exists(file_path):
            return
        
        try:
            mtime = os.path.getmtime(file_path)
            file_key = (file_path, mtime)
            
            # 检查文件是否已处理
            if file_key in _processed_files:
                return
            
            logger.info(f"[新文件创建] {filename}")
            
            # 判断文件类型并处理
            if is_question_file(filename):
                if process_question_file(file_path):
                    _processed_files.add(file_key)
            else:
                if process_answer_file(file_path):
                    _processed_files.add(file_key)
        except Exception as e:
            logger.debug(f"处理新文件时出错 {filename}: {e}")
    
    def on_modified(self, event):
        """文件修改事件（处理文件更新）- 只处理真正新修改的文件"""
        if event.is_directory:
            return
        
        file_path = event.src_path
        filename = os.path.basename(file_path)
        
        # 检查文件是否在quiz目录下
        if 'quiz' not in file_path.replace('\\', '/'):
            return
        
        # 获取文件修改时间
        try:
            mtime = os.path.getmtime(file_path)
            last_time = self.last_processed_time.get(file_path, 0)
            
            # 如果文件最近被修改过，且距离上次处理超过2秒，则处理（避免重复处理）
            if mtime > last_time + 2:
                file_key = (file_path, mtime)
                if file_key not in _processed_files:
                    logger.info(f"[文件更新] {filename}")
                    
                    if is_question_file(filename):
                        if process_question_file(file_path):
                            _processed_files.add(file_key)
                            self.last_processed_time[file_path] = mtime
                    else:
                        if process_answer_file(file_path):
                            _processed_files.add(file_key)
                            self.last_processed_time[file_path] = mtime
        except:
            pass


def process_existing_files(folder_path: str):
    """处理已存在的文件（启动时）- 只处理最新的文件"""
    logger.info("正在扫描已存在的文件（只处理最新的）...")
    
    quiz_folder = os.path.join(folder_path, 'quiz-activity.eastmoney.com', 'qa', 'api', 'quiz')
    
    if not os.path.exists(quiz_folder):
        logger.warning(f"题目文件夹不存在: {quiz_folder}")
        return
    
    # 收集所有文件及其修改时间
    files_with_time = []
    for filename in os.listdir(quiz_folder):
        file_path = os.path.join(quiz_folder, filename)
        if os.path.isfile(file_path):
            try:
                mtime = os.path.getmtime(file_path)
                files_with_time.append((file_path, filename, mtime))
            except:
                continue
    
    if not files_with_time:
        logger.info("没有找到已存在的文件")
        return
    
    # 按修改时间排序，最新的在前
    files_with_time.sort(key=lambda x: x[2], reverse=True)
    
    # 只处理最新的文件（题目文件和答案文件各取最新的1个）
    latest_question_file = None
    latest_answer_file = None
    
    for file_path, filename, mtime in files_with_time:
        if is_question_file(filename):
            if latest_question_file is None:
                latest_question_file = (file_path, filename, mtime)
        else:
            if latest_answer_file is None:
                latest_answer_file = (file_path, filename, mtime)
        
        # 如果已经找到最新的题目和答案文件，可以提前退出
        if latest_question_file and latest_answer_file:
            break
    
    count = 0
    # 处理最新的题目文件
    if latest_question_file:
        file_path, filename, mtime = latest_question_file
        file_key = (file_path, mtime)
        if file_key not in _processed_files:
            logger.info(f"处理最新的题目文件: {filename}")
            if process_question_file(file_path):
                _processed_files.add(file_key)
                count += 1
    
    # 处理最新的答案文件
    if latest_answer_file:
        file_path, filename, mtime = latest_answer_file
        file_key = (file_path, mtime)
        if file_key not in _processed_files:
            logger.info(f"处理最新的答案文件: {filename}")
            if process_answer_file(file_path):
                _processed_files.add(file_key)
                count += 1
    
    if count > 0:
        logger.info(f"已处理 {count} 个最新的文件")
    else:
        logger.info("没有需要处理的新文件")


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("Charles Mirror 文件监听服务")
    logger.info("=" * 60)
    logger.info(f"监听文件夹: {MIRROR_FOLDER}")
    logger.info(f"Flask服务地址: {FLASK_URL}")
    logger.info("=" * 60)
    logger.info("")
    
    # 检查文件夹是否存在
    if not os.path.exists(MIRROR_FOLDER):
        logger.error(f"文件夹不存在: {MIRROR_FOLDER}")
        logger.info("请确保Charles Mirror功能已配置，并保存文件到此文件夹")
        return
    
    # 检查Flask服务是否运行
    try:
        response = requests.get("http://localhost:8889/health", timeout=3)
        if response.status_code == 200:
            logger.info("✓ Flask服务运行正常")
        else:
            logger.warning("⚠ Flask服务可能未就绪，但将继续运行")
    except:
        logger.warning("⚠ 无法连接到Flask服务，请确保 proxy_server.py 正在运行")
        logger.info("监听服务将继续运行，但搜题功能可能无法使用")
    
    logger.info("")
    
    # 处理已存在的文件
    process_existing_files(MIRROR_FOLDER)
    
    logger.info("")
    logger.info("开始监听文件夹变化...")
    logger.info("按 Ctrl+C 停止")
    logger.info("")
    
    # 创建观察者
    event_handler = CharlesMirrorHandler()
    observer = Observer()
    observer.schedule(event_handler, MIRROR_FOLDER, recursive=True)
    observer.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("")
        logger.info("正在停止监听服务...")
        observer.stop()
    
    observer.join()
    logger.info("监听服务已停止")


if __name__ == '__main__':
    main()

