"""
混合答案引擎：本地题库优先，低置信度时调用大模型 API。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

import requests

import config

logger = logging.getLogger(__name__)


def normalize_text(text: str) -> str:
    """统一空白、标点和大小写，便于本地精确匹配。"""
    text = str(text or "").strip().lower()
    return re.sub(r"[\s\u3000，。！？；：、（）()【】\[\]“”‘’\"'…·]+", "", text)


def answer_letters(answer: Any) -> str:
    """把答案收成去重、升序的选项字母。没有字母时返回空串。"""
    return "".join(sorted({ch for ch in str(answer or "").upper() if "A" <= ch <= "Z"}))


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
        # 题库语义结果要真正采用需同时满足这个更严的相似度门槛。
        # 实测 0.6~0.85 区间常有跨考点误配，因此默认抬到 0.95。
        self.semantic_min_similarity = config.get_float("SEMANTIC_MIN_SIMILARITY", 0.95)
        # 唯一大模型平台：DeepSeek 官方 OpenAI 兼容接口。
        self.llm_api_base = (
            config.get("LLM_API_BASE", "https://api.deepseek.com/v1") or ""
        ).rstrip("/")
        self.llm_api_key = config.api_key("DEEPSEEK_API_KEY")
        self.llm_model = config.get("LLM_MODEL", "deepseek-flash")
        self._exact_index: Dict[str, List[Dict[str, Any]]] = {}
        self._load_exact_index(database_path)

    def _load_exact_index(self, database_path: str) -> None:
        """加载本地题库，建立无模型精确匹配索引。

        去标点后题干相同的题目放在同一组。题库里有两道题只差一个逗号，
        答案分别是 B 和 C，后写入的那条不能盖掉前一条。
        """
        try:
            with open(database_path, "r", encoding="utf-8") as file:
                database = json.load(file)
        except (OSError, json.JSONDecodeError):
            return
        for item in database:
            question = str(item.get("question", ""))
            key = normalize_text(question)
            if key:
                self._exact_index.setdefault(key, []).append(item)

    def _pick_exact(self, question_text: str) -> Optional[Dict[str, Any]]:
        """按题干取精确匹配；题干撞车时用选项正文区分。"""
        title_only = re.split(r"\s+A\.\s*", question_text, maxsplit=1)[0]
        group = self._exact_index.get(normalize_text(question_text))
        if not group:
            group = self._exact_index.get(normalize_text(title_only))
        if not group:
            return None
        if len(group) == 1:
            return group[0]

        live = normalize_text(question_text)
        matched: List[Dict[str, Any]] = []
        for item in group:
            options = item.get("options") or {}
            if not isinstance(options, dict):
                continue
            texts = [normalize_text(value) for value in options.values()]
            texts = [text for text in texts if text]
            if texts and all(text in live for text in texts):
                matched.append(item)
        if len(matched) == 1:
            return matched[0]
        answers = {str(item.get("answer") or "") for item in group}
        if len(answers) == 1:
            return group[-1]
        return None

    def resolve(
        self,
        question_text: str,
        options: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """按优先级返回单个答案：题库精确 -> 本地语义 -> 大模型。

        保留此方法供只需一个答案的场景使用；需要同时看题库与大模型两份结论时用
        `resolve_both`。
        """
        exact = self._pick_exact(question_text)
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

    def resolve_both(
        self,
        question_text: str,
        options: Any = None,
    ) -> Dict[str, Any]:
        """同时给出题库侧与大模型侧两份答案，供人工选择。

        与 `resolve` 的区别是不做优先级短路：即使题库命中，也照样询问大模型，
        让两种判断并列呈现、互相校验。
        """
        bank = self._resolve_bank(question_text, options)
        llm = self._resolve_with_llm(question_text, options)
        if llm:
            llm["route"] = "llm"

        agree = None
        if bank and llm:
            bank_answer = answer_letters(bank.get("answer"))
            llm_answer = answer_letters(llm.get("answer"))
            agree = bool(bank_answer) and bank_answer == llm_answer

        return {
            "question_text": question_text,
            "options": options,
            "bank": bank,
            "llm": llm,
            "agree": agree,
        }

    def _resolve_bank(
        self,
        question_text: str,
        options: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """只走题库侧：精确匹配优先，语义检索必须达到严格门槛才采用。

        实测题库语义检索会把不同考点的题当成近似题（如「中证1000 期权的合约要素」
        匹到「中证1000 期货的合约乘数」，相似度 0.77），这类结果比没有答案更有害。
        因此语义结果必须同时满足：相似度达到 semantic_min_similarity，
        且匹配到的题干与当前题干在去掉标点后高度一致，否则丢弃。
        """
        exact = self._pick_exact(question_text)
        if exact:
            return self._format_local(exact, 1.0, options, "local_exact")

        scanned = self._scan_titles(question_text, options)
        if scanned:
            return scanned

        if not self.local_resolver:
            return None

        try:
            local = self.local_resolver(question_text)
        except Exception:
            logger.exception("本地语义检索失败")
            return None
        if not (local and local.get("answer")):
            return None

        similarity = float(local.get("similarity") or 0)
        # 双重门槛：相似度 + 题干字符重合度
        if similarity < self.semantic_min_similarity:
            logger.info(
                "[题库] 语义相似度 %.4f 低于 %.2f，不采用",
                similarity, self.semantic_min_similarity,
            )
            return None
        if not self._titles_close(local.get("question"), question_text):
            logger.info(
                "[题库] 题干差异过大，不采用（相似度 %.4f）", similarity
            )
            return None

        if local.get("answer_index") is None:
            local["answer_index"] = answer_index(local.get("answer"), options)
        local["answer_source"] = "local_semantic"
        local["route"] = "bank"
        local.setdefault("matched_question", local.get("question"))
        local.setdefault("matched_options", local.get("options"))
        local.setdefault("matched_number", local.get("number"))
        return local

    def resolve_bank_view(
        self,
        question_text: str,
        options: Any = None,
    ) -> Dict[str, Any]:
        """只做题库匹配，不等待大模型。"""
        return {
            "question_text": question_text,
            "options": options,
            "bank": self._resolve_bank(question_text, options),
            "llm": None,
            "agree": None,
        }

    def _scan_titles(
        self,
        question_text: str,
        options: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """按题干重合和选项重合找题库题。

        现场题常改语序、空格和「是 / 有」。字面精确匹配会落空，
        但选项大体相同的仍是同一题。选项完全不同时只作参考，不采用答案。
        """
        title = normalize_text(re.split(r"\s+A\.\s*", question_text, maxsplit=1)[0])
        if len(title) < 8:
            return None
        live_opts = self._option_norms(options, question_text)
        best = None
        best_ratio = 0.0
        best_overlap = 0.0
        for group in self._exact_index.values():
            for item in group:
                bank_title = normalize_text(item.get("question"))
                if not bank_title:
                    continue
                if abs(len(bank_title) - len(title)) > max(18, int(0.55 * len(title))):
                    continue
                ratio = difflib.SequenceMatcher(None, title, bank_title).ratio()
                if ratio <= best_ratio:
                    continue
                best_ratio = ratio
                best = item
                best_overlap = self._option_overlap(
                    live_opts, self._option_norms(item.get("options"), "")
                )
        if best is None or best_ratio < 0.60:
            return None
        adopted = (best_ratio >= 0.90 and best_overlap >= 0.50) or (
            best_ratio >= 0.78 and best_overlap >= 0.75
        )
        source = "local_semantic" if adopted else "local_semantic_low"
        found = self._format_local(best, round(best_ratio, 4), options, source)
        if not adopted:
            found["explanation"] = (
                f"题干相似度 {best_ratio:.2f}，选项重合 {best_overlap:.2f}，仅供参考"
            )
        return found

    @staticmethod
    def _option_norms(options: Any, question_text: str) -> list:
        """抽出选项正文，用于判断是不是同一题。"""
        values = []
        if isinstance(options, dict):
            values = list(options.values())
        elif isinstance(options, list):
            for item in options:
                if isinstance(item, dict):
                    values.append(item.get("content", item.get("text", "")))
                else:
                    values.append(item)
        if not values and question_text:
            parts = re.split(r"\s+[A-Z]\.\s*", question_text, maxsplit=12)
            values = parts[1:]
        return [normalize_text(value) for value in values if normalize_text(value)]

    @staticmethod
    def _option_overlap(live: list, bank: list) -> float:
        """现场选项有多少能在题库选项里找到。"""
        if not live or not bank:
            return 0.0
        hit = 0
        for text in live:
            if any(
                text == other or (len(text) >= 4 and (text in other or other in text))
                for other in bank
            ):
                hit += 1
        return hit / max(len(live), len(bank))

    def _titles_close(self, matched: Any, current: str) -> bool:
        """匹配到的题干与当前题干是否实质相同。

        用去标点后的字符重合率判断，避免「期货」「期权」这类一字之差
        被当成同一题。
        """
        a = normalize_text(matched)
        b = normalize_text(re.split(r"\s+A\.\s*", str(current or ""), maxsplit=1)[0])
        if not a or not b:
            return False
        if a == b:
            return True
        # 必须是连续子串，不能按「这个字在对方里出现过」计数。
        # 后者会把长题干里的「期货 / 期权」一字之差当成同一题。
        short, long = (a, b) if len(a) <= len(b) else (b, a)
        if short not in long:
            return False
        return len(short) / len(long) >= 0.95

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
            "matched_options": item.get("options"),
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
            "请分析下面的选择题，只返回 JSON，不要返回 Markdown。\n"
            "规则：\n"
            "1. 单选题只填一个字母；多选题把所有正确项的字母连写，按字母升序，"
            "例如 ABD，不要加逗号或空格。\n"
            "2. answer_index 仅在单选题时填 0 开始的序号，多选题固定填 null。\n"
            '3. 返回格式：{"answer":"A","answer_index":0,"explanation":"...","confidence":0.0}\n'
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
        # 降低推理深度：实测默认思考下多选题最慢 8.3 秒，设为 low 后最慢降到 2.6 秒，
        # 而 12 道样例题的准确率保持 12/12。需要更强推理时可用 LLM_REASONING_EFFORT 覆盖。
        effort = config.get("LLM_REASONING_EFFORT", "low")
        if effort:
            payload["reasoning_effort"] = effort
        try:
            started = time.time()
            response = requests.post(
                f"{self.llm_api_base}/chat/completions",
                json=payload,
                headers=headers,
                # 硬上限：超过这个时间就不再等，直接放弃大模型侧。
                timeout=config.get_float("LLM_TIMEOUT", 8.0),
            )
            response.raise_for_status()
            body = response.json()
            elapsed = time.time() - started
            budget = config.get_float("LLM_TIMEOUT", 8.0)
            logger.info("[LLM] 用时 %.2fs（预算 %.0fs）", elapsed, budget)
            content = body["choices"][0]["message"]["content"]
            result = self._parse_json(content)
            if not result.get("answer"):
                return None

            # 归一化答案：大写、只保留字母、去重并按字母升序，多选因此稳定成 ABD 形式
            raw_answer = str(result.get("answer") or "")
            letters = sorted({ch for ch in raw_answer.upper() if "A" <= ch <= "Z"})
            if not letters:
                return None
            result["answer"] = "".join(letters)

            # 序号以字母为准。模型常同时给出正确字母和错误的 answer_index，
            # 保留后者会让面板点到另一个选项。
            if len(letters) == 1:
                derived = answer_index(result["answer"], options)
                if derived is not None:
                    result["answer_index"] = derived
                elif not options:
                    result["answer_index"] = ord(letters[0]) - ord("A")
                else:
                    result["answer_index"] = None
            else:
                result["answer_index"] = None

            result["answer_source"] = "llm_api"
            result["similarity"] = float(result.get("confidence") or 0)
            result["elapsed"] = round(elapsed, 2)
            return result
        except requests.Timeout:
            logger.warning(
                "LLM 超过 %.0fs 未返回，本题只保留题库侧答案",
                config.get_float("LLM_TIMEOUT", 8.0),
            )
            return None
        except (OSError, requests.RequestException, KeyError, ValueError, TypeError) as exc:
            logger.warning("LLM 解析失败: %s", exc)
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
