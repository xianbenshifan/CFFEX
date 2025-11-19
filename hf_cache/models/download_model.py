# download_model.py
import os
from huggingface_hub import snapshot_download

MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

BASE_DIR = os.path.join(os.getcwd(), "hf_cache", "models")
TARGET_DIR = os.path.join(BASE_DIR, "paraphrase-multilingual-MiniLM-L12-v2")
os.makedirs(TARGET_DIR, exist_ok=True)

print(f"目标目录: {TARGET_DIR}")

download_kwargs = {
    "repo_id": MODEL_ID,
    "local_dir": TARGET_DIR,
    "local_dir_use_symlinks": False,
    "revision": "main",
    "resume_download": True,
}

# 如果仍需通过代理，可取消下面两行的注释并按需修改端口
# download_kwargs["proxies"] = {
#     "http": "http://127.0.0.1:8888",
#     "https": "http://127.0.0.1:8888",
# }

snapshot_download(**download_kwargs)
print("模型下载完成，可以重新运行 semantic_search.py 了。")