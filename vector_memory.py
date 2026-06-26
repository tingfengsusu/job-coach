#!/usr/bin/env python3
"""
向量记忆模块 — 基于 ChromaDB + sentence-transformers
为每轮面试对话提供语义检索能力

模型下载：
  - 首次运行自动下载 BAAI/bge-small-zh-v1.5（~200MB，约 2 分钟）
  - 国内用户: 设置环境变量 HF_ENDPOINT=https://hf-mirror.com 加速
  - 手动下载: 将模型文件放入 ~/.cache/huggingface/hub/ 对应目录
  - 模型不可用时，系统自动降级为无向量检索模式（不影响基本功能）
"""

import os
import time
from pathlib import Path

# 必须在导入 huggingface_hub 相关模块前设置镜像
if not os.environ.get('HF_ENDPOINT'):
    os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

import chromadb
from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL_NAME = 'BAAI/bge-small-zh-v1.5'


class VectorMemory:
    """向量记忆：存储和检索面试对话的语义向量。模型不可用时自动降级。"""

    def __init__(self, persist_dir: str = None):
        if persist_dir is None:
            persist_dir = str(Path.home() / ".job_coach" / "chroma")
        self.persist_dir = persist_dir
        self.client = chromadb.PersistentClient(path=persist_dir)
        self._model = None
        self._model_load_attempted = False

    @property
    def model(self):
        """延迟加载 embedding 模型。失败时返回 None。"""
        if self._model is None and not self._model_load_attempted:
            self._model_load_attempted = True
            try:
                self._model = SentenceTransformer(EMBEDDING_MODEL_NAME)
            except Exception:
                pass
        return self._model

    @property
    def is_available(self) -> bool:
        """向量模型是否可用"""
        return self.model is not None

    def _get_collection(self, company_id: int):
        """每个公司独立的 collection，避免数据混淆"""
        collection_name = f"company_{company_id}"
        return self.client.get_or_create_collection(name=collection_name)

    def add_turn(self, company_id: int, turn_id: int, content: str, role: str = "interviewer"):
        """将对话轮次向量化并存储。模型不可用时静默跳过。"""
        if not self.is_available:
            return
        if not content or len(content.strip()) < 5:
            return

        try:
            collection = self._get_collection(company_id)
            embedding = self.model.encode(content).tolist()
            collection.add(
                ids=[str(turn_id)],
                embeddings=[embedding],
                metadatas=[{
                    "turn_id": turn_id,
                    "content_preview": content[:200],
                    "role": role,
                    "timestamp": str(time.time())
                }],
                documents=[content[:1000]]
            )
        except Exception:
            pass  # 向量存储失败不影响主流程

    def search_similar(self, company_id: int, query: str, top_k: int = 3) -> list:
        """
        检索与 query 语义相似的对话。
        返回: [{"turn_id": int, "content": str, "similarity": float}, ...]
        模型不可用或结果为空时返回空列表。
        """
        if not self.is_available:
            return []
        if not query or len(query.strip()) < 5:
            return []

        try:
            collection = self._get_collection(company_id)
            if collection.count() == 0:
                return []

            query_embedding = self.model.encode(query).tolist()
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, collection.count()),
                include=["documents", "metadatas", "distances"]
            )

            similar_turns = []
            if results['ids'] and results['ids'][0]:
                for i, turn_id in enumerate(results['ids'][0]):
                    distance = results['distances'][0][i] if results['distances'] else 0
                    similarity = max(0, 1 - distance / 2)
                    similar_turns.append({
                        "turn_id": int(turn_id),
                        "content": results['documents'][0][i] if results['documents'] else "",
                        "similarity": round(similarity, 3)
                    })
            return similar_turns
        except Exception:
            return []

    def delete_company_collection(self, company_id: int):
        """删除公司的所有向量数据"""
        collection_name = f"company_{company_id}"
        try:
            self.client.delete_collection(collection_name)
        except Exception:
            pass

    def rebuild_from_db(self, company_id: int):
        """从 SQLite 重建指定公司的向量索引（用于修复/迁移）"""
        if not self.is_available:
            return 0

        import sqlite3
        db_path = Path.home() / ".job_coach" / "jobs.db"
        if not db_path.exists():
            return 0

        self.delete_company_collection(company_id)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT ct.id, ct.raw_ocr_text, ct.role
               FROM conversation_turns ct
               JOIN interview_sessions s ON ct.session_id = s.id
               WHERE s.company_id = ? AND ct.raw_ocr_text IS NOT NULL
               ORDER BY ct.id""",
            (company_id,)
        ).fetchall()
        conn.close()

        count = 0
        for row in rows:
            if row['raw_ocr_text'] and len(row['raw_ocr_text'].strip()) > 10:
                self.add_turn(company_id, row['id'], row['raw_ocr_text'], row['role'])
                count += 1

        return count
