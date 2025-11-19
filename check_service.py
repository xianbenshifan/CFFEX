"""
检查代理服务状态
"""
import requests
import time
import sys

PORT = 8889
BASE_URL = f"http://localhost:{PORT}"

def check_service():
    """检查服务状态"""
    print("=" * 60)
    print("检查代理服务状态")
    print("=" * 60)
    print(f"服务地址: {BASE_URL}")
    print()
    
    try:
        print("正在连接服务...")
        response = requests.get(f"{BASE_URL}/health", timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            print("[OK] 服务运行正常！")
            print(f"  状态: {data.get('status')}")
            print(f"  缓存题目数: {data.get('cache_size', 0)}")
            return True
        elif response.status_code == 503:
            print("[等待] 服务未就绪（模型正在加载中）")
            print("  请等待1-2分钟，模型加载完成后服务会自动就绪")
            return False
        else:
            print(f"[错误] 服务异常，状态码: {response.status_code}")
            return False
            
    except requests.exceptions.ConnectionError:
        print("[错误] 无法连接到服务")
        print("  请确保 proxy_server.py 正在运行")
        print(f"  检查端口 {PORT} 是否被占用")
        return False
    except Exception as e:
        print(f"[错误] 检查失败: {e}")
        return False

def check_cache():
    """检查缓存"""
    try:
        response = requests.get(f"{BASE_URL}/cache", timeout=5)
        if response.status_code == 200:
            data = response.json()
            count = data.get('count', 0)
            print(f"\n当前缓存: {count} 道题目")
            if count > 0:
                print("\n缓存的题目:")
                for qid, info in list(data.get('cache', {}).items())[:5]:
                    print(f"  ID {qid}: {info.get('answer', 'N/A')} (相似度: {info.get('similarity', 0)})")
            return True
    except Exception as e:
        print(f"无法获取缓存: {e}")
        return False

if __name__ == '__main__':
    print("等待服务初始化...")
    for i in range(6):
        time.sleep(5)
        print(f"尝试 {i+1}/6...")
        if check_service():
            check_cache()
            print("\n" + "=" * 60)
            print("服务已就绪！")
            print("=" * 60)
            print("\n使用说明:")
            print(f"1. 代理地址: localhost:{PORT} (或你的电脑IP:{PORT})")
            print("2. 在手机/设备上配置HTTP代理")
            print("3. 打开东方财富APP开始答题")
            print("4. 服务会自动搜题并修改答案")
            sys.exit(0)
    
    print("\n" + "=" * 60)
    print("服务可能还在初始化中，请稍后再试")
    print("或者检查 proxy_server.py 的输出日志")
    print("=" * 60)

