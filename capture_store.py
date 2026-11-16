"""
抓包结果持久化层。

使用 SQLite 保存：
1. Charles/MCP 原始抓包；
2. 解析后的题目、选项；
3. 本地题库匹配出的答案；
4. 服务器返回的正确性和解析；
5. 用户提交记录。

所有写入都通过本模块完成，避免原来的内存缓存重启后丢失。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


def utc_now() -> str:
    """返回统一的 UTC 时间，便于多设备抓包时排序。"""
    return datetime.now(timezone.utc).isoformat()


def json_text(value: Any) -> str:
    """把任意对象稳定序列化为 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def safe_json(value: Any, default: Any = None) -> Any:
    """尽量解析 JSON 文本，解析失败时返回默认值。"""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class QuizCaptureStore:
    """基于 SQLite 的抓包与题目存储。"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        # 默认把运行数据放入独立目录，不污染原始 Charles Mirror 文件。
        root = os.path.dirname(os.path.abspath(__file__))
        self.db_path = db_path or os.path.join(root, "capture_data", "quiz_capture.db")
        # 测试时允许传入 ":memory:" 或当前目录文件名。
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        """创建线程安全的 SQLite 连接。"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        """初始化表结构和索引。"""
        with closing(self._connect()) as conn:
            with conn:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS captures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    captured_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    method TEXT,
                    url TEXT,
                    host TEXT,
                    path TEXT,
                    status_code INTEGER,
                    request_headers_json TEXT,
                    request_body_json TEXT,
                    response_headers_json TEXT,
                    response_body_json TEXT,
                    metadata_json TEXT
                );

                CREATE TABLE IF NOT EXISTS questions (
                    question_id TEXT PRIMARY KEY,
                    quiz_id TEXT,
                    question_type TEXT,
                    question_title TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    server_analysis TEXT,
                    answer_explanation TEXT,
                    server_correct_index TEXT,
                    server_right_index TEXT,
                    inferred_answer TEXT,
                    inferred_answer_index INTEGER,
                    similarity REAL,
                    answer_source TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    capture_id INTEGER,
                    raw_json TEXT NOT NULL,
                    FOREIGN KEY(capture_id) REFERENCES captures(id)
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    question_id TEXT,
                    quiz_id TEXT,
                    submitted_answer TEXT,
                    server_correct INTEGER,
                    server_analysis TEXT,
                    raw_json TEXT NOT NULL,
                    capture_id INTEGER,
                    FOREIGN KEY(capture_id) REFERENCES captures(id)
                );

                CREATE INDEX IF NOT EXISTS idx_questions_last_seen
                    ON questions(last_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_submissions_question_id
                    ON submissions(question_id);
                    """
                )
                # 兼容早期已经创建的数据库，增量补充服务器判定字段。
                existing_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(questions)").fetchall()
                }
                for column, definition in {
                    "answer_explanation": "TEXT",
                    "server_correct": "INTEGER",
                    "server_answer_index": "TEXT",
                }.items():
                    if column not in existing_columns:
                        conn.execute(
                            f"ALTER TABLE questions ADD COLUMN {column} {definition}"
                        )

    def record_capture(self, envelope: Dict[str, Any], source: str = "charles_mcp") -> int:
        """保存一条抓包信封，按内容指纹自动去重。"""
        request = envelope.get("request") or {}
        response = envelope.get("response") or {}
        metadata = envelope.get("metadata") or {}

        fingerprint_payload = {
            "source": source,
            "request": request,
            "response": response,
            "metadata": metadata,
        }
        fingerprint = hashlib.sha256(json_text(fingerprint_payload).encode("utf-8")).hexdigest()

        with self._lock, closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO captures (
                        fingerprint, captured_at, source, method, url, host, path,
                        status_code, request_headers_json, request_body_json,
                        response_headers_json, response_body_json, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        envelope.get("captured_at") or utc_now(),
                        source,
                        request.get("method"),
                        request.get("url"),
                        request.get("host"),
                        request.get("path"),
                        response.get("status_code"),
                        json_text(request.get("headers") or {}),
                        json_text(request.get("body")),
                        json_text(response.get("headers") or {}),
                        json_text(response.get("body")),
                        json_text(metadata),
                    ),
                )
                row = conn.execute(
                    "SELECT id FROM captures WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
                return int(row["id"])

    def upsert_question(
        self,
        question: Dict[str, Any],
        capture_id: Optional[int] = None,
    ) -> None:
        """新增或更新题目，保留服务器字段和本地匹配字段。"""
        now = utc_now()
        question_id = str(question.get("question_id") or question.get("id") or "")
        if not question_id:
            return

        options = question.get("options") or []
        question_title = str(question.get("question_title") or question.get("question") or "")
        question_text = str(question.get("question_text") or question_title)

        with self._lock, closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO questions (
                    question_id, quiz_id, question_type, question_title,
                    options_json, question_text, server_analysis,
                    answer_explanation,
                    server_correct, server_answer_index, server_correct_index,
                    server_right_index, inferred_answer,
                    inferred_answer_index, similarity, answer_source,
                    first_seen, last_seen, capture_id, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(question_id) DO UPDATE SET
                        quiz_id = COALESCE(excluded.quiz_id, questions.quiz_id),
                        question_type = COALESCE(excluded.question_type, questions.question_type),
                        question_title = COALESCE(NULLIF(excluded.question_title, ''), questions.question_title),
                        options_json = CASE
                            WHEN excluded.options_json = '[]' THEN questions.options_json
                            ELSE excluded.options_json
                        END,
                        question_text = COALESCE(NULLIF(excluded.question_text, ''), questions.question_text),
                    server_analysis = COALESCE(excluded.server_analysis, questions.server_analysis),
                    answer_explanation = COALESCE(
                        excluded.answer_explanation, questions.answer_explanation
                    ),
                    server_correct = COALESCE(excluded.server_correct, questions.server_correct),
                    server_answer_index = COALESCE(
                        excluded.server_answer_index, questions.server_answer_index
                    ),
                    server_correct_index = COALESCE(
                            excluded.server_correct_index, questions.server_correct_index
                        ),
                        server_right_index = COALESCE(
                            excluded.server_right_index, questions.server_right_index
                        ),
                        inferred_answer = COALESCE(excluded.inferred_answer, questions.inferred_answer),
                        inferred_answer_index = COALESCE(
                            excluded.inferred_answer_index, questions.inferred_answer_index
                        ),
                        similarity = COALESCE(excluded.similarity, questions.similarity),
                        answer_source = COALESCE(excluded.answer_source, questions.answer_source),
                        last_seen = excluded.last_seen,
                        capture_id = COALESCE(excluded.capture_id, questions.capture_id),
                        raw_json = excluded.raw_json
                    """,
                    (
                        question_id,
                        question.get("quiz_id"),
                        question.get("type"),
                        question_title,
                        json_text(options),
                    question_text,
                    question.get("server_analysis"),
                    question.get("answer_explanation"),
                    question.get("server_correct"),
                    question.get("server_answer_index"),
                    question.get("server_correct_index"),
                        question.get("server_right_index"),
                        question.get("inferred_answer"),
                        question.get("inferred_answer_index"),
                        question.get("similarity"),
                        question.get("answer_source"),
                        now,
                        now,
                        capture_id,
                        json_text(question.get("raw") or question),
                    ),
                )

    def update_server_result(
        self,
        question_id: Any,
        correct: Any = None,
        answer_index: Any = None,
        analysis: Any = None,
        right_index: Any = None,
    ) -> None:
        """把答案提交后的服务器结果回写到对应题目。"""
        if question_id is None:
            return
        with self._lock, closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE questions
                    SET server_correct = COALESCE(?, server_correct),
                        server_answer_index = COALESCE(?, server_answer_index),
                        server_analysis = COALESCE(?, server_analysis),
                        server_right_index = COALESCE(?, server_right_index),
                        last_seen = ?
                    WHERE question_id = ?
                    """,
                    (
                        correct,
                        answer_index,
                        analysis,
                        right_index,
                        utc_now(),
                        str(question_id),
                    ),
                )

    def record_submission(
        self,
        submission: Dict[str, Any],
        capture_id: Optional[int] = None,
    ) -> None:
        """保存一次答案提交及服务器返回的正确性/解析。"""
        with self._lock, closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO submissions (
                        captured_at, question_id, quiz_id, submitted_answer,
                        server_correct, server_analysis, raw_json, capture_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        submission.get("captured_at") or utc_now(),
                        str(submission.get("question_id") or ""),
                        str(submission.get("quiz_id") or ""),
                        # 选项索引 0 是合法提交（A），不能被 "or" 吞成空串。
                        "" if submission.get("submitted_answer") is None
                        else str(submission.get("submitted_answer")),
                        submission.get("server_correct"),
                        submission.get("server_analysis"),
                        json_text(submission.get("raw") or submission),
                        capture_id,
                    ),
                )

    def list_questions(self, limit: int = 100, offset: int = 0) -> list[Dict[str, Any]]:
        """分页读取已保存题目。"""
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM questions
                ORDER BY last_seen DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._question_row(row) for row in rows]

    def get_question(self, question_id: str) -> Optional[Dict[str, Any]]:
        """按服务器题目 ID 读取题目。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM questions WHERE question_id = ?", (str(question_id),)
            ).fetchone()
        return self._question_row(row) if row else None

    def list_submissions(self, question_id: str) -> list[Dict[str, Any]]:
        """按题目读取提交记录，供答题记录导出使用。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT captured_at, submitted_answer, server_correct,
                       server_analysis, quiz_id
                FROM submissions
                WHERE question_id = ?
                ORDER BY id DESC
                """,
                (str(question_id),),
            ).fetchall()
        return [
            {
                "captured_at": row["captured_at"],
                "submitted_answer": row["submitted_answer"],
                "server_correct": row["server_correct"],
                "server_analysis": row["server_analysis"],
                "quiz_id": row["quiz_id"],
            }
            for row in rows
        ]

    def list_captures(self, limit: int = 100) -> list[Dict[str, Any]]:
        """读取最近抓包元数据，正文按需从数据库返回。"""
        limit = max(1, min(int(limit), 1000))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, fingerprint, captured_at, source, method, url,
                       host, path, status_code, metadata_json
                FROM captures ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "fingerprint": row["fingerprint"],
                "captured_at": row["captured_at"],
                "source": row["source"],
                "method": row["method"],
                "url": row["url"],
                "host": row["host"],
                "path": row["path"],
                "status_code": row["status_code"],
                "metadata": safe_json(row["metadata_json"], {}),
            }
            for row in rows
        ]

    def latest_capture_time(self) -> Optional[str]:
        """返回最近一条抓包的时间，供在线状态判断使用。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT captured_at FROM captures ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["captured_at"] if row else None

    def stats(self) -> Dict[str, int]:
        """返回存储统计信息。"""
        with closing(self._connect()) as conn:
            captures = conn.execute("SELECT COUNT(*) AS n FROM captures").fetchone()["n"]
            questions = conn.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]
            submissions = conn.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"]
        return {
            "captures": int(captures),
            "questions": int(questions),
            "submissions": int(submissions),
        }

    @staticmethod
    def _question_row(row: sqlite3.Row) -> Dict[str, Any]:
        """把数据库行转换为 API 友好的对象。"""
        if row is None:
            return {}
        return {
            "question_id": row["question_id"],
            "quiz_id": row["quiz_id"],
            "type": row["question_type"],
            "question_title": row["question_title"],
            "question_text": row["question_text"],
            "options": safe_json(row["options_json"], []),
            "server_analysis": row["server_analysis"],
            "answer_explanation": row["answer_explanation"],
            "server_correct": row["server_correct"],
            "server_answer_index": row["server_answer_index"],
            "server_correct_index": row["server_correct_index"],
            "server_right_index": row["server_right_index"],
            "answer": row["inferred_answer"],
            "answer_index": row["inferred_answer_index"],
            "similarity": row["similarity"],
            "answer_source": row["answer_source"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "capture_id": row["capture_id"],
            "raw": safe_json(row["raw_json"], {}),
        }
