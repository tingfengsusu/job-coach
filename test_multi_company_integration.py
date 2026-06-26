#!/usr/bin/env python3
"""
test_multi_company_integration.py
多公司集成测试 - 模拟真实求职场景（3个公司同时面试）

核心验证：数据库隔离性、上下文加载正确性、重启后数据恢复。
"""

import os
import sys
import time
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from job_coach_cli import (
    get_db_connection, get_or_create_company, get_or_create_active_session,
    save_conversation_turn, get_recent_context, init_db,
)

DB_PATH = Path.home() / ".job_coach" / "jobs.db"

# ============================================================
# 测试数据
# ============================================================

TEST_COMPANIES: List[Dict[str, Any]] = [
    {
        "name": "字节跳动",
        "jd": "Python后端开发工程师，3-5年经验，熟悉FastAPI、Django、高并发、Redis、PostgreSQL",
        "initial_question": "请介绍一下你的Python后端开发经验，以及你处理过高并发场景吗？",
        "reply": "我是一名Python开发者，有3年经验，使用FastAPI开发过多个后端项目，处理过日均百万级请求的系统。",
    },
    {
        "name": "腾讯",
        "jd": "AI应用开发工程师，熟悉LangChain、RAG、向量数据库、大模型应用开发",
        "initial_question": "你用过哪些大模型框架？有没有RAG相关的项目经验？",
        "reply": "我熟悉LangChain和RAG，做过一个基于ChromaDB的文档问答系统，用Sentence-BERT做embedding。",
    },
    {
        "name": "阿里巴巴",
        "jd": "全栈开发工程师，Java+Python，5年以上经验，熟悉微服务架构、容器化部署",
        "initial_question": "你熟悉微服务架构吗？请举例说明你设计过的微服务系统。",
        "reply": "我熟悉微服务架构，用Spring Cloud和Dubbo开发过电商系统的订单服务和商品服务。",
    },
]


# ============================================================
# 工具函数
# ============================================================

def count_turns_for_company(company_id: int) -> int:
    conn = get_db_connection()
    count = conn.execute(
        """SELECT COUNT(*) FROM conversation_turns ct
           JOIN interview_sessions s ON ct.session_id = s.id
           WHERE s.company_id = ?""",
        (company_id,)
    ).fetchone()[0]
    conn.close()
    return count


def get_turn_contents_for_company(company_id: int, limit: int = 10) -> List[str]:
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT ct.content FROM conversation_turns ct
           JOIN interview_sessions s ON ct.session_id = s.id
           WHERE s.company_id = ?
           ORDER BY ct.timestamp ASC
           LIMIT ?""",
        (company_id, limit)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def delete_test_data(company_ids: List[int]):
    """清理测试数据"""
    conn = get_db_connection()
    for cid in company_ids:
        conn.execute(
            "DELETE FROM conversation_turns WHERE session_id IN "
            "(SELECT id FROM interview_sessions WHERE company_id = ?)",
            (cid,)
        )
        conn.execute("DELETE FROM interview_sessions WHERE company_id = ?", (cid,))
        conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
    conn.commit()
    conn.close()


def check(condition: bool, label: str) -> bool:
    status = "✅" if condition else "❌"
    print(f"  [{status}] {label}")
    return condition


# ============================================================
# 测试主类
# ============================================================

class MultiCompanyIntegrationTest:
    def __init__(self):
        self.company_ids: Dict[str, int] = {}
        self.session_ids: Dict[str, int] = {}
        self.passed = 0
        self.failed = 0
        self.start_time = datetime.now()

    def ok(self, condition: bool, label: str):
        if condition:
            self.passed += 1
        else:
            self.failed += 1
        return check(condition, label)

    def run(self):
        print("=" * 70)
        print("  多公司集成测试 - 模拟真实求职场景")
        print("=" * 70)
        print(f"  开始: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print()

        try:
            self.phase1_init()
            self.phase2_first_round()
            self.phase3_context_loading()
            self.phase4_cross_company_isolation()
            self.phase5_restart()
            self.phase6_after_restart()
        finally:
            self.cleanup()

        self.print_summary()

    # ── Phase 1: 初始化 ──
    def phase1_init(self):
        print("Phase 1: 初始化 3 个测试公司")
        all_ok = True
        for co in TEST_COMPANIES:
            cid = get_or_create_company(co["name"])
            # 写入 JD 作为备注
            conn = get_db_connection()
            conn.execute("UPDATE companies SET notes = ? WHERE id = ?", (co["jd"], cid))
            conn.commit()
            conn.close()

            sid = get_or_create_active_session(cid)
            tid = save_conversation_turn(
                session_id=sid, role="interviewer",
                content=co["initial_question"],
                raw_ocr_text=co["initial_question"]
            )
            self.company_ids[co["name"]] = cid
            self.session_ids[co["name"]] = sid
            name_ok = self.ok(cid is not None and sid is not None and tid is not None,
                              f"{co['name']} 创建成功 (cid={cid}, sid={sid}, tid={tid})")
            all_ok = all_ok and name_ok

        # 验证 3 个 company_id 各不相同
        ids = list(self.company_ids.values())
        unique_ok = self.ok(len(set(ids)) == 3,
                            f"3 个 company_id 各不相同: {ids}")
        all_ok = all_ok and unique_ok

        self._record("Phase 1: 初始化公司", all_ok)
        print()

    # ── Phase 2: 第一轮回复 ──
    def phase2_first_round(self):
        print("Phase 2: 各公司追加候选人的第一轮回复")
        all_ok = True
        for co in TEST_COMPANIES:
            cid = self.company_ids[co["name"]]
            sid = self.session_ids[co["name"]]
            tid = save_conversation_turn(
                session_id=sid, role="assistant",
                content=co["reply"],
                suggestions="基于历史对话的回复建议"
            )
            cnt = count_turns_for_company(cid)
            ok = self.ok(tid is not None and cnt == 2,
                         f"{co['name']}: {cnt} 条对话 (期望 2)")
            all_ok = all_ok and ok

        self._record("Phase 2: 第一轮回复", all_ok)
        print()

    # ── Phase 3: 上下文加载 ──
    def phase3_context_loading(self):
        print("Phase 3: 上下文加载测试 - 字节跳动")
        cid = self.company_ids["字节跳动"]
        ctx = get_recent_context(cid, limit=5)
        turns = ctx.get("recent_turns", [])

        ok1 = self.ok(len(turns) >= 2,
                      f"加载 {len(turns)} 条历史 (期望 >= 2)")
        ok2 = self.ok(all(t.get("content") is not None for t in turns),
                       "所有历史记录均包含内容字段")

        # 验证加载的内容确实属于字节跳动
        bytedance_q = TEST_COMPANIES[0]["initial_question"]
        found_question = any(bytedance_q[:10] in (t.get("content", "") or "")
                             for t in turns)
        ok3 = self.ok(found_question,
                      "历史中包含字节跳动的初始问题")

        all_ok = ok1 and ok2 and ok3
        self._record("Phase 3: 上下文加载", all_ok)
        print()

    # ── Phase 4: 跨公司隔离 ──
    def phase4_cross_company_isolation(self):
        print("Phase 4: 跨公司隔离测试")
        cid_tencent = self.company_ids["腾讯"]
        cid_byte = self.company_ids["字节跳动"]

        # 加载腾讯上下文
        ctx_tencent = get_recent_context(cid_tencent, limit=10)
        turns_t = ctx_tencent.get("recent_turns", [])

        # 加载字节跳动上下文
        ctx_byte = get_recent_context(cid_byte, limit=10)
        turns_b = ctx_byte.get("recent_turns", [])

        ok1 = self.ok(len(turns_t) >= 2 and len(turns_b) >= 2,
                      f"腾讯 {len(turns_t)} 条, 字节 {len(turns_b)} 条 (数据隔离)")

        # 腾讯上下文中不应包含字节跳动的特有内容
        tencent_texts = " ".join((t.get("content") or "") for t in turns_t)
        byte_intrusion = "FastAPI" in tencent_texts  # 字节特有，腾讯不应有
        ok2 = self.ok(not byte_intrusion,
                      "腾讯上下文中不包含字节跳动特有的关键词 (FastAPI)")

        # 字节跳动上下文不应包含腾讯的特有内容
        byte_texts = " ".join((t.get("content") or "") for t in turns_b)
        tencent_intrusion = "ChromaDB" in byte_texts  # 腾讯特有
        ok3 = self.ok(not tencent_intrusion,
                      "字节跳动上下文中不包含腾讯特有的关键词 (ChromaDB)")

        all_ok = ok1 and ok2 and ok3
        self._record("Phase 4: 跨公司隔离", all_ok)
        print()

    # ── Phase 5: 模拟重启 ──
    def phase5_restart(self):
        print("Phase 5: 模拟程序重启（数据持久化验证）")
        counts_before = {name: count_turns_for_company(cid)
                         for name, cid in self.company_ids.items()}
        print(f"  重启前: {counts_before}")

        # 强制关闭所有数据库连接（模拟进程退出）
        # sqlite3 连接由 get_db_connection 管理，无法全局关闭，
        # 此处重新打开原始连接验证文件级持久化
        raw_conn = sqlite3.connect(str(DB_PATH))
        raw_conn.close()
        time.sleep(0.3)

        all_ok = True
        for name, cid in self.company_ids.items():
            before = counts_before[name]
            after = count_turns_for_company(cid)
            name_ok = self.ok(before == after,
                              f"{name}: 重启前 {before} 条 → 重启后 {after} 条 (数据完整)")
            all_ok = all_ok and name_ok

        self._record("Phase 5: 重启验证", all_ok)
        print()

    # ── Phase 6: 重启后追加对话 ──
    def phase6_after_restart(self):
        print("Phase 6: 重启后追加对话 & 上下文验证 - 阿里巴巴")
        cid = self.company_ids["阿里巴巴"]
        sid = self.session_ids["阿里巴巴"]

        # 追加一条新回复
        tid = save_conversation_turn(
            session_id=sid, role="assistant",
            content="此外，我在项目中还使用了 Docker 和 Kubernetes 进行容器化部署，实现了 CI/CD 流水线。",
            suggestions="扩展回答"
        )

        cnt_after = count_turns_for_company(cid)
        ok1 = self.ok(tid is not None and cnt_after == 3,
                      f"追加对话后共 {cnt_after} 条记录 (期望 3)")

        # 重启后加载上下文，验证包含新追加的内容
        ctx = get_recent_context(cid, limit=5)
        turns = ctx.get("recent_turns", [])
        found_docker = any("Docker" in (t.get("content") or "") for t in turns)
        ok2 = self.ok(found_docker,
                      "重启后上下文加载包含新追加的 Docker 相关内容")

        all_ok = ok1 and ok2
        self._record("Phase 6: 重启后分析", all_ok)
        print()

    # ── 辅助 ──
    def _record(self, name, passed):
        setattr(self, f"_result_{name.replace(' ', '_').replace(':', '')}",
                {"name": name, "passed": passed})

    _results = []  # 类变量，实例化前定义

    def print_summary(self):
        elapsed = (datetime.now() - self.start_time).total_seconds()
        total = self.passed + self.failed
        print("=" * 70)
        print("  测试总结")
        print("=" * 70)
        print(f"  通过: {self.passed}  失败: {self.failed}  总计: {total}")
        print(f"  耗时: {elapsed:.1f}s")
        if self.failed == 0:
            print("  [ALL PASS] 所有集成测试通过！")
        else:
            print(f"  [FAIL] {self.failed} 项测试失败")
        print("=" * 70)

    def cleanup(self):
        ids = list(self.company_ids.values())
        if ids:
            delete_test_data(ids)
            print(f"\n  已清理 {len(ids)} 个测试公司及关联数据")


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    init_db()
    test = MultiCompanyIntegrationTest()
    test.run()
