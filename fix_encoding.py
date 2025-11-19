"""
修复Windows控制台编码问题的辅助脚本
在启动服务前运行此脚本可以设置正确的编码
"""
import sys
import os

def fix_console_encoding():
    """修复Windows控制台编码"""
    if sys.platform == 'win32':
        # 设置控制台代码页为UTF-8
        os.system('chcp 65001 >nul 2>&1')
        
        # 重新配置标准输出流
        if hasattr(sys.stdout, 'reconfigure'):
            try:
                sys.stdout.reconfigure(encoding='utf-8')
            except:
                pass
        if hasattr(sys.stderr, 'reconfigure'):
            try:
                sys.stderr.reconfigure(encoding='utf-8')
            except:
                pass
        
        # 设置环境变量
        os.environ['PYTHONIOENCODING'] = 'utf-8'
        
        print("控制台编码已设置为UTF-8")

if __name__ == '__main__':
    fix_console_encoding()

