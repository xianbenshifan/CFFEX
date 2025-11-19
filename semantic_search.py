import json
import os
from typing import List, Dict, Any
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
DEFAULT_PROXY = "http://127.0.0.1:8888"
os.environ.setdefault("HTTP_PROXY", DEFAULT_PROXY)
os.environ.setdefault("HTTPS_PROXY", DEFAULT_PROXY)
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HOME", os.path.join(os.getcwd(), "hf_cache"))
os.environ["HF_HUB_DISABLE_SSL_VERIFY"] = "1"  # 代理自签证书场景
os.environ["CURL_CA_BUNDLE"] = ""
HF_CACHE_DIR = os.environ["HF_HOME"]
os.makedirs(HF_CACHE_DIR, exist_ok=True)
CACHE_FOLDER = os.path.join(HF_CACHE_DIR, "models")
os.makedirs(CACHE_FOLDER, exist_ok=True)

import numpy as np
import torch
from sentence_transformers import SentenceTransformer, util

# ---------------- 配置 ----------------
DATABASE_PATH = "database.json"
EMBEDDING_CACHE = "question_embeddings.npy"
META_CACHE = "question_meta.json"
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"  # 支持中文的轻量模型
MODEL_DIR = os.path.join(CACHE_FOLDER, MODEL_NAME)
TOP_K_DEFAULT = 5
DEVICE = "cpu"  # 强制使用 CPU，避免 CUDA 兼容问题


def load_database(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到题库文件: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def prepare_model_precision(model: SentenceTransformer) -> None:
    """
    torch==2.5 在 CPU 上推理 MiniLM 时会在 float32 精度里产生 NaN。
    将底层 Transformer 转成 float64 可以彻底规避该问题。
    """
    if DEVICE != "cpu":
        return
    try:
        first_module = model._first_module()
    except AttributeError:
        return
    auto_model = getattr(first_module, "auto_model", None)
    if auto_model is not None:
        first_module.auto_model = auto_model.to(dtype=torch.float64)


def ensure_clean_embeddings(path: str) -> np.ndarray:
    embeddings = np.load(path)
    if np.isnan(embeddings).any():
        raise ValueError("缓存向量包含 NaN")
    return embeddings


def build_corpus_text(q: Dict[str, Any]) -> str:
    """
    把题干 + 选项 拼成一个用于语义搜索的文本。
    这样模型在编码时能同时考虑题目和选项的语义。
    """
    question = q.get("question", "").strip()
    options = q.get("options", {})
    option_parts = []
    for k in sorted(options.keys()):
        option_parts.append(f"{k}. {options[k]}")
    option_text = " ".join(option_parts)
    return f"{question}  {option_text}" if option_text else question


def build_or_load_embeddings(
    db: List[Dict[str, Any]],
    model: SentenceTransformer,
):
    """
    优先从本地缓存加载向量；若没有，则重新计算并缓存。
    """
    if os.path.exists(EMBEDDING_CACHE) and os.path.exists(META_CACHE):
        print(">> 从缓存加载题目向量 ...")
        try:
            embeddings = ensure_clean_embeddings(EMBEDDING_CACHE)
            with open(META_CACHE, "r", encoding="utf-8") as f:
                meta = json.load(f)
            return embeddings, meta
        except ValueError:
            print(">> 检测到缓存包含 NaN，删除缓存并重新计算 ...")
            os.remove(EMBEDDING_CACHE)
            os.remove(META_CACHE)

    print(">> 未检测到缓存，开始重新编码所有题目 ...")
    corpus_texts = []
    meta = []  # 保存 number / type / index 等信息
    for idx, q in enumerate(db):
        text = build_corpus_text(q)
        if not text.strip():
            continue
        corpus_texts.append(text)
        meta.append(
            {
                "idx": idx,
                "number": q.get("number"),
                "type": q.get("type"),
            }
        )

    print(f">> 需要编码的题目数量: {len(corpus_texts)}")
    embeddings = model.encode(
        corpus_texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
        batch_size=64,
        show_progress_bar=True,
        device=DEVICE,
    )

    if np.isnan(embeddings).any():
        raise RuntimeError(
            "重新编码后的向量仍包含 NaN，请检查模型或硬件环境。"
        )
    embeddings = embeddings.astype(np.float32, copy=False)

    np.save(EMBEDDING_CACHE, embeddings)
    with open(META_CACHE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(">> 向量已缓存，下次启动可以直接加载。")
    return embeddings, meta


def search_questions(
    query: str,
    db: List[Dict[str, Any]],
    embeddings: np.ndarray,
    meta: List[Dict[str, Any]],
    model: SentenceTransformer,
    top_k: int = TOP_K_DEFAULT,
):
    """
    根据查询语句进行语义搜索，返回相似度最高的 top_k 道题。
    """
    query_vec = model.encode(
        query,
        convert_to_tensor=True,
        normalize_embeddings=True,
        device=DEVICE,
    )
    query_vec = query_vec.to(dtype=torch.float32)
    corpus_vecs = torch.from_numpy(embeddings)
    scores = util.cos_sim(query_vec, corpus_vecs)[0]

    # 取 top_k 索引
    top_k = min(top_k, len(meta))
    top_results = np.argpartition(-scores.cpu().numpy(), range(top_k))[:top_k]
    # 按相似度重新排序
    top_results = sorted(top_results, key=lambda i: float(scores[i]), reverse=True)

    results = []
    for rank, idx in enumerate(top_results, start=1):
        m = meta[idx]
        q_idx = m["idx"]
        q = db[q_idx]
        sim = float(scores[idx])
        results.append((q, sim))
    return results


def pretty_print_question(q: Dict[str, Any], sim: float):
    number = q.get("number")
    q_type = q.get("type", "unknown")
    question = q.get("question", "").strip()
    options = q.get("options", {})
    answer = q.get("answer", "答案未找到")

    print("=" * 80)
    print(f"题号: {number}    题型: {q_type}    相似度: {sim:.4f}")
    print("题干:")
    print(question)
    if options:
        print("选项:")
        for k in sorted(options.keys()):
            print(f"  {k}. {options[k]}")
    print(f"标准答案: {answer}")


def main():
    print("=== 语义搜题系统 (NLP 嵌入检索) ===")
    print("首次运行会自动加载模型并为整套题库生成向量，可能需要几分钟。")
    print("输入自然语言问题或关键词进行搜索，输入 空行 / exit / quit 退出。")

    # 1. 加载题库
    db = load_database(DATABASE_PATH)

    # 2. 加载模型
    print(f">> 加载语义模型: {MODEL_DIR} (device={DEVICE})")
    model = SentenceTransformer(
        MODEL_DIR,
        device=DEVICE,
    )
    prepare_model_precision(model)

    # 3. 构建或加载题目向量
    embeddings, meta = build_or_load_embeddings(db, model)

    # 4. 交互式查询
    while True:
        try:
            query = input("\n请输入要搜索的题目描述: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not query or query.lower() in {"exit", "quit"}:
            print("结束搜索，再见！")
            break

        # try:
        #     k_str = input(f"返回前多少条结果? (默认 {TOP_K_DEFAULT}): ").strip()
        # except (EOFError, KeyboardInterrupt):
        #     k_str = ""
        # if not k_str.isdigit():
        #     top_k = TOP_K_DEFAULT
        # else:
        #     top_k = max(1, int(k_str))
        

        top_k = TOP_K_DEFAULT
        print("\n>> 正在检索，请稍候 ...")
        results = search_questions(query, db, embeddings, meta, model, top_k=top_k)

        print(f"\n=== 为你找到 {len(results)} 条相似题目 ===")
        for q, sim in results:
            pretty_print_question(q, sim)


if __name__ == "__main__":
    main()