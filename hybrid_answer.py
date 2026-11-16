"""
混合答案引擎：本地题库优先，低置信度时调用大模型 API。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Dict, Optional

import requests

import config

logger = logging.getLogger(__name__)


def normalize_text(text: str) -> str:
    """统一空白、标点和大小写，便于本地精确匹配。"""
    text = str(text or "").strip().lower()
    return re.sub(r"[\s\u3000，。！？；：、（）()【】\[\]“”‘’\"'…·]+", "", text)


def answer_index(answer: Any, options: Any) -> Optional[int]:
    """把答案字母转换为选项索引。"""
    value = str(answer or "").strip().upper()
    if len(value) == 1 and "A" <= value <= "Z":
        index = ord(value) - ord("A")
        if isinstance(options, list) and index < len(options):
            return index
        if isinstance(options, dict):
            keys = sorted(options.keys())
            return keys.index(value) if value in keys else None
    return None


class HybridAnswerEngine:
    """本地题库 + 本地语义检索 + 大模型 API 的三级答案引擎。"""

    def __init__(
        self,
        local_resolver: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        database_path: str = "database.json",
        local_threshold: Optional[float] = None,
    ) -> None:
        self.local_resolver = local_resolver
        self.local_threshold = float(
            local_threshold
            if local_threshold is not None
            else config.get_float("LOCAL_MATCH_THRESHOLD", 0.82)
        )
        # 唯一大模型平台：DeepSeek 官方 OpenAI 兼容接口。
        self.llm_api_base = (
            config.get("LLM_API_BASE", "https://api.deepseek.com/v1") or ""
        ).rstrip("/")
        self.llm_api_key = config.api_key("DEEPSEEK_API_KEY")
        self.llm_model = config.get("LLM_MODEL", "deepseek-flash")
        self._exact_index: Dict[str, Dict[str, Any]] = {}
        self._load_exact_index(database_path)

    def _load_exact_index(self, database_path: str) -> None:
        """加载本地题库，建立无模型精确匹配索引。"""
        try:
            with open(database_path, "r", encoding="utf-8") as file:
                database = json.load(file)
        except (OSError, json.JSONDecodeError):
            return
        for item in database:
            question = str(item.get("question", ""))
            key = normalize_text(question)
            if key:
                self._exact_index[key] = item

    def resolve(
        self,
        question_text: str,
        options: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """执行本地精确、本地语义、大模型兜底。"""
        exact = self._exact_index.get(normalize_text(question_text))
        if exact is None:
            # 抓包管线会把选项拼接到题干后面，再尝试只匹配题干。
            title_only = re.split(r"\s+A\.\s*", question_text, maxsplit=1)[0]
            exact = self._exact_index.get(normalize_text(title_only))
        if exact:
            return self._format_local(exact, 1.0, options, "local_exact")

        if self.local_resolver:
            local = self.local_resolver(question_text)
            if local and local.get("answer"):
                similarity = float(local.get("similarity") or 0)
                if similarity >= self.local_threshold:
                    local["answer_index"] = local.get("answer_index")
                    if local["answer_index"] is None:
                        local["answer_index"] = answer_index(local.get("answer"), options)
                    local["answer_source"] = "local_semantic"
                    local["route"] = "local"
                    return local

        remote = self._resolve_with_llm(question_text, options)
        if remote:
            remote["route"] = "llm_fallback"
        return remote

    def _format_local(
        self,
        item: Dict[str, Any],
        similarity: float,
        options: Any,
        source: str,
    ) -> Dict[str, Any]:
        """统一本地题库结果格式。"""
        return {
            "answer": item.get("answer"),
            "answer_index": answer_index(item.get("answer"), options or item.get("options")),
            "similarity": similarity,
            "explanation": item.get("analysis") or item.get("explanation"),
            "answer_source": source,
            "matched_number": item.get("number"),
            "matched_question": item.get("question"),
        }

    def _resolve_with_llm(
        self,
        question_text: str,
        options: Any,
    ) -> Optional[Dict[str, Any]]:
        """调用 OpenAI-compatible API，并强制要求 JSON 输出。"""
        if not self.llm_api_key:
            logger.warning(
                "未设置 DEEPSEEK_API_KEY，跳过 llm_api 兜底；"
                "其余答案路由不受影响。"
            )
            return None
        option_text = self._format_options(options)
        prompt = (
            "请分析下面的选择题，只返回 JSON，不要返回 Markdown。"
            'JSON 格式：{"answer":"A","answer_index":0,"explanation":"...","confidence":0.0}。'
            f"\n题目：{question_text}\n选项：{option_text}"
        )
        headers = {"Content-Type": "application/json"}
        if self.llm_api_key:
            headers["Authorization"] = f"Bearer {self.llm_api_key}"
        payload = {
            "model": self.llm_model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": "你是题目解析服务，必须依据题干和选项给出答案与简短解析。",
                },
                {"role": "user", "content": prompt},
            ],
        }
        try:
            response = requests.post(
                f"{self.llm_api_base}/chat/completions",
                json=payload,
                headers=headers,
                timeout=config.get_float("LLM_TIMEOUT", 30.0),
            )
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            result = self._parse_json(content)
            if not result.get("answer"):
                return None
            result["answer_index"] = result.get("answer_index")
            if result["answer_index"] is None:
                result["answer_index"] = answer_index(result.get("answer"), options)
            result["answer_source"] = "llm_api"
            result["similarity"] = float(result.get("confidence") or 0)
            return result
        except (OSError, requests.RequestException, KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _format_options(options: Any) -> str:
        """格式化选项文本。"""
        if isinstance(options, dict):
            return " ".join(f"{key}. {value}" for key, value in sorted(options.items()))
        if isinstance(options, list):
            return " ".join(f"{chr(65 + i)}. {value}" for i, value in enumerate(options))
        return ""

    @staticmethod
    def _parse_json(content: str) -> Dict[str, Any]:
        """兼容模型偶尔返回的代码围栏。"""
        content = str(content or "").strip()
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.I)
        content = re.sub(r"\s*```$", "", content)
        return json.loads(content)
