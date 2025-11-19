"""
API测试脚本
演示如何调用搜题API
"""
import requests
import json

API_BASE = "http://localhost:5000"


def test_health():
    """测试健康检查"""
    print("=" * 60)
    print("测试健康检查接口")
    print("=" * 60)
    response = requests.get(f"{API_BASE}/health")
    print(f"状态码: {response.status_code}")
    print(f"响应: {json.dumps(response.json(), ensure_ascii=False, indent=2)}")
    print()


def test_search():
    """测试单题搜索"""
    print("=" * 60)
    print("测试单题搜索接口")
    print("=" * 60)
    
    # 测试查询
    query = "债期货合约最后交易日之前的交割结算价为卖方交割申报当日的结算价，最后交易日的交割结算价为集中交易中该合约最后交易日全部成交价格按照成交量的加权平均价"
    
    payload = {
        "query": query,
        "top_k": 5
    }
    
    print(f"查询: {query[:50]}...")
    print(f"请求体: {json.dumps(payload, ensure_ascii=False, indent=2)}")
    print()
    
    response = requests.post(
        f"{API_BASE}/search",
        json=payload,
        headers={"Content-Type": "application/json"}
    )
    
    print(f"状态码: {response.status_code}")
    result = response.json()
    
    if result.get("success"):
        print(f"找到 {result['count']} 条结果:")
        for i, item in enumerate(result["results"], 1):
            print(f"\n结果 {i}:")
            print(f"  题号: {item['number']}")
            print(f"  相似度: {item['similarity']}")
            print(f"  题干: {item['question'][:60]}...")
            print(f"  答案: {item['answer']}")
    else:
        print(f"错误: {result.get('error')}")
    print()


def test_batch_search():
    """测试批量搜索"""
    print("=" * 60)
    print("测试批量搜索接口")
    print("=" * 60)
    
    queries = [
        "政府对新能源汽车实施生产厂家补贴政策",
        "国债期货合约最后交易日之前的交割结算价",
        "完全竞争市场上短期生产者均衡的条件"
    ]
    
    payload = {
        "queries": queries,
        "top_k": 3
    }
    
    print(f"批量查询 {len(queries)} 个题目")
    print()
    
    response = requests.post(
        f"{API_BASE}/search/batch",
        json=payload,
        headers={"Content-Type": "application/json"}
    )
    
    print(f"状态码: {response.status_code}")
    result = response.json()
    
    if result.get("success"):
        for i, item in enumerate(result["results"], 1):
            if item.get("success"):
                print(f"\n查询 {i}: {item['query'][:40]}...")
                print(f"  找到 {item['count']} 条结果")
                if item["results"]:
                    top = item["results"][0]
                    print(f"  最佳匹配: 题号{top['number']}, 相似度{top['similarity']}")
            else:
                print(f"\n查询 {i} 失败: {item.get('error')}")
    else:
        print(f"错误: {result.get('error')}")
    print()


if __name__ == '__main__':
    try:
        # 测试健康检查
        test_health()
        
        # 测试单题搜索
        test_search()
        
        # 测试批量搜索
        test_batch_search()
        
        print("=" * 60)
        print("所有测试完成！")
        print("=" * 60)
        
    except requests.exceptions.ConnectionError:
        print("错误: 无法连接到API服务器")
        print("请确保 api_server.py 正在运行")
    except Exception as e:
        print(f"测试出错: {e}")

