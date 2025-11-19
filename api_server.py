"""
搜题API服务器
监听HTTP请求，自动解析题目并返回搜索结果
"""
import json
import os
import logging
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify
from flask_cors import CORS

# 导入搜题模块
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

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # 允许跨域请求

# 全局变量存储模型和向量
_model: Optional[SentenceTransformer] = None
_database: Optional[list] = None
_embeddings = None
_meta: Optional[list] = None


def init_search_engine():
    """初始化搜题引擎（加载模型和向量）"""
    global _model, _database, _embeddings, _meta
    
    try:
        logger.info("正在初始化搜题引擎...")
        
        # 1. 加载题库
        logger.info(f"加载题库: {DATABASE_PATH}")
        _database = load_database(DATABASE_PATH)
        logger.info(f"题库加载完成，共 {len(_database)} 道题")
        
        # 2. 加载模型
        logger.info(f"加载语义模型: {MODEL_DIR} (device={DEVICE})")
        _model = SentenceTransformer(MODEL_DIR, device=DEVICE)
        prepare_model_precision(_model)
        logger.info("模型加载完成")
        
        # 3. 加载或构建向量
        _embeddings, _meta = build_or_load_embeddings(_database, _model)
        logger.info(f"向量加载完成，共 {len(_meta)} 个向量")
        
        logger.info("搜题引擎初始化完成！")
        return True
        
    except Exception as e:
        logger.error(f"初始化失败: {e}", exc_info=True)
        return False


def format_question_result(q: Dict[str, Any], sim: float) -> Dict[str, Any]:
    """格式化题目结果为JSON"""
    return {
        "number": q.get("number"),
        "type": q.get("type", "unknown"),
        "question": q.get("question", "").strip(),
        "options": q.get("options", {}),
        "answer": q.get("answer", "答案未找到"),
        "similarity": round(float(sim), 4),
    }


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    if _model is None or _database is None:
        return jsonify({
            "status": "not_ready",
            "message": "搜题引擎未初始化"
        }), 503
    
    return jsonify({
        "status": "ready",
        "message": "搜题引擎运行正常",
        "database_size": len(_database),
        "embeddings_size": len(_meta) if _meta else 0
    })


@app.route('/search', methods=['POST'])
def search():
    """搜题接口
    
    请求体格式（JSON）:
    {
        "query": "题目文本或关键词",
        "top_k": 5  // 可选，默认5
    }
    
    返回格式（JSON）:
    {
        "success": true,
        "query": "原始查询",
        "count": 结果数量,
        "results": [
            {
                "number": "题号",
                "type": "题型",
                "question": "题干",
                "options": {"A": "...", "B": "..."},
                "answer": "标准答案",
                "similarity": 0.95
            },
            ...
        ]
    }
    """
    if _model is None or _database is None or _embeddings is None:
        return jsonify({
            "success": False,
            "error": "搜题引擎未初始化，请稍后重试"
        }), 503
    
    try:
        # 解析请求数据
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "error": "请求体必须为JSON格式"
            }), 400
        
        query = data.get("query", "").strip()
        if not query:
            return jsonify({
                "success": False,
                "error": "query字段不能为空"
            }), 400
        
        top_k = data.get("top_k", TOP_K_DEFAULT)
        if not isinstance(top_k, int) or top_k < 1:
            top_k = TOP_K_DEFAULT
        top_k = min(top_k, 20)  # 限制最多返回20条
        
        logger.info(f"收到搜题请求: query='{query[:50]}...', top_k={top_k}")
        
        # 执行搜索
        results = search_questions(
            query=query,
            db=_database,
            embeddings=_embeddings,
            meta=_meta,
            model=_model,
            top_k=top_k
        )
        
        # 格式化结果
        formatted_results = [
            format_question_result(q, sim) for q, sim in results
        ]
        
        logger.info(f"搜索完成，找到 {len(formatted_results)} 条结果")
        
        return jsonify({
            "success": True,
            "query": query,
            "count": len(formatted_results),
            "results": formatted_results
        })
        
    except Exception as e:
        logger.error(f"搜题失败: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": f"搜题过程出错: {str(e)}"
        }), 500


@app.route('/search/batch', methods=['POST'])
def search_batch():
    """批量搜题接口
    
    请求体格式（JSON）:
    {
        "queries": ["题目1", "题目2", ...],
        "top_k": 5  // 可选
    }
    """
    if _model is None or _database is None or _embeddings is None:
        return jsonify({
            "success": False,
            "error": "搜题引擎未初始化"
        }), 503
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "error": "请求体必须为JSON格式"
            }), 400
        
        queries = data.get("queries", [])
        if not isinstance(queries, list) or len(queries) == 0:
            return jsonify({
                "success": False,
                "error": "queries字段必须是非空数组"
            }), 400
        
        top_k = data.get("top_k", TOP_K_DEFAULT)
        if not isinstance(top_k, int) or top_k < 1:
            top_k = TOP_K_DEFAULT
        top_k = min(top_k, 20)
        
        logger.info(f"收到批量搜题请求: {len(queries)} 个查询")
        
        batch_results = []
        for query in queries:
            if not isinstance(query, str) or not query.strip():
                batch_results.append({
                    "success": False,
                    "query": query,
                    "error": "查询文本无效"
                })
                continue
            
            try:
                results = search_questions(
                    query=query.strip(),
                    db=_database,
                    embeddings=_embeddings,
                    meta=_meta,
                    model=_model,
                    top_k=top_k
                )
                batch_results.append({
                    "success": True,
                    "query": query,
                    "count": len(results),
                    "results": [
                        format_question_result(q, sim) for q, sim in results
                    ]
                })
            except Exception as e:
                logger.error(f"批量搜索中单个查询失败: {e}")
                batch_results.append({
                    "success": False,
                    "query": query,
                    "error": str(e)
                })
        
        return jsonify({
            "success": True,
            "total": len(queries),
            "results": batch_results
        })
        
    except Exception as e:
        logger.error(f"批量搜题失败: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": f"批量搜题过程出错: {str(e)}"
        }), 500


@app.route('/', methods=['GET'])
def index():
    """API文档"""
    return jsonify({
        "name": "搜题API服务",
        "version": "1.0.0",
        "endpoints": {
            "GET /health": "健康检查",
            "POST /search": "单题搜索",
            "POST /search/batch": "批量搜索",
        },
        "usage": {
            "单题搜索": {
                "url": "/search",
                "method": "POST",
                "body": {
                    "query": "题目文本",
                    "top_k": 5
                }
            },
            "批量搜索": {
                "url": "/search/batch",
                "method": "POST",
                "body": {
                    "queries": ["题目1", "题目2"],
                    "top_k": 5
                }
            }
        }
    })


if __name__ == '__main__':
    # 初始化搜题引擎
    if not init_search_engine():
        logger.error("搜题引擎初始化失败，程序退出")
        exit(1)
    
    # 启动服务器
    host = os.getenv('API_HOST', '0.0.0.0')
    port = int(os.getenv('API_PORT', '5000'))
    
    logger.info(f"API服务器启动: http://{host}:{port}")
    logger.info("API文档: http://localhost:{}/".format(port))
    logger.info("健康检查: http://localhost:{}/health".format(port))
    logger.info("搜题接口: http://localhost:{}/search".format(port))
    
    app.run(host=host, port=port, debug=False, threaded=True)

