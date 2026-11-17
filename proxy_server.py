"""
HTTP代理服务器
监听东方财富答题API，自动搜题并返回答案
"""
# -*- coding: utf-8 -*-
import json
import os
import sys
import logging
import re
import threading
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify, Response, send_file
import requests
from flask_cors import CORS
import config
from capture_pipeline import CapturePipeline
from capture_store import QuizCaptureStore
from markdown_export import default_markdown_path, export_markdown
from progress_tracker import get_progress_tracker
from system_status import collect as collect_status

# 修复Windows控制台编码问题
if sys.platform == 'win32':
    try:
        # 设置控制台代码页为UTF-8
        import subprocess
        subprocess.run(['chcp', '65001'], shell=True, capture_output=True, check=False)
    except:
        pass
    
    # 重新配置标准输出流
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
    
    # 设置环境变量
    os.environ['PYTHONIOENCODING'] = 'utf-8'

# 导入搜题模块；抓包归档模式不应被模型依赖阻塞。
try:
    from semantic_search import (
        load_database,
        build_or_load_embeddings,
        search_questions,
        prepare_model_precision,
        SentenceTransformer,
        DATABASE_PATH,
        MODEL_DIR,
        DEVICE,
        TOP_K_DEFAULT,
    )
    _SEARCH_IMPORT_ERROR = None
except (ModuleNotFoundError, OSError) as exc:
    # 允许只启动 Charles MCP 接收端：缺依赖或原生库加载失败（如页面文件不足
    # 导致 torch 的 WinError 1455）时降级为无向量检索，服务本身仍可启动。
    _SEARCH_IMPORT_ERROR = str(exc)
    SentenceTransformer = Any
    DATABASE_PATH = os.path.join(os.path.dirname(__file__), "database.json")
    MODEL_DIR = ""
    DEVICE = "cpu"
    TOP_K_DEFAULT = 5

    def load_database(*args, **kwargs):
        raise RuntimeError(f"搜题引擎不可用: {_SEARCH_IMPORT_ERROR}")

    def build_or_load_embeddings(*args, **kwargs):
        raise RuntimeError(f"搜题引擎不可用: {_SEARCH_IMPORT_ERROR}")

    def search_questions(*args, **kwargs):
        return []

    def prepare_model_precision(*args, **kwargs):
        return None

# 配置日志
_log_handlers = [logging.StreamHandler(sys.stdout)]

# 落盘日志按大小轮转，避免长期运行把磁盘写满。
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")
try:
    os.makedirs(_log_dir, exist_ok=True)
    from logging.handlers import RotatingFileHandler

    _log_handlers.append(
        RotatingFileHandler(
            os.path.join(_log_dir, "proxy.log"),
            maxBytes=8 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    )
except OSError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=_log_handlers
)
logger = logging.getLogger(__name__)

# 浏览器重试或代理递归时，werkzeug 的访问日志会瞬间刷爆；
# 这里把它压到 WARNING，业务日志仍走 INFO。
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# 确保日志处理器使用UTF-8编码
for handler in logger.handlers:
    if hasattr(handler, 'stream') and hasattr(handler.stream, 'reconfigure'):
        try:
            handler.stream.reconfigure(encoding='utf-8', errors='replace')
        except:
            pass

app = Flask(__name__)
CORS(app)

# 目标API基础URL
TARGET_HOST = "quiz-activity.eastmoney.com"
TARGET_BASE = f"https://{TARGET_HOST}"

# 需要特殊处理的路径（只处理这些路径，其他路径直接转发）
SPECIAL_PATHS = [
    'health',
    'cache',
    'proxy_info'
]

# 全局变量
_model: Optional[SentenceTransformer] = None
_database: Optional[list] = None
_embeddings = None
_meta: Optional[list] = None

# 题目缓存（questionId -> 答案）
_question_cache: Dict[int, Dict[str, Any]] = {}
_capture_store = QuizCaptureStore()
_capture_pipeline: Optional[CapturePipeline] = None
_automation_runner = None
_progress = get_progress_tracker()


def init_search_engine():
    """初始化搜题引擎"""
    global _model, _database, _embeddings, _meta
    
    try:
        logger.info("正在初始化搜题引擎...")
        _database = load_database(DATABASE_PATH)
        logger.info(f"题库加载完成，共 {len(_database)} 道题")
        
        _model = SentenceTransformer(MODEL_DIR, device=DEVICE)
        prepare_model_precision(_model)
        logger.info("模型加载完成")
        
        _embeddings, _meta = build_or_load_embeddings(_database, _model)
        logger.info(f"向量加载完成，共 {len(_meta)} 个向量")
        
        logger.info("搜题引擎初始化完成！")
        return True
    except Exception as e:
        logger.error(f"初始化失败: {e}", exc_info=True)
        return False


def extract_question_text(question_data: Dict[str, Any]) -> str:
    """从API响应中提取题目文本"""
    question_text = question_data.get("question", "").strip()
    
    # 提取选项
    options = question_data.get("options", [])
    if isinstance(options, list):
        option_texts = []
        for i, opt in enumerate(options):
            if isinstance(opt, dict):
                option_texts.append(f"{opt.get('label', chr(65+i))}. {opt.get('content', '')}")
            elif isinstance(opt, str):
                option_texts.append(f"{chr(65+i)}. {opt}")
        if option_texts:
            question_text += " " + " ".join(option_texts)
    elif isinstance(options, dict):
        option_texts = []
        for k, v in sorted(options.items()):
            option_texts.append(f"{k}. {v}")
        if option_texts:
            question_text += " " + " ".join(option_texts)
    
    return question_text


def search_answer(question_text: str, top_k: int = 3) -> Optional[Dict[str, Any]]:
    """搜索题目答案"""
    if _model is None or _database is None or _embeddings is None:
        return None
    
    try:
        results = search_questions(
            query=question_text,
            db=_database,
            embeddings=_embeddings,
            meta=_meta,
            model=_model,
            top_k=top_k
        )
        
        if not results:
            return None
        
        # 返回最佳匹配
        best_match = results[0]
        q, sim = best_match
        
        return {
            "question": q.get("question", ""),
            "answer": q.get("answer", ""),
            "similarity": round(float(sim), 4),
            "all_results": [
                {
                    "number": item[0].get("number"),
                    "question": item[0].get("question", ""),
                    "answer": item[0].get("answer", ""),
                    "options": item[0].get("options", {}),
                    "similarity": round(float(item[1]), 4)
                }
                for item in results[:top_k]
            ]
        }
    except Exception as e:
        logger.error(f"搜题失败: {e}", exc_info=True)
        return None


def find_answer_index(answer_text: str, options: list) -> Optional[int]:
    """根据答案文本找到选项索引（0-based）"""
    if not options:
        return None
    
    # 清理答案文本
    answer_clean = answer_text.strip().upper()
    
    # 尝试直接匹配选项标签（单个字母）
    if len(answer_clean) == 1 and answer_clean in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        idx = ord(answer_clean) - ord('A')
        if 0 <= idx < len(options):
            return idx
    
    # 尝试匹配选项内容
    for i, opt in enumerate(options):
        if isinstance(opt, dict):
            opt_label = str(opt.get("label", "")).strip().upper()
            opt_content = str(opt.get("content", "")).strip()
            # 匹配标签
            if opt_label == answer_clean:
                return i
            # 匹配内容（包含关系）
            if answer_clean and answer_clean in opt_content.upper():
                return i
            # 如果选项内容就是答案文本
            if opt_content.upper() == answer_clean:
                return i
        elif isinstance(opt, str):
            opt_upper = opt.upper()
            if answer_clean in opt_upper or opt_upper == answer_clean:
                return i
    
    # 如果答案是多选（如"ABCD"），返回第一个有效选项
    if len(answer_clean) > 1:
        for char in answer_clean:
            if char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                idx = ord(char) - ord('A')
                if 0 <= idx < len(options):
                    return idx
    
    return None


class _StatusContext:
    """把状态检测需要的运行时对象打包，避免依赖模块全局名。"""

    def __init__(self) -> None:
        self._database = _database
        self._meta = _meta
        self._model = _model
        self._capture_store = _capture_store
        self._SEARCH_IMPORT_ERROR = _SEARCH_IMPORT_ERROR


def build_status_context() -> _StatusContext:
    """构造状态检测上下文，反映当前进程内的真实加载情况。"""
    return _StatusContext()


def get_capture_pipeline() -> CapturePipeline:
    """懒加载抓包管线，避免模块导入阶段触发模型初始化。"""
    global _capture_pipeline
    if _capture_pipeline is None:
        _capture_pipeline = CapturePipeline(
            store=_capture_store,
            answer_resolver=search_answer,
        )
    return _capture_pipeline


def get_automation_runner():
    """懒加载完整自动答题编排器。"""
    global _automation_runner
    if _automation_runner is None:
        from automation_runner import QuizAutomation

        _automation_runner = QuizAutomation(
            store=_capture_store,
            local_resolver=search_answer,
            progress=_progress,
        )
    return _automation_runner


def ingest_capture(payload: Dict[str, Any], source: str) -> Dict[str, Any]:
    """统一保存抓包，并把结果同步到旧版内存缓存。"""
    result = get_capture_pipeline().ingest(payload, source=source)
    question = result.get("question") or {}
    question_id = question.get("question_id")
    if question_id:
        _question_cache[int(question_id) if str(question_id).isdigit() else question_id] = {
            "question_text": question.get("question_text", ""),
            "question_title": question.get("question_title", ""),
            "options": question.get("options", []),
            "answer": question.get("inferred_answer"),
            "answer_index": question.get("inferred_answer_index"),
            "similarity": question.get("similarity"),
            "analysis": question.get("server_analysis"),
            "answer_source": question.get("answer_source"),
            "capture_id": result.get("capture_id"),
            "raw": question.get("raw", {}),
        }
    return result


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    if _model is None:
        return jsonify({"status": "not_ready"}), 503
    return jsonify({
        "status": "ready",
        "cache_size": len(_question_cache),
        "capture_store": _capture_store.stats(),
        "progress": _progress.snapshot()["state"],
    })


@app.route('/qa/api/quiz/<path:quiz_id>', methods=['GET'])
def receive_quiz_only(quiz_id: str):
    """只接收题目数据包，不发送响应（镜像模式）"""
    try:
        # 获取请求数据
        query_params = request.query_string.decode('utf-8')
        logger.info(f"[接收题目] 题目ID: {quiz_id}, 参数: {query_params}")
        logger.info(f"[接收题目] 请求来源: {request.remote_addr}")
        
        # 尝试从请求体或查询参数中获取题目数据
        # 如果Charles转发的是响应数据，可能在请求体中
        question_data = None
        
        # 方法1: 从请求体获取（如果Charles转发响应）
        if request.data:
            try:
                question_data = request.get_json()
                logger.info(f"[接收题目] 从请求体获取数据")
            except:
                pass
        
        # 方法2: 如果没有数据，说明这只是通知，需要从真实服务器获取
        # 但用户说不需要Flask发送，所以这里只记录
        if not question_data:
            logger.info(f"[接收题目] 收到题目请求通知，等待Charles转发响应数据...")
            # 返回简单确认，不阻塞
            return jsonify({"status": "received", "message": "题目请求已接收，等待响应数据"}), 200
        
        # 如果有题目数据，进行搜题
        if isinstance(question_data, dict):
            data = question_data.get("data", question_data)
            question_id = data.get("questionId") or data.get("id")
            question_text = extract_question_text(data)
            # 保留原始题目响应，后续可在 SQLite 中查看完整选项和服务器字段。
            try:
                ingest_capture(
                    {
                        "request": {
                            "method": request.method,
                            "path": request.path,
                            "query": query_params,
                            "headers": dict(request.headers),
                        },
                        "response": {"status_code": 200, "body": question_data},
                    },
                    source="legacy_receive_question",
                )
            except Exception:
                logger.exception("[抓包持久化] 题目保存失败")
            
            if question_text and question_id:
                logger.info(f"[接收题目] 检测到题目 ID={question_id}, 文本: {question_text[:50]}...")
                
                # 使用线程异步搜题
                def search_in_background():
                    try:
                        answer_info = search_answer(question_text)
                        if answer_info:
                            logger.info(f"[搜题结果] 找到答案: {answer_info['answer']}, 相似度: {answer_info['similarity']}")
                            
                            # 缓存答案
                            _question_cache[question_id] = {
                                "question_text": question_text,
                                "answer": answer_info["answer"],
                                "similarity": answer_info["similarity"],
                                "all_results": answer_info.get("all_results", [])
                            }
                            
                            # 尝试找到选项索引
                            options = data.get("options", [])
                            answer_index = find_answer_index(answer_info["answer"], options)
                            
                            if answer_index is not None:
                                logger.info(f"[搜题结果] 答案索引: {answer_index} (选项: {chr(65+answer_index)})")
                                _question_cache[question_id]["answer_index"] = answer_index
                            
                            # 在控制台输出答案信息
                            logger.info("")
                            logger.info("=" * 80)
                            logger.info(f"题目ID: {question_id}")
                            logger.info("-" * 80)
                            logger.info(f"题目: {question_text}")
                            logger.info("")
                            logger.info("【最匹配的3个结果】")
                            logger.info("-" * 80)
                            all_results = answer_info.get("all_results", [])
                            for i, result in enumerate(all_results[:3], 1):
                                question_text_result = result.get('question', '')
                                answer_text = result.get('answer', 'N/A')
                                similarity = result.get('similarity', 0)
                                number = result.get('number', 'N/A')
                                options = result.get('options', {})
                                
                                logger.info(f"结果 {i}: 题号 {number} | 相似度: {similarity:.4f}")
                                logger.info(f"  题目: {question_text_result}")
                                if options:
                                    logger.info("  选项:")
                                    for opt_key in sorted(options.keys()):
                                        logger.info(f"    {opt_key}. {options[opt_key]}")
                                logger.info(f"  答案: {answer_text}")
                                if i < len(all_results[:3]):
                                    logger.info("-" * 80)
                            
                            logger.info("")
                            logger.info("【推荐答案】")
                            logger.info(f"  答案: {answer_info['answer']}")
                            logger.info(f"  相似度: {answer_info['similarity']:.4f}")
                            if answer_index is not None:
                                logger.info(f"  选项索引: {answer_index} ({chr(65+answer_index)})")
                            logger.info("=" * 80)
                            logger.info("")
                        else:
                            logger.warning(f"[搜题结果] 未找到答案: {question_id}")
                    except Exception as e:
                        logger.error(f"[搜题错误] {e}", exc_info=True)
                
                # 启动后台线程搜题
                thread = threading.Thread(target=search_in_background, daemon=True)
                thread.start()
        
        # 返回简单确认（不返回实际数据，因为Charles会处理）
        return jsonify({"status": "received", "message": "题目数据已接收，正在搜题"}), 200
        
    except Exception as e:
        logger.error(f"[接收题目错误] {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 200  # 即使出错也返回200，不影响Charles


# 同一路径的代理错误只记录一次，避免浏览器重试把日志刷爆
_proxy_error_seen: dict = {}


def _log_proxy_error_once(path: str, exc: Exception, exc_info: bool = False) -> None:
    """同一路径的同类错误只记一条，之后静默累计。"""
    key = (path, type(exc).__name__)
    count = _proxy_error_seen.get(key, 0) + 1
    _proxy_error_seen[key] = count
    if count == 1:
        logger.error("代理请求失败 (%s): %s", path, exc, exc_info=exc_info)
    elif count in (10, 100, 1000, 10000):
        logger.error("代理请求失败 (%s) 已累计 %d 次，后续不再记录", path, count)


def _is_self_request(host_header: str) -> bool:
    """判断请求是否是打到本服务自身。

    系统代理指向 Charles 时，浏览器访问本机面板的请求会经 Charles 转回来，
    Host 仍是本机的 IP:端口。此时若继续当代理目标转发，就会无限递归。
    """
    if not host_header:
        return False
    host = host_header.split(":")[0].strip().lower()
    if not host:
        return False

    port = config.get_int("PROXY_PORT", 8889)
    # Host 带端口且不是本服务端口时，说明是别的主机/服务
    if ":" in host_header:
        try:
            if int(host_header.split(":")[-1]) != port:
                return False
        except ValueError:
            pass

    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"):
        return True

    # 本机各网卡地址
    try:
        import socket

        local_names = {socket.gethostname().lower()}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                local_names.add(str(info[4][0]).lower())
        except OSError:
            pass
        # 通过 UDP 探测本机出站 IP
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            local_names.add(probe.getsockname()[0].lower())
        except OSError:
            pass
        finally:
            probe.close()
        if host in local_names:
            return True
    except OSError:
        pass

    return False


def _not_proxied_response(path: str):
    """对打到自己身上的请求给出明确提示，而不是转发造成递归。"""
    if path.endswith("favicon.ico"):
        # 浏览器自动请求，直接 204 避免噪声
        return "", 204
    return (
        jsonify(
            {
                "error": "refuse to proxy to self",
                "message": "该请求的目标是本服务自身，已拒绝转发以避免无限递归。",
                "hint": "把本机地址（如 192.168.x.x）加入代理例外，或在 Charles 中排除该域名。",
                "path": path,
            }
        ),
        508,
    )


# 注意：通配符路由必须放在最后，确保特定路由优先匹配
@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'CONNECT'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'CONNECT'])
def proxy_all(path):
    """代理所有其他请求（转发到原始服务器）"""
    try:
        # 处理CONNECT请求（HTTPS隧道）
        if request.method == 'CONNECT':
            logger.warning("=" * 60)
            logger.warning("收到CONNECT请求（HTTPS隧道）")
            logger.warning("Flask无法直接处理CONNECT请求")
            logger.warning("")
            logger.warning("解决方案：")
            logger.warning("1. 使用Charles/Fiddler处理CONNECT请求")
            logger.warning("2. 在Charles中配置Map Remote，将请求转发到此服务")
            logger.warning("3. 手机配置代理指向Charles（不是直接指向此服务）")
            logger.warning("=" * 60)
            return jsonify({
                "error": "CONNECT method not supported",
                "message": "此服务不支持CONNECT请求。请使用Charles/Fiddler处理HTTPS，然后转发HTTP请求到此服务。",
                "solution": "配置Charles Map Remote: https://quiz-activity.eastmoney.com/qa/api/quiz/* -> http://localhost:8889/qa/api/quiz/*"
            }), 501
        
        # 检查是否是特殊路径（已由其他路由处理）
        for special in SPECIAL_PATHS:
            if path.startswith(special.strip('/')):
                # 让Flask继续处理其他路由
                from flask import abort
                abort(404)
        
        # 判断目标服务器
        host_header = request.headers.get('Host', '')

        # 防自转发：系统代理指向 Charles 时，访问本服务的请求会被转回自己，
        # 通配路由再把它当目标转发，形成无限递归。命中自身地址时直接拒绝。
        if self_host := _is_self_request(host_header):
            logger.warning(
                "[防递归] 拒绝转发到本服务自身: Host=%s path=%s。"
                "访问面板请把本机地址加入代理例外（不走 Charles）。",
                host_header,
                request.path,
            )
            return _not_proxied_response(request.path)

        # 如果Host是quiz-activity.eastmoney.com，转发到HTTPS
        if 'quiz-activity.eastmoney.com' in host_header:
            target_base = f"https://quiz-activity.eastmoney.com"
        elif host_header:
            # 对于其他请求，默认使用HTTP（因为通过HTTP代理访问）
            # 如果原始请求是HTTPS，可能需要特殊处理
            target_base = f"http://{host_header}"
        else:
            # 如果没有Host头，尝试从请求URL推断
            if 'quiz-activity.eastmoney.com' in request.url:
                target_base = f"https://quiz-activity.eastmoney.com"
            else:
                # 无法确定目标，返回错误
                logger.warning(f"无法确定目标服务器: Host={host_header}, URL={request.url}")
                return jsonify({"error": "Cannot determine target server"}), 400
        
        # 构建目标URL
        query_params = request.query_string.decode('utf-8')
        if path:
            target_url = f"{target_base}/{path}"
        else:
            target_url = target_base
        if query_params:
            target_url += f"?{query_params}"
        
        logger.debug(f"转发请求: {request.method} {target_url}")
        
        # 复制请求头
        headers = {}
        for key, value in request.headers:
            key_lower = key.lower()
            if key_lower not in ['host', 'content-length', 'connection', 'proxy-connection']:
                headers[key] = value
        
        # 根据请求方法转发
        try:
            if request.method == 'GET':
                response = requests.get(target_url, headers=headers, timeout=10, verify=False, allow_redirects=True)
            elif request.method == 'POST':
                data = request.get_data()
                content_type = request.headers.get('Content-Type', '')
                if 'application/json' in content_type:
                    import json as json_lib
                    try:
                        json_data = json_lib.loads(data)
                        response = requests.post(target_url, json=json_data, headers=headers, timeout=10, verify=False)
                    except:
                        response = requests.post(target_url, data=data, headers=headers, timeout=10, verify=False)
                else:
                    response = requests.post(target_url, data=data, headers=headers, timeout=10, verify=False)
            elif request.method == 'PUT':
                data = request.get_data()
                response = requests.put(target_url, data=data, headers=headers, timeout=10, verify=False)
            elif request.method == 'DELETE':
                response = requests.delete(target_url, headers=headers, timeout=10, verify=False)
            elif request.method == 'PATCH':
                data = request.get_data()
                response = requests.patch(target_url, data=data, headers=headers, timeout=10, verify=False)
            elif request.method == 'OPTIONS':
                response = requests.options(target_url, headers=headers, timeout=10, verify=False)
            else:
                return jsonify({"error": "Method not allowed"}), 405
        except requests.exceptions.SSLError as e:
            logger.error(f"SSL错误: {e}，尝试使用HTTP")
            # 如果HTTPS失败，尝试HTTP
            if target_url.startswith('https://'):
                target_url = target_url.replace('https://', 'http://')
                response = requests.get(target_url, headers=headers, timeout=10, allow_redirects=True)
            else:
                raise
        
        # 返回响应
        return Response(
            response.content,
            status=response.status_code,
            headers=dict(response.headers),
            mimetype=response.headers.get('Content-Type', 'application/json')
        )
        
    except requests.exceptions.RequestException as e:
        _log_proxy_error_once(path, e)
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        _log_proxy_error_once(path, e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/qa/api/quiz/answer', methods=['POST'])
def receive_answer_only():
    """只接收答题数据包，不发送响应（镜像模式）"""
    try:
        # 获取请求数据
        request_data = request.get_json() or {}
        question_id = request_data.get("questionId")
        answer_index = request_data.get("answerIndex")
        quiz_id = request_data.get("quizId")
        
        logger.info(f"[接收答案] questionId={question_id}, answerIndex={answer_index}, quizId={quiz_id}")
        logger.info(f"[接收答案] 请求来源: {request.remote_addr}")
        try:
            # 镜像模式只能看到请求，先记录提交；服务器响应由 Charles MCP 事件补齐。
            ingest_capture(
                {
                    "request": {
                        "method": request.method,
                        "path": request.path,
                        "headers": dict(request.headers),
                        "body": request_data,
                    },
                    "response": {"status_code": 200, "body": {}},
                },
                source="legacy_answer_request",
            )
        except Exception:
            logger.exception("[抓包持久化] 答案提交保存失败")
        
        # 检查缓存中是否有答案（仅用于日志显示）
        if question_id and question_id in _question_cache:
            cache_info = _question_cache[question_id]
            correct_answer = cache_info.get('answer', 'N/A')
            similarity = cache_info.get('similarity', 0)
            logger.info("=" * 60)
            logger.info(f"[答案对比] 题目ID: {question_id}")
            logger.info(f"[答案对比] 用户选择: {answer_index} ({chr(65+int(answer_index)) if answer_index and answer_index.isdigit() and 0 <= int(answer_index) < 26 else 'N/A'})")
            logger.info(f"[答案对比] 正确答案: {correct_answer}")
            logger.info(f"[答案对比] 相似度: {similarity}")
            if cache_info.get('answer_index') is not None:
                logger.info(f"[答案对比] 正确答案索引: {cache_info['answer_index']} ({chr(65+cache_info['answer_index'])})")
            logger.info("=" * 60)
        else:
            logger.info(f"[接收答案] 未找到该题目的缓存答案")
        
        # 只返回确认，不发送实际响应（Charles会处理）
        return jsonify({"status": "received", "message": "答案提交已接收"}), 200
        
    except Exception as e:
        logger.error(f"[接收答案错误] {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 200  # 即使出错也返回200


@app.route('/cache', methods=['GET'])
def get_cache():
    """查看题目缓存"""
    return jsonify({
        "count": len(_question_cache),
        "cache": _question_cache
    })


@app.route('/mcp/charles/capture', methods=['POST'])
def receive_charles_mcp_capture():
    """接收外部推送的标准抓包信封。"""
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "请求体必须是 JSON 对象"}), 400
        # 默认开启完整编排；提交通道一律直连目标 API。
        if config.get_int("AUTO_SUBMIT", 1) == 1:
            result = get_automation_runner().handle_capture(payload)
        else:
            result = ingest_capture(payload, source="charles_mcp")
        return jsonify({"success": True, **result})
    except Exception as exc:
        logger.error("[Charles MCP] 抓包处理失败: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/mcp/capture/start', methods=['POST'])
def mcp_capture_start():
    """启动 MCP 收题：轮询 Charles 抓包并自动处理题目。"""
    try:
        from mcp_capture import get_poller

        poller = get_poller(handler=_handle_mcp_envelope)
        return jsonify({"success": True, **poller.start()})
    except Exception as exc:
        logger.error("[收题] 启动失败: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/mcp/capture/stop', methods=['POST'])
def mcp_capture_stop():
    """停止 MCP 收题。"""
    try:
        from mcp_capture import get_poller

        return jsonify({"success": True, **get_poller().stop()})
    except Exception as exc:
        logger.error("[收题] 停止失败: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/mcp/capture/status', methods=['GET'])
def mcp_capture_status():
    """查看 MCP 收题当前状态。"""
    from mcp_capture import get_poller

    poller = get_poller()
    return jsonify(
        {
            "success": True,
            "running": poller.running,
            "capture_id": poller.capture_id,
            "interval": poller.interval,
            "min_interval": poller.MIN_INTERVAL,
            "max_interval": poller.MAX_INTERVAL,
            "stats": dict(poller.stats),
        }
    )


@app.route('/mcp/capture/interval', methods=['POST'])
def mcp_capture_set_interval():
    """运行时调整轮询间隔（秒），立即生效且无需重启。"""
    from mcp_capture import get_poller

    payload = request.get_json(silent=True) or {}
    raw = payload.get("interval", payload.get("seconds"))
    if raw is None:
        return jsonify({"success": False, "error": "缺少 interval 参数"}), 400
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": f"interval 不是数字: {raw}"}), 400

    result = get_poller().set_interval(value)
    return jsonify({"success": True, **result})


def _handle_mcp_envelope(envelope):
    """把 MCP 收到的信封交给自动答题编排。"""
    if config.get_int("AUTO_SUBMIT", 1) == 1:
        return get_automation_runner().handle_capture(envelope)
    return ingest_capture(envelope, source="charles_mcp_poll")


@app.route('/captures', methods=['GET'])
def list_captures():
    """读取最近抓包元数据。"""
    limit = request.args.get("limit", default=100, type=int)
    return jsonify({"success": True, "items": _capture_store.list_captures(limit)})


@app.route('/favicon.ico', methods=['GET'])
def favicon():
    """浏览器自动请求的图标，返回空内容避免落到通配代理路由。"""
    return "", 204


@app.route('/system/status', methods=['GET'])
def system_status_api():
    """返回题库、配套文件、抓包工具与可答题状态。"""
    try:
        return jsonify({"success": True, **collect_status(build_status_context())})
    except Exception as exc:
        logger.error("[状态检测] 失败: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/progress', methods=['GET'])
def progress_status():
    """返回自动化当前状态和最近事件。"""
    return jsonify({"success": True, **_progress.snapshot()})


@app.route('/progress/stream', methods=['GET'])
def progress_stream():
    """通过 SSE 推送自动化进度。"""
    subscriber = _progress.subscribe()

    def event_stream():
        """把进度事件编码为 SSE 数据帧。"""
        for event in _progress.stream(subscriber):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route('/dashboard', methods=['GET'])
def progress_dashboard():
    """提供浏览器进度仪表盘。"""
    path = os.path.join(os.path.dirname(__file__), "progress_dashboard.html")
    return send_file(path, mimetype="text/html")


@app.route('/questions', methods=['GET'])
def list_questions():
    """分页读取题目、答案、相似度和服务器解析。"""
    limit = request.args.get("limit", default=100, type=int)
    offset = request.args.get("offset", default=0, type=int)
    return jsonify({
        "success": True,
        "items": _capture_store.list_questions(limit=limit, offset=offset),
        "stats": _capture_store.stats(),
    })


@app.route('/questions/<question_id>', methods=['GET'])
def get_question(question_id: str):
    """读取单道题的完整归档。"""
    item = _capture_store.get_question(question_id)
    if item is None:
        return jsonify({"success": False, "error": "题目不存在"}), 404
    return jsonify({"success": True, "item": item})


@app.route('/answers.md', methods=['GET'])
def answers_markdown():
    """重新导出并返回答题记录 Markdown。"""
    try:
        result = export_markdown(_capture_store)
    except OSError as exc:
        logger.error("[答题记录] 导出失败: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500
    return send_file(result["path"], mimetype="text/markdown")


@app.route('/answers.md/export', methods=['POST'])
def trigger_answers_export():
    """手动触发答题记录导出。"""
    try:
        return jsonify({"success": True, **export_markdown(_capture_store)})
    except OSError as exc:
        logger.error("[答题记录] 导出失败: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/automation/next', methods=['GET'])
def automation_next():
    """返回最近一道已解析题目，供答题端按需消费。"""
    items = _capture_store.list_questions(limit=1)
    if not items:
        return jsonify({"success": False, "error": "尚未捕获题目"}), 404
    item = items[0]
    return jsonify({
        "success": True,
        "question": item,
        "ready": bool(item.get("answer")),
    })


@app.route('/automation/submission', methods=['POST'])
def automation_submission():
    """记录答题端提交结果，便于离线回放和准确率统计。"""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "请求体必须是 JSON 对象"}), 400
    try:
        result = ingest_capture(
            {
                "request": {
                    "method": "POST",
                    "path": "/qa/api/quiz/answer",
                    "body": payload,
                },
                "response": {
                    "status_code": payload.get("status_code", 200),
                    "body": payload.get("server_response", {}),
                },
            },
            source="automation_client",
        )
        return jsonify({"success": True, **result})
    except Exception as exc:
        logger.error("[自动化记录] 失败: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/cache/clear', methods=['POST'])
def clear_cache():
    """清空缓存"""
    _question_cache.clear()
    return jsonify({"message": "缓存已清空"})


@app.route('/qa/api/quiz/receive', methods=['POST'])
def receive_quiz_response():
    """接收题目响应数据（从Charles Rewrite规则调用）"""
    try:
        # 接收Charles转发的响应数据
        data = request.get_json() or {}
        logger.info(f"[接收响应] 收到题目响应数据")
        try:
            ingest_capture(
                {
                    "request": {
                        "method": request.method,
                        "path": request.path,
                        "headers": dict(request.headers),
                    },
                    "response": {"status_code": 200, "body": data},
                },
                source="legacy_charles_rewrite",
            )
        except Exception:
            logger.exception("[抓包持久化] Charles 响应保存失败")
        
        # 提取题目信息
        if isinstance(data, dict):
            question_data = data.get("data", data)
            question_id = question_data.get("questionId") or question_data.get("id")
            question_text = extract_question_text(question_data)
            
            if question_text and question_id:
                logger.info(f"[接收响应] 题目 ID={question_id}, 文本: {question_text[:50]}...")
                
                # 使用线程异步搜题
                def search_in_background():
                    try:
                        answer_info = search_answer(question_text)
                        if answer_info:
                            logger.info(f"[搜题结果] 找到答案: {answer_info['answer']}, 相似度: {answer_info['similarity']}")
                            
                            # 缓存答案
                            _question_cache[question_id] = {
                                "question_text": question_text,
                                "answer": answer_info["answer"],
                                "similarity": answer_info["similarity"],
                                "all_results": answer_info.get("all_results", [])
                            }
                            
                            # 尝试找到选项索引
                            options = question_data.get("options", [])
                            answer_index = find_answer_index(answer_info["answer"], options)
                            
                            if answer_index is not None:
                                logger.info(f"[搜题结果] 答案索引: {answer_index} (选项: {chr(65+answer_index)})")
                                _question_cache[question_id]["answer_index"] = answer_index
                            
                            # 在控制台输出答案信息
                            logger.info("")
                            logger.info("=" * 80)
                            logger.info(f"题目ID: {question_id}")
                            logger.info("-" * 80)
                            logger.info(f"题目: {question_text}")
                            logger.info("")
                            logger.info("【最匹配的3个结果】")
                            logger.info("-" * 80)
                            all_results = answer_info.get("all_results", [])
                            for i, result in enumerate(all_results[:3], 1):
                                question_text_result = result.get('question', '')
                                answer_text = result.get('answer', 'N/A')
                                similarity = result.get('similarity', 0)
                                number = result.get('number', 'N/A')
                                options = result.get('options', {})
                                
                                logger.info(f"结果 {i}: 题号 {number} | 相似度: {similarity:.4f}")
                                logger.info(f"  题目: {question_text_result}")
                                if options:
                                    logger.info("  选项:")
                                    for opt_key in sorted(options.keys()):
                                        logger.info(f"    {opt_key}. {options[opt_key]}")
                                logger.info(f"  答案: {answer_text}")
                                if i < len(all_results[:3]):
                                    logger.info("-" * 80)
                            
                            logger.info("")
                            logger.info("【推荐答案】")
                            logger.info(f"  答案: {answer_info['answer']}")
                            logger.info(f"  相似度: {answer_info['similarity']:.4f}")
                            if answer_index is not None:
                                logger.info(f"  选项索引: {answer_index} ({chr(65+answer_index)})")
                            logger.info("=" * 80)
                            logger.info("")
                        else:
                            logger.warning(f"[搜题结果] 未找到答案: {question_id}")
                    except Exception as e:
                        logger.error(f"[搜题错误] {e}", exc_info=True)
                
                # 启动后台线程搜题
                thread = threading.Thread(target=search_in_background, daemon=True)
                thread.start()
        
        return jsonify({"status": "received"}), 200
        
    except Exception as e:
        logger.error(f"[接收响应错误] {e}", exc_info=True)
        return jsonify({"status": "error"}), 200


@app.route('/proxy_info', methods=['GET'])
def proxy_info():
    """服务信息"""
    return jsonify({
        "name": "东方财富答题镜像服务",
        "version": "2.0.0",
        "mode": "mirror",
        "status": "ready" if _model else "not_ready",
        "description": "只接收数据包，不发送响应（镜像模式）",
        "endpoints": {
            "GET /qa/api/quiz/<id>": "接收题目请求（镜像）",
            "POST /qa/api/quiz/receive": "接收题目响应数据（从Charles Rewrite调用）",
            "POST /qa/api/quiz/answer": "接收答案提交（镜像）",
            "GET /cache": "查看题目缓存",
            "POST /cache/clear": "清空缓存",
            "GET /health": "健康检查",
            "GET /proxy_info": "服务信息",
            "POST /mcp/charles/capture": "接收 Charles MCP 抓包",
            "GET /captures": "查看抓包索引",
            "GET /progress": "查看当前自动化进度",
            "GET /progress/stream": "订阅实时进度 SSE",
            "GET /dashboard": "打开浏览器进度仪表盘",
            "GET /questions": "查看题目、答案、解析归档",
            "GET /answers.md": "下载答题记录 Markdown",
            "POST /answers.md/export": "手动刷新答题记录 Markdown",
            "GET /automation/next": "读取下一道已解析题目",
            "POST /automation/submission": "记录答题端提交结果",
        },
        "note": "此服务只接收数据，不参与实际的请求-响应流程。Charles会正常转发所有请求。"
    })


if __name__ == '__main__':
    # 初始化搜题引擎
    if not init_search_engine():
        logger.error("搜题引擎初始化失败，程序退出")
        exit(1)
    
    # 启动服务器
    host = config.get("PROXY_HOST", "0.0.0.0")
    port = config.get_int("PROXY_PORT", 8889)
    
    logger.info("=" * 60)
    logger.info("东方财富答题镜像服务启动")
    logger.info(f"监听地址: http://{host}:{port}")
    logger.info("=" * 60)
    logger.info("📋 镜像模式 - 只接收数据，不发送响应")
    logger.info("=" * 60)
    logger.info("")
    logger.info("⚠️  重要：不要配置Charles Map Remote！")
    logger.info("   让Charles正常转发所有请求到真实服务器")
    logger.info("")
    logger.info("配置方法（推荐手动方式）:")
    logger.info("")
    logger.info("1. 在Charles中查看题目响应")
    logger.info("   - 找到 quiz-activity.eastmoney.com/qa/api/quiz/* 的响应")
    logger.info("   - 复制响应JSON数据")
    logger.info("")
    logger.info("2. 发送到Flask服务:")
    logger.info("   curl -X POST http://localhost:8889/qa/api/quiz/receive \\")
    logger.info("     -H 'Content-Type: application/json' \\")
    logger.info("     -d '{\"data\": {粘贴响应数据}}'")
    logger.info("")
    logger.info("3. 或者使用Python脚本自动发送（见文档）")
    logger.info("")
    logger.info("服务接口:")
    logger.info("   POST /qa/api/quiz/receive - 接收题目响应（搜题）")
    logger.info("   POST /qa/api/quiz/answer - 接收答案提交（对比）")
    logger.info("   GET /cache - 查看缓存")
    logger.info("")
    logger.info("详细配置请查看: Charles镜像配置步骤.md")
    logger.info("=" * 60)
    
    app.run(host=host, port=port, debug=False, threaded=True)

