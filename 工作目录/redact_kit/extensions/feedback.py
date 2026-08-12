"""
Phase 4: 增量学习（用户反馈驱动）
extensions/feedback.py

功能：
1. 收集用户的确认/拒绝/修改决策
2. 存入反馈库（SQLite）
3. 基于反馈调整规则权重和 LLM Prompt
4. 生成个性化规则增强文件
"""

from __future__ import annotations

import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

logger = logging.getLogger("feedback")


# ============================================================
# 反馈数据库
# ============================================================

FEEDBACK_DB = Path(__file__).parent.parent / "feedback.db"


def init_db():
    """初始化反馈数据库"""
    conn = sqlite3.connect(str(FEEDBACK_DB))
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        entity_text TEXT,
        entity_category TEXT,
        original_replacement TEXT,
        user_action TEXT,         -- confirm|reject|modify
        final_replacement TEXT,
        confidence REAL,
        source TEXT,              -- llm|regex|rule
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS learning_stats (
        entity_text TEXT PRIMARY KEY,
        total_confirms INTEGER DEFAULT 0,
        total_rejects INTEGER DEFAULT 0,
        current_replacement TEXT,
        last_updated TEXT
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_feedback_entity
    ON feedback(entity_text)
    """)

    conn.commit()
    conn.close()


class FeedbackCollector:
    """
    用户反馈收集器

    收集人工确认阶段的决策，
    用于增量学习改进脱敏质量。
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(FEEDBACK_DB)
        init_db()

    def record(
        self,
        session_id: str,
        entity_text: str,
        entity_category: str,
        original_replacement: str,
        user_action: str,
        final_replacement: Optional[str] = None,
        confidence: float = 0.0,
        source: str = "llm",
    ):
        """
        记录一条用户反馈

        Args:
            session_id: 会话ID
            entity_text: 实体原文
            entity_category: 实体类别
            original_replacement: 原始替换值
            user_action: confirm | reject | modify
            final_replacement: 最终替换值（confirm/modify时有效）
            confidence: LLM置信度
            source: 来源（llm/regex/rule）
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        try:
            # 插入反馈记录
            c.execute("""
                INSERT INTO feedback
                (session_id, entity_text, entity_category,
                 original_replacement, user_action, final_replacement,
                 confidence, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_id, entity_text, entity_category,
                original_replacement, user_action, final_replacement,
                confidence, source
            ))

            # 更新学习统计
            if user_action == "confirm":
                self._update_stats(c, entity_text, "confirm", final_replacement or original_replacement)
            elif user_action == "reject":
                self._update_stats(c, entity_text, "reject", None)
            elif user_action == "modify":
                self._update_stats(c, entity_text, "modify", final_replacement)

            conn.commit()
            logger.debug(f"反馈已记录: {entity_text!r} → {user_action}")

        finally:
            conn.close()

    def _update_stats(
        self, cursor, entity_text: str,
        action: str, replacement: Optional[str]
    ):
        """更新学习统计表"""
        now = datetime.now().isoformat()

        existing = cursor.execute(
            "SELECT * FROM learning_stats WHERE entity_text = ?",
            (entity_text,)
        ).fetchone()

        if existing:
            confirms = existing[2]
            rejects = existing[3]
            if action == "confirm":
                confirms += 1
            elif action == "reject":
                rejects += 1

            cursor.execute("""
                UPDATE learning_stats
                SET total_confirms = ?, total_rejects = ?,
                    current_replacement = ?, last_updated = ?
                WHERE entity_text = ?
            """, (confirms, rejects, replacement, now, entity_text))
        else:
            confirms = 1 if action == "confirm" else 0
            rejects = 1 if action == "reject" else 0
            cursor.execute("""
                INSERT INTO learning_stats
                (entity_text, total_confirms, total_rejects,
                 current_replacement, last_updated)
                VALUES (?, ?, ?, ?, ?)
            """, (entity_text, confirms, rejects, replacement, now))

    def get_learned_replacements(self) -> dict:
        """
        获取学习到的替换规则

        Returns:
            {
                "replacements": {
                    "蔡昀煜": {"replacement": "XXX", "confidence": 0.99},
                    "海峡银行": {"replacement": "XX银行", "confidence": 0.99},
                    ...
                },
                "rejections": {
                    "客户经理": {"reason": "通用术语", "count": 5},
                    ...
                }
            }
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        replacements = {}
        rejections = {}

        rows = c.execute("SELECT * FROM learning_stats").fetchall()
        for row in rows:
            entity_text = row[0]
            confirms = row[2]
            rejects = row[3]
            current_rep = row[4]

            if rejects > confirms:
                rejections[entity_text] = {
                    "reason": "用户拒绝率 > 50%",
                    "count": rejects,
                }
            else:
                replacements[entity_text] = {
                    "replacement": current_rep,
                    "confidence": confirms / max(confirms + rejects, 1),
                    "confirms": confirms,
                    "rejects": rejects,
                }

        conn.close()
        return {"replacements": replacements, "rejections": rejections}

    def generate_enhanced_rules(self) -> str:
        """
        基于用户反馈生成增强规则文件

        Returns:
            Python 规则代码，可直接追加到 entity_detector.py
        """
        learned = self.get_learned_replacements()

        lines = [
            "# === 增量学习增强规则（自动生成）===",
            "# 生成时间: " + datetime.now().isoformat(),
            "#",
            "# 本文件由用户反馈自动生成，每次人工确认都会更新这些规则。",
            "# 使用方式：将本文件内容追加到 entity_detector.py 的 EXCLUDED_COMMON_WORDS 和 KNOWN_PERSONS",
            "",
        ]

        # 替换规则
        lines.append("# 增强替换规则")
        lines.append("LEARNED_REPLACEMENTS = {")
        for text, info in learned["replacements"].items():
            rep = info["replacement"]
            conf = info["confidence"]
            lines.append(f"    {text!r}: ({rep!r}, {conf:.2f}),  # 确认{info['confirms']}次")
        lines.append("}")
        lines.append("")

        # 拒绝规则
        lines.append("# 增强排除规则（用户拒绝过的项）")
        lines.append("LEARNED_EXCLUDED = {")
        for text, info in learned["rejections"].items():
            lines.append(f"    {text!r}: {info['reason']!r},  # 拒绝{info['count']}次")
        lines.append("}")

        code = "\n".join(lines)

        # 保存到文件
        output_path = Path(__file__).parent.parent / "learned_rules.py"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(code)

        logger.info(f"增强规则已生成: {output_path}")
        return code

    def get_quality_stats(self) -> dict:
        """获取学习统计"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        total = c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        confirms = c.execute(
            "SELECT COUNT(*) FROM feedback WHERE user_action='confirm'"
        ).fetchone()[0]
        rejects = c.execute(
            "SELECT COUNT(*) FROM feedback WHERE user_action='reject'"
        ).fetchone()[0]
        modifies = c.execute(
            "SELECT COUNT(*) FROM feedback WHERE user_action='modify'"
        ).fetchone()[0]

        conn.close()

        return {
            "total_feedbacks": total,
            "confirms": confirms,
            "rejects": rejects,
            "modifies": modifies,
            "confirm_rate": confirms / max(total, 1),
        }
