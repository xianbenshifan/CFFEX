"""
自动从Charles导出的JSON中提取题目响应并发送到Flask服务
"""
import json
import requests
import sys

FLASK_URL = "http://localhost:8889/qa/api/quiz/receive"

def send_quiz_response(response_data):
    """发送题目响应到Flask"""
    try:
        response = requests.post(
            FLASK_URL,
            json=response_data,
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        if response.status_code == 200:
            print(f"✓ 已发送到Flask: {response.json().get('message', '')}")
            return True
        else:
            print(f"✗ 发送失败: {response.status_code}")
            return False
    except Exception as e:
        print(f"✗ 发送错误: {e}")
        return False

def extract_from_charles_json(file_path):
    """从Charles导出的JSON中提取题目响应"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Charles导出的格式可能不同，需要根据实际格式调整
        sessions = data.get('sessions', [])
        if not sessions:
            sessions = data  # 如果直接是数组
        
        count = 0
        for session in sessions:
            host = session.get('host', '') or session.get('request', {}).get('host', '')
            path = session.get('path', '') or session.get('request', {}).get('path', '')
            
            if 'quiz-activity.eastmoney.com' in host and '/qa/api/quiz/' in path:
                # 获取响应数据
                response_body = session.get('response', {}).get('body', {})
                if isinstance(response_body, str):
                    try:
                        response_body = json.loads(response_body)
                    except:
                        continue
                
                if response_body:
                    print(f"找到题目响应: {path}")
                    if send_quiz_response(response_body):
                        count += 1
        
        print(f"\n共发送 {count} 个题目响应到Flask")
        return count > 0
        
    except FileNotFoundError:
        print(f"文件不存在: {file_path}")
        return False
    except json.JSONDecodeError as e:
        print(f"JSON解析错误: {e}")
        return False
    except Exception as e:
        print(f"处理错误: {e}")
        return False

def manual_send():
    """手动输入响应数据"""
    print("请输入题目响应JSON数据（输入空行结束）:")
    lines = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append(line)
    
    try:
        data = json.loads('\n'.join(lines))
        send_quiz_response(data)
    except json.JSONDecodeError as e:
        print(f"JSON格式错误: {e}")
    except Exception as e:
        print(f"错误: {e}")

if __name__ == '__main__':
    if len(sys.argv) > 1:
        # 从文件读取
        file_path = sys.argv[1]
        extract_from_charles_json(file_path)
    else:
        # 交互式输入
        print("=" * 60)
        print("发送题目响应到Flask服务")
        print("=" * 60)
        print()
        print("使用方法:")
        print("1. 从Charles复制响应JSON数据")
        print("2. 粘贴到下方（输入空行结束）")
        print()
        manual_send()

