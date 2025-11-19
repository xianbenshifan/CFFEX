"""
测试代理服务器
模拟东方财富API请求
"""
import requests
import json
import time

PROXY_BASE = "http://localhost:8888"
TARGET_BASE = "https://quiz-activity.eastmoney.com"


def test_get_quiz():
    """测试获取题目接口"""
    print("=" * 60)
    print("测试获取题目接口")
    print("=" * 60)
    
    # 模拟请求头
    headers = {
        'Host': 'quiz-activity.eastmoney.com',
        'Accept': 'application/json, text/plain, */*',
        'User-Agent': 'Mozilla/5.0 (Linux; Android 9; ROG Phone 7 Ultimate Build/PQ3A.190705.10241111; wv) AppleWebKit/537.36',
        'Origin': 'https://quiz-activity.eastmoney.com',
        'Referer': 'https://quiz-activity.eastmoney.com/Mzpvia',
    }
    
    # 使用代理服务器
    url = f"{PROXY_BASE}/qa/api/quiz/Mzpvia"
    params = {
        '_ts': int(time.time() * 1000),
        'qgqpBId': '',
        'bankCode': '98WExa'
    }
    
    print(f"请求URL: {url}")
    print(f"参数: {params}")
    print()
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        print(f"状态码: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"响应数据: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}...")
            
            # 检查是否有题目数据
            question_data = data.get("data", {})
            if question_data:
                question_id = question_data.get("questionId") or question_data.get("id")
                question_text = question_data.get("question", "")
                print(f"\n题目ID: {question_id}")
                print(f"题目: {question_text[:100]}...")
        else:
            print(f"错误: {response.text}")
    except Exception as e:
        print(f"请求失败: {e}")
    
    print()


def test_submit_answer():
    """测试提交答案接口"""
    print("=" * 60)
    print("测试提交答案接口")
    print("=" * 60)
    
    headers = {
        'Host': 'quiz-activity.eastmoney.com',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Linux; Android 9; ROG Phone 7 Ultimate Build/PQ3A.190705.10241111; wv) AppleWebKit/537.36',
        'Origin': 'https://quiz-activity.eastmoney.com',
        'Referer': 'https://quiz-activity.eastmoney.com/Mzpvia',
    }
    
    # 测试数据（模拟提交错误答案）
    payload = {
        "answerIndex": "2",  # 错误答案
        "questionId": 54263,
        "quizId": "17285994"
    }
    
    url = f"{PROXY_BASE}/qa/api/quiz/answer"
    params = {
        '_ts': int(time.time() * 1000)
    }
    
    print(f"请求URL: {url}")
    print(f"原始答案: {payload['answerIndex']}")
    print(f"题目ID: {payload['questionId']}")
    print()
    
    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            params=params,
            timeout=10
        )
        print(f"状态码: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"响应: {json.dumps(data, ensure_ascii=False, indent=2)}")
        else:
            print(f"错误: {response.text}")
    except Exception as e:
        print(f"请求失败: {e}")
    
    print()


def test_cache():
    """查看缓存"""
    print("=" * 60)
    print("查看题目缓存")
    print("=" * 60)
    
    try:
        response = requests.get(f"{PROXY_BASE}/cache")
        data = response.json()
        
        print(f"缓存数量: {data.get('count', 0)}")
        print()
        
        cache = data.get('cache', {})
        for qid, info in cache.items():
            print(f"题目ID: {qid}")
            print(f"  题目: {info.get('question_text', '')[:60]}...")
            print(f"  答案: {info.get('answer', '')}")
            print(f"  相似度: {info.get('similarity', 0)}")
            if 'answer_index' in info:
                print(f"  答案索引: {info['answer_index']} (选项: {chr(65+info['answer_index'])})")
            print()
    except Exception as e:
        print(f"获取缓存失败: {e}")


def test_health():
    """测试健康检查"""
    print("=" * 60)
    print("测试健康检查")
    print("=" * 60)
    
    try:
        response = requests.get(f"{PROXY_BASE}/health")
        print(f"状态码: {response.status_code}")
        print(f"响应: {json.dumps(response.json(), ensure_ascii=False, indent=2)}")
    except Exception as e:
        print(f"请求失败: {e}")
    print()


if __name__ == '__main__':
    print("=" * 60)
    print("代理服务器测试")
    print("=" * 60)
    print("请确保 proxy_server.py 正在运行")
    print()
    
    try:
        # 健康检查
        test_health()
        
        # 测试获取题目（需要先有真实的题目ID）
        # test_get_quiz()
        
        # 查看缓存
        test_cache()
        
        # 测试提交答案（需要先有缓存的题目）
        # test_submit_answer()
        
        print("=" * 60)
        print("提示：")
        print("1. 如果使用真实API，请先运行 test_get_quiz() 获取题目")
        print("2. 然后运行 test_submit_answer() 测试自动修改答案")
        print("3. 使用 test_cache() 查看所有缓存的题目和答案")
        print("=" * 60)
        
    except requests.exceptions.ConnectionError:
        print("错误: 无法连接到代理服务器")
        print("请确保 proxy_server.py 正在运行在 http://localhost:8888")
    except Exception as e:
        print(f"测试出错: {e}")

