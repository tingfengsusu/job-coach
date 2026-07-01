#!/usr/bin/env python3
"""
求职辅助 CLI 工具（完整版）
命令：
  init            --resume FILE --save-text    # 初始化简历
  analyze-screenshot --image FILE             # 分析职位截图并保存
  tailor          --job-id ID                 # 生成简历修改建议和话术
  interview       --image FILE                # 分析面试对话截图
  board                                       # 显示职位看板
  feedback        --job-id ID --labels LBL    # 记录反馈（如 "薪资偏低,公司远"）
"""

import os
import sys
import json
import sqlite3
import hashlib
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from pathlib import Path
from typing import List, Dict, Any, Optional

import easyocr
import pygetwindow as gw
import pyperclip
from PIL import Image, ImageGrab, ImageChops
from PIL import ImageDraw as PILDraw
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

try:
    import pystray
    HAS_PYSTRAY = True
except ImportError:
    HAS_PYSTRAY = False

from vector_memory import VectorMemory

load_dotenv()

# Windows 终端 UTF-8 编码支持
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ---------- 配置 ----------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

llm = ChatOpenAI(
    model=DEEPSEEK_MODEL,
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
    temperature=0.2
)

# ---------- OCR 初始化 ----------
reader = easyocr.Reader(['ch_sim', 'en'], gpu=False)

# ---------- 向量记忆 ----------
vector_memory = VectorMemory()

def extract_text_from_image(image_path: str) -> str:
    result = reader.readtext(image_path, detail=0)
    return '\n'.join(result)

# ---------- 数据库 ----------
DB_PATH = Path.home() / ".job_coach" / "jobs.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def get_db_connection():
    """Create a per-call connection (thread-safe)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT,
            title TEXT,
            city TEXT,
            salary TEXT,
            match_score INTEGER,
            analysis TEXT,
            status TEXT DEFAULT 'pending',
            applied_date TEXT,
            feedback_labels TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_preferences (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS interview_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            summary TEXT DEFAULT '',
            FOREIGN KEY (company_id) REFERENCES companies(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'interviewer',
            content TEXT DEFAULT '',
            suggestions TEXT DEFAULT '',
            raw_ocr_text TEXT DEFAULT '',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES interview_sessions(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS job_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            jd_text TEXT DEFAULT '',
            pitfall_assessment TEXT DEFAULT '',
            match_score INTEGER DEFAULT 0,
            strengths TEXT DEFAULT '[]',
            gaps TEXT DEFAULT '[]',
            resume_advice TEXT DEFAULT '',
            self_intro TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS resume_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_analysis_id INTEGER,
            original_resume TEXT DEFAULT '',
            tailored_resume TEXT DEFAULT '',
            changes_summary TEXT DEFAULT '',
            output_path TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_analysis_id) REFERENCES job_analyses(id)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_conversation_turns_session
            ON conversation_turns(session_id)
    ''')
    conn.commit()
    conn.close()

init_db()

def save_job(job_info: Dict[str, Any]):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO jobs (company, title, city, salary, match_score, analysis, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        job_info.get('company', ''),
        job_info.get('title', ''),
        job_info.get('city', ''),
        job_info.get('salary', ''),
        job_info.get('match_score', 0),
        job_info.get('analysis', ''),
        'pending'
    ))
    conn.commit()
    conn.close()
    print(f"✔ 已保存职位 {job_info.get('title')} 到看板")

def add_feedback(job_id: int, labels: List[str]):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 更新当前职位的反馈标签
    cursor.execute('UPDATE jobs SET feedback_labels = ? WHERE id = ?', (json.dumps(labels), job_id))
    # 更新偏好表（累计计数）
    for label in labels:
        key = f'dislike_{label}'
        cursor.execute('''
            INSERT INTO user_preferences (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1
        ''', (key, 1))
    conn.commit()
    conn.close()
    print(f"✔ 已记录反馈：{labels}")

def show_board():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, company, title, match_score, status FROM jobs ORDER BY created_at DESC')
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        print("暂无职位记录。")
        return
    print("\n===== 职位看板 =====")
    for row in rows:
        print(f"[{row[0]}] {row[1]} | {row[2]} | 匹配度: {row[3]}% | 状态: {row[4]}")
    print("==================\n")

def get_or_create_company(name: str) -> int:
    """Insert company if not exists, return company_id."""
    conn = get_db_connection()
    conn.execute("INSERT OR IGNORE INTO companies (name) VALUES (?)", (name,))
    conn.commit()
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row["id"]


def get_or_create_company_from_window_title(window_title: str) -> Optional[int]:
    """从窗口标题提取公司名，自动创建/获取公司ID"""
    if not window_title or len(window_title.strip()) < 2:
        return None

    import re
    title = window_title.strip()

    app_suffixes = [
        'google chrome', 'microsoft edge', 'firefox', 'opera', 'safari',
        'brave', 'chromium', 'internet explorer', 'edge',
        '微信', 'wechat', 'qq', '钉钉', 'dingtalk', '飞书', 'lark', 'feishu',
        'zoom', 'teams', 'slack', 'discord', 'telegram',
        '腾讯会议', 'tencent meeting', 'voov meeting',
        'visual studio code', 'vscode', 'intellij idea',
        'boss直聘', '智联招聘', '猎聘', '拉勾', '51job', '前程无忧', 'linkedin',
        '网易邮箱', 'outlook', 'gmail', 'foxmail',
        # Chrome 多用户配置
        '用户配置', '个人资料', 'profile', 'person',
        '用户', '默认',
    ]

    # 按分隔符拆分
    separators = [' — ', ' - ', ' | ', ' – ', '｜', ' · ', ' • ']
    parts = [title]
    for sep in separators:
        if sep in title:
            parts = [p.strip() for p in title.split(sep)]
            break

    candidates = []
    for p in parts:
        p = p.strip()
        if len(p) < 2 or len(p) > 40:
            continue
        is_app = any(
            p.lower() == a.lower() or p.lower().endswith(' ' + a.lower())
            for a in app_suffixes
        )
        if not is_app:
            candidates.append(p)

    if not candidates:
        cleaned = title
        for app in sorted(app_suffixes, key=len, reverse=True):
            cleaned = re.sub(re.escape(app), '', cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip().strip('- — | ｜ · •').strip()
        if len(cleaned) >= 2:
            candidates = [cleaned]

    if candidates:
        # 优先选择包含公司关键词的
        for kw in ['公司', '科技', '集团', '有限', '技术', '网络', '软件', '信息', '咨询', '教育']:
            for c in candidates:
                if kw in c:
                    return get_or_create_company(c[:30])
        candidates.sort(key=len)
        return get_or_create_company(candidates[0][:30])

    # 兜底：直接用窗口标题作为公司名
    fallback = title.strip()
    if len(fallback) > 30:
        fallback = fallback[:30]
    if len(fallback) >= 2:
        return get_or_create_company(fallback)
    return None


def get_or_create_active_session(company_id: int) -> int:
    """Get active session for company (end_time IS NULL), or create one."""
    conn = get_db_connection()
    row = conn.execute(
        """SELECT id FROM interview_sessions
           WHERE company_id = ? AND end_time IS NULL
           ORDER BY start_time DESC LIMIT 1""",
        (company_id,)
    ).fetchone()
    if row:
        session_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO interview_sessions (company_id) VALUES (?)",
            (company_id,)
        )
        conn.commit()
        session_id = cur.lastrowid
    conn.close()
    return session_id

def _store_vector_async(company_id, turn_id, raw_ocr_text, role):
    """后台异步存储向量，失败不影响主流程"""
    try:
        vector_memory.add_turn(company_id, turn_id, raw_ocr_text, role)
    except Exception:
        pass

def save_conversation_turn(
    session_id: int, role: str, content: str,
    suggestions: str = None, raw_ocr_text: str = None
) -> int:
    """Save a conversation turn. Returns turn_id."""
    conn = get_db_connection()
    cur = conn.execute(
        """INSERT INTO conversation_turns
           (session_id, role, content, suggestions, raw_ocr_text)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, role, content, suggestions, raw_ocr_text)
    )
    conn.commit()
    turn_id = cur.lastrowid

    # 向量存储：仅存储面试官的原始 OCR 文本（内容有意义时）
    if role == 'interviewer' and raw_ocr_text and len(raw_ocr_text.strip()) > 10:
        row = conn.execute(
            "SELECT company_id FROM interview_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row and row[0]:
            threading.Thread(
                target=_store_vector_async,
                args=(row[0], turn_id, raw_ocr_text, role),
                daemon=True
            ).start()

    conn.close()
    return turn_id

def get_recent_context(company_id: int, limit: int = 5, current_query: str = None) -> dict:
    """
    获取公司上下文（最近对话 + 语义相似历史）
    返回: {"recent_turns": list, "similar_turns": list}
    """
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT ct.role, ct.content, ct.suggestions, ct.raw_ocr_text, ct.timestamp
           FROM conversation_turns ct
           JOIN interview_sessions s ON ct.session_id = s.id
           WHERE s.company_id = ? AND s.end_time IS NULL
           ORDER BY ct.timestamp DESC
           LIMIT ?""",
        (company_id, limit)
    ).fetchall()
    conn.close()

    recent_turns = [dict(r) for r in reversed(rows)]

    similar_turns = []
    if current_query and len(current_query.strip()) > 10:
        try:
            similar_turns = vector_memory.search_similar(company_id, current_query, top_k=3)
        except Exception:
            pass

    return {"recent_turns": recent_turns, "similar_turns": similar_turns}


def build_enhanced_prompt(current_question: str, company_name: str,
                          company_notes: str, context: dict) -> str:
    """构建增强提示词，包含语义相似历史和最近对话"""
    parts = [f"你是求职面试教练。你正在帮助用户准备一场真实的面试。\n\n公司: {company_name}"]

    if company_notes:
        parts.append(f"公司备注: {company_notes}")

    similar = context.get("similar_turns", [])
    if similar:
        parts.append("\n## 语义相似的历史问答")
        for i, turn in enumerate(similar, 1):
            parts.append(f"相似问题{i}（相似度{turn.get('similarity', 0)}）: {turn['content'][:200]}")

    recent = context.get("recent_turns", [])
    if recent:
        parts.append("\n## 最近对话上下文")
        for i, turn in enumerate(recent, 1):
            parts.append(f"第{i}轮 - 面试官: {turn.get('raw_ocr_text', turn.get('content', ''))[:300]}")
            if turn.get('suggestions'):
                parts.append(f"第{i}轮 - 回复建议: {turn['suggestions'][:200]}")

    parts.append(f"\n## 面试官最新发言\n{current_question}")
    parts.append(
        '\n根据上面的上下文和面试官的最新发言，输出JSON格式（不要包含其他文字）:\n'
        '{"suggestions": "方案一：\\\\n可直接发送：<完整回复文本，50-150字>\\\\n策略说明：<为什么这样说>\\\\n\\\\n方案二：...（共3个方案，每个含完整回复+策略说明，风格差异化）", '
        '"analysis": "面试官意图分析和当前面试阶段判断"}'
    )

    return '\n'.join(parts)


def end_active_session(company_id: int) -> bool:
    """End all active sessions for a company. Returns True if any ended."""
    conn = get_db_connection()
    cur = conn.execute(
        """UPDATE interview_sessions
           SET end_time = CURRENT_TIMESTAMP
           WHERE company_id = ? AND end_time IS NULL""",
        (company_id,)
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0

def _safe_json_dumps(obj, default="[]"):
    """安全地将对象序列化为 JSON 字符串，转换失败时返回默认值"""
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        print(f"[JSON] 序列化失败: {e}, 使用默认值")
        return default


def _safe_json_loads(text, default=None):
    """安全地解析 JSON 字符串，解析失败时返回默认值"""
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"[JSON] 解析失败: {e}, 使用默认值")
        return default


def save_job_analysis(
    company_id: int, jd_text: str, pitfall_assessment: str,
    match_score: int, strengths: list, gaps: list,
    resume_advice: str, self_intro: str
) -> int:
    """保存岗位分析结果，返回 analysis_id"""
    # ── 类型检查和转换 ──
    print(f"[DB] save_job_analysis 参数类型: "
          f"company_id={type(company_id).__name__}, "
          f"jd_text={type(jd_text).__name__}, "
          f"pitfall_assessment={type(pitfall_assessment).__name__}, "
          f"match_score={type(match_score).__name__}, "
          f"strengths={type(strengths).__name__}, "
          f"gaps={type(gaps).__name__}, "
          f"resume_advice={type(resume_advice).__name__}, "
          f"self_intro={type(self_intro).__name__}")

    # company_id: 确保是 int
    if not isinstance(company_id, int):
        company_id = int(company_id)

    # jd_text: 确保是 str
    if not isinstance(jd_text, str):
        jd_text = str(jd_text) if jd_text else ""

    # pitfall_assessment: 确保是 str
    if not isinstance(pitfall_assessment, str):
        pitfall_assessment = str(pitfall_assessment) if pitfall_assessment else ""

    # match_score: 确保是 int
    if not isinstance(match_score, int):
        try:
            match_score = int(match_score)
        except (ValueError, TypeError):
            match_score = 0

    # strengths: 确保是 list，然后转 JSON 字符串
    if not isinstance(strengths, list):
        strengths = [str(strengths)] if strengths else []
    print(f"[DB]   strengths: list[{len(strengths)}] = {[str(s)[:40] for s in strengths[:3]]}")

    # gaps: 确保是 list，然后转 JSON 字符串
    if not isinstance(gaps, list):
        gaps = [str(gaps)] if gaps else []
    print(f"[DB]   gaps: list[{len(gaps)}] = {[str(g)[:40] for g in gaps[:3]]}")

    # resume_advice: list → '\n'.join(), None → '', 确保是 str
    if isinstance(resume_advice, list):
        print(f"[DB]   resume_advice 是 list(len={len(resume_advice)})，转为字符串")
        resume_advice = '\n'.join(str(s) for s in resume_advice)
    elif resume_advice is None:
        resume_advice = ""
    elif not isinstance(resume_advice, str):
        resume_advice = str(resume_advice)
    print(f"[DB]   resume_advice: str[{len(resume_advice)}] = {resume_advice[:60]}...")

    # self_intro: list → '\n'.join(), None → '', 确保是 str
    if isinstance(self_intro, list):
        print(f"[DB]   self_intro 是 list(len={len(self_intro)})，转为字符串")
        self_intro = '\n'.join(str(s) for s in self_intro)
    elif self_intro is None:
        self_intro = ""
    elif not isinstance(self_intro, str):
        self_intro = str(self_intro)
    print(f"[DB]   self_intro: str[{len(self_intro)}] = {self_intro[:60]}...")

    strengths_json = _safe_json_dumps(strengths)
    gaps_json = _safe_json_dumps(gaps)

    conn = get_db_connection()
    cur = conn.execute(
        """INSERT INTO job_analyses
           (company_id, jd_text, pitfall_assessment, match_score,
            strengths, gaps, resume_advice, self_intro)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (company_id, jd_text, pitfall_assessment, match_score,
         strengths_json, gaps_json,
         resume_advice, self_intro)
    )
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    print(f"[DB] save_job_analysis 完成: company_id={company_id}, analysis_id={aid}, "
          f"match_score={match_score}, strengths={len(strengths)}, gaps={len(gaps)}")
    return aid


def get_job_analysis(analysis_id: int) -> Optional[dict]:
    """读取岗位分析记录，将 JSON 字段反序列化为 Python 对象"""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM job_analyses WHERE id = ?", (analysis_id,)
    ).fetchone()
    conn.close()

    if not row:
        print(f"[DB] get_job_analysis: analysis_id={analysis_id} 未找到")
        return None

    return {
        "id": row["id"],
        "company_id": row["company_id"],
        "jd_text": row["jd_text"] or "",
        "pitfall_assessment": row["pitfall_assessment"] or "",
        "match_score": row["match_score"] or 0,
        "strengths": _safe_json_loads(row["strengths"], []),
        "gaps": _safe_json_loads(row["gaps"], []),
        "resume_advice": row["resume_advice"] or "",
        "self_intro": row["self_intro"] or "",
        "created_at": row["created_at"],
    }

def save_resume_version(
    job_analysis_id: int, original_resume: str,
    tailored_resume: str, changes_summary: str, output_path: str
) -> int:
    """保存简历版本，返回 version_id"""
    conn = get_db_connection()
    cur = conn.execute(
        """INSERT INTO resume_versions
           (job_analysis_id, original_resume, tailored_resume,
            changes_summary, output_path)
           VALUES (?, ?, ?, ?, ?)""",
        (job_analysis_id, original_resume, tailored_resume,
         changes_summary, output_path)
    )
    conn.commit()
    vid = cur.lastrowid
    conn.close()
    return vid

def load_resume_text(config_path: str = None) -> Optional[str]:
    """从文件加载简历文本"""
    default_path = Path.home() / ".job_coach" / "resume.md"
    path = Path(config_path) if config_path else default_path
    if path.exists():
        return path.read_text(encoding='utf-8')
    # 回退到数据库中的旧版简历
    conn = get_db_connection()
    row = conn.execute(
        "SELECT value FROM user_preferences WHERE key='original_resume_text'"
    ).fetchone()
    conn.close()
    if row:
        return row["value"]
    return None

# ---------- 核心功能 ----------
def analyze_resume(resume_text: str) -> Dict[str, Any]:
    system_prompt = """你是求职顾问。分析简历，输出JSON格式：
{
  "tech_tags": ["标签1", "标签2", "标签3"],
  "recommended_job_titles": ["职位1", "职位2", "职位3"]
}
不要包含其他文字。"""
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"简历：{resume_text}")
    ]
    response = llm.invoke(messages)
    try:
        return json.loads(response.content)
    except:
        return {"tech_tags": [], "recommended_job_titles": ["后端开发", "软件开发"]}

def match_job(resume_text: str, jd_text: str) -> Dict[str, Any]:
    system_prompt = """你是专业职业顾问。根据用户简历和职位描述，输出JSON：
{
  "match_score": 0-100,
  "strengths": ["优势1","优势2"],
  "improvements": ["待提升1","待提升2"],
  "risk_keywords": ["外包","临时","派遣"]中命中的词（若无则空数组）
}
不要解释其他。"""
    user_content = f"简历：{resume_text}\n职位描述：{jd_text}"
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content)
    ]
    response = llm.invoke(messages)
    try:
        return json.loads(response.content)
    except:
        return {"match_score": 0, "strengths": [], "improvements": [], "risk_keywords": []}

def extract_job_details(jd_text: str) -> Dict[str, str]:
    prompt = f"""
从以下职位描述中提取信息，输出 JSON 格式，不要包含其他文字。如果某一项信息完全缺失，则对应值为空字符串。
{{
  "company": "公司名称（如“字节跳动”）",
  "title": "职位名称（如“Python后端开发”）",
  "city": "工作城市（如“上海”）",
  "salary": "薪资范围（如“20-30K·14薪”）"
}}

职位描述：
{jd_text[:1500]}
"""
    messages = [HumanMessage(content=prompt)]
    response = llm.invoke(messages)
    content = response.content.strip()
    if content.startswith("```json"):
        content = content[7:]
    if content.endswith("```"):
        content = content[:-3]
    try:
        return json.loads(content)
    except:
        return {"company": "", "title": "", "city": "", "salary": ""}

def analyze_screenshot_core(image_path: str, company_id: int = None,
                           window_title: str = None,
                           use_vision: bool = False) -> dict:
    """
    Core screenshot analysis: OCR → LLM → DB (or vision mode).
    Auto-detects company from window title when company_id is not provided.
    Returns: {"success": bool, "ocr_text": str, "suggestions": str,
              "analysis": str, "turn_id": int|None}
    """
    result = {
        "success": False, "ocr_text": "", "suggestions": "",
        "analysis": "", "turn_id": None, "company_id": None
    }

    if not os.path.exists(image_path):
        return result

    # ── 多模态分析路径 ──
    vision_succeeded = False
    if use_vision:
        try:
            from vision_analyzer import analyze_interview_with_vision
            print("[Vision] 多模态面试分析中...")
            vision_result = analyze_interview_with_vision(image_path)
            if vision_result.get("success"):
                result["suggestions"] = vision_result.get("suggestions", "")
                result["analysis"] = vision_result.get("analysis", "")
                result["ocr_text"] = ""
                vision_succeeded = True
                print(f"[Vision] 面试分析成功")
            else:
                print(f"[降级] 多模态分析失败: {vision_result.get('error')}")
        except Exception as e:
            print(f"[降级] 多模态分析异常: {e}")

    if not vision_succeeded and use_vision:
        print("[降级] 多模态未成功，回退到 OCR + LLM 方案")

    ocr_text = ""
    if not vision_succeeded:
        ocr_text = extract_text_from_image(image_path)
        if not ocr_text.strip():
            return result
        result["ocr_text"] = ocr_text

    # 自动检测公司：当未传入 company_id 时，从窗口标题提取
    if company_id is None and window_title:
        company_id = get_or_create_company_from_window_title(window_title)
    if company_id is None:
        try:
            active_win = gw.getActiveWindow()
            if active_win and active_win.title:
                company_id = get_or_create_company_from_window_title(active_win.title)
        except Exception:
            pass
    # 最终兜底：确保分析结果能保存
    if company_id is None:
        company_id = get_or_create_company("未命名公司")

    if not vision_succeeded:
        if company_id is not None:
            conn = get_db_connection()
            company_row = conn.execute(
                "SELECT name, notes FROM companies WHERE id = ?", (company_id,)
            ).fetchone()
            conn.close()

            company_name = company_row["name"] if company_row else "未知公司"
            company_notes = company_row["notes"] if company_row else ""

            context = get_recent_context(company_id, limit=5, current_query=ocr_text)
            system_prompt = build_enhanced_prompt(
                ocr_text, company_name, company_notes, context
            )
        else:
            system_prompt = """你是求职面试教练。分析面试官的最新发言（从聊天截图OCR识别），输出JSON格式（不要包含其他文字）:
{
  "suggestions": "三个完整回复方案，每个包含可直接发送的完整回复文本+策略说明，用换行分隔。格式：方案一：\\n可直接发送：xxx\\n策略说明：xxx\\n\\n方案二：...",
  "analysis": "面试官意图分析和建议的反问问题"
}"""

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"面试官发言（OCR识别）:\n{ocr_text}")
        ]

        try:
            response = llm.invoke(messages)
            content = response.content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.endswith("```"):
                content = content[:-3]
            parsed = json.loads(content)
        except Exception:
            fallback = ""
            try:
                fallback = response.content[:500]
            except Exception:
                fallback = "LLM调用失败"
            parsed = {
                "suggestions": "无法解析LLM响应",
                "analysis": fallback
            }

        suggestions = parsed.get("suggestions", "")
        if isinstance(suggestions, list):
            suggestions = "\n".join(f"{i+1}. {s}" for i, s in enumerate(suggestions))
        result["suggestions"] = suggestions
        result["analysis"] = parsed.get("analysis", "")

    if company_id is not None:
        try:
            print(f"[分析] 保存面试分析: company_id={company_id}")
            session_id = get_or_create_active_session(company_id)
            turn_id = save_conversation_turn(
                session_id=session_id,
                role="interviewer",
                content=result["analysis"],
                suggestions=result["suggestions"],
                raw_ocr_text=ocr_text
            )
            result["turn_id"] = turn_id
            print(f"[分析] 面试分析已保存: turn_id={turn_id}, session_id={session_id}")
        except Exception as e:
            print(f"警告: 保存对话记录失败 - {e}")
            import traceback
            traceback.print_exc()
    else:
        print("[分析] 警告: company_id 为 None，无法保存面试分析")

    result["company_id"] = company_id
    result["success"] = True
    return result


def _call_llm_json(prompt: str, max_tokens: int = 1024) -> dict:
    """Helper: call LLM with a JSON-output prompt, return parsed dict or empty dict."""
    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        content = response.content.strip()
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        return json.loads(content.strip())
    except Exception as e:
        print(f"[LLM JSON] 解析失败: {e}")
        return {}


def analyze_screenshot_core_with_feedback(
    image_path: str, company_id: int = None,
    window_title: str = None, use_vision: bool = False
) -> dict:
    """
    OCR+LLM 面试分析 + 面试官视角评估 + 优化建议。
    先调用 analyze_screenshot_core 获取原始建议，再进行评估和优化。
    """
    # Step 1: 原始分析
    result = analyze_screenshot_core(
        image_path, company_id=company_id,
        window_title=window_title, use_vision=use_vision
    )
    if not result.get("success"):
        return result

    suggestions_text = result.get("suggestions", "")
    analysis = result.get("analysis", "")

    # Step 2: 面试官视角评估
    assess_prompt = f"""你是资深面试官。评估以下面试回答建议：

面试官问题分析：{analysis[:300]}
回答建议：{suggestions_text}

从面试官视角评估这个回答（语气是否自信得体、逻辑是否清晰直接、深度是否足够有案例支撑），输出严格JSON（不要markdown包裹）：
{{
  "assessment": "总体评价（一句话，50字以内）",
  "score": 0到100的整数,
  "strengths": ["优点1", "优点2"],
  "weaknesses": ["问题1", "问题2"]
}}"""

    perspective = _call_llm_json(assess_prompt)
    if not perspective:
        perspective = {
            "assessment": "评估失败",
            "score": 0,
            "strengths": [],
            "weaknesses": []
        }

    # Step 3: 优化建议
    optimize_prompt = f"""你是面试辅导专家。根据面试官评估优化回复方案：

原始回复方案：{suggestions_text}
面试官评估：{json.dumps(perspective, ensure_ascii=False)}

请输出优化后的回复方案，严格JSON（不要markdown包裹）：
{{
  "optimized_suggestions": "优化后的3个完整回复方案，每个包含可直接发送的回复文本+策略说明，用换行分隔。格式同原始方案"
}}"""

    optimized = _call_llm_json(optimize_prompt)
    optimized_suggestions = optimized.get("optimized_suggestions", suggestions_text)

    result["original_suggestions"] = suggestions_text
    result["interviewer_perspective"] = perspective
    result["optimized_suggestions"] = optimized_suggestions
    return result


def detect_content_type(ocr_text: str) -> str:
    """使用 LLM 判断内容是岗位 JD 还是面试对话。返回 'job' 或 'interview'"""
    prompt = f"""判断以下文本是岗位JD还是面试对话。只输出一个单词：job 或 interview

文本：
{ocr_text[:1000]}"""
    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        result = response.content.strip().lower()
        if 'job' in result:
            return 'job'
        return 'interview'
    except Exception:
        # 回退：基于关键词简单判断
        jd_keywords = ['职位描述', '岗位职责', '任职要求', '薪资', '五险一金', '经验', '学历']
        score = sum(1 for kw in jd_keywords if kw in ocr_text)
        return 'job' if score >= 2 else 'interview'


def detect_scene_lightweight(image_path: str) -> str:
    """
    轻量级场景判断：只读取图片顶部 1/3 区域 + 关键词匹配，不调用 LLM。
    返回: 'job' 或 'interview'
    """
    from pathlib import Path
    from PIL import Image

    try:
        img = Image.open(image_path)
        w, h = img.size
        crop_top = img.crop((0, 0, w, max(h // 3, 100)))

        temp_crop = str(Path(image_path).parent / "_temp_crop.png")
        crop_top.save(temp_crop)

        result = reader.readtext(temp_crop, detail=0)
        text = ' '.join(result)[:300]

        try:
            Path(temp_crop).unlink()
        except Exception:
            pass
    except Exception as e:
        print(f"[场景判断] 轻量OCR失败: {e}")
        return 'interview'

    job_keywords = ['职位描述', '岗位职责', '任职要求', 'JD', '招聘', '薪资范围',
                    '岗位', '职位', '薪资', '待遇', '福利', '五险一金', '学历要求']
    score = sum(1 for kw in job_keywords if kw in text)
    scene = 'job' if score >= 2 else 'interview'
    print(f"[场景判断] 轻量级 → {scene} (关键词命中: {score}, 文本预览: {text[:80]})")
    return scene


def analyze_job_screenshot(image_path: str, company_id: int = None,
                           original_resume: str = None,
                           window_title: str = None,
                           use_vision: bool = False) -> dict:
    """分析岗位 JD 截图，use_vision=True 时使用多模态模型跳过 OCR"""
    result = {
        "success": False, "ocr_text": "", "pitfall_assessment": "",
        "match_score": 0, "strengths": [], "gaps": [],
        "resume_advice": "", "self_intro": "", "analysis_id": None,
        "company_id": None
    }

    if not os.path.exists(image_path):
        return result

    # ── 多模态分析路径 ──
    vision_result = None
    if use_vision:
        try:
            from vision_analyzer import analyze_job_with_vision
            print("[Vision] 多模态岗位分析中...")
            vision_result = analyze_job_with_vision(image_path, original_resume)
            if vision_result.get("success"):
                result["pitfall_assessment"] = str(vision_result.get("pitfall_assessment", ""))
                try:
                    result["match_score"] = int(vision_result.get("match_score", 0))
                except (ValueError, TypeError):
                    result["match_score"] = 0
                result["strengths"] = vision_result.get("strengths", [])
                if not isinstance(result["strengths"], list):
                    result["strengths"] = []
                result["gaps"] = vision_result.get("gaps", [])
                if not isinstance(result["gaps"], list):
                    result["gaps"] = []
                result["resume_advice"] = str(vision_result.get("resume_advice", ""))
                result["self_intro"] = str(vision_result.get("self_intro", ""))
                result["ocr_text"] = ""  # 视觉模式无 OCR 文本
                print(f"[Vision] 岗位分析成功: match_score={result['match_score']}")
            else:
                print(f"[降级] 多模态分析失败: {vision_result.get('error')}")
        except Exception as e:
            print(f"[降级] 多模态分析异常: {e}")

    # 判断多模态是否成功：只要 API 返回 success 且无 error 字段即视为成功
    # match_score=0 是合法值，pitfall_assessment 可能为空字符串
    vision_succeeded = (
        vision_result is not None
        and vision_result.get("success")
        and "error" not in vision_result
    )
    if use_vision and not vision_succeeded:
        print("[降级] 多模态未成功，回退到 OCR + LLM 方案")

    # ── OCR 路径（use_vision=False 或 vision 失败时走这里）─
    ocr_text = ""
    if not vision_succeeded:
        ocr_text = extract_text_from_image(image_path)
        if not ocr_text.strip():
            result["pitfall_assessment"] = "无法识别图片中的文字，请重新截图。"
            return result
        if len(ocr_text.strip()) < 10:
            result["ocr_text"] = ocr_text
            result["pitfall_assessment"] = (
                f"识别文字过少（仅{len(ocr_text.strip())}字符），"
                "请截取包含完整岗位描述的区域。"
            )
            return result
        result["ocr_text"] = ocr_text

    # 自动检测公司：当未传入 company_id 时，从窗口标题提取
    if company_id is None and window_title:
        company_id = get_or_create_company_from_window_title(window_title)
    if company_id is None:
        try:
            active_win = gw.getActiveWindow()
            if active_win and active_win.title:
                company_id = get_or_create_company_from_window_title(active_win.title)
        except Exception:
            pass
    # 最终兜底：确保分析结果能保存
    if company_id is None:
        company_id = get_or_create_company("未命名公司")

    # 加载公司上下文
    company_name = "未知公司"
    company_notes = ""
    if company_id is not None:
        conn = get_db_connection()
        company_row = conn.execute(
            "SELECT name, notes FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        conn.close()
        if company_row:
            company_name = company_row["name"]
            company_notes = company_row["notes"] or ""

    if not vision_succeeded:
        resume_block = ""
        if original_resume:
            resume_block = "\n\n候选人简历：\n" + original_resume[:2500]

        # 用字符串拼接而非 f-string，避免 OCR 文本中的 {} 被误解析
        system_prompt = (
            "你是资深职业顾问。分析以下岗位JD，输出严格JSON格式（不要包含其他文字）。\n\n"
            + "【强制要求】以下所有字段都必须输出，即使没有信息也要输出默认值：\n"
            + "- pitfall_assessment: 你看到的文本就是JD，基于JD内容做坑位评估（加班文化、外包、薪资模糊等），禁止说\"JD缺失\"或\"无法分析\"\n"
            + "- match_score: 必须有0-100的整数\n"
            + "- strengths: 必须输出数组，至少2条\n"
            + "- gaps: 必须输出数组，至少2条\n"
            + "- resume_advice: 必须有简历修改建议\n"
            + "- self_intro: 必须有自荐话术\n\n"
            + "公司：" + company_name + "\n"
            + "公司备注：" + company_notes + resume_block + "\n\n"
            + "岗位JD（OCR识别）：\n" + ocr_text[:3000] + "\n\n"
            + "输出JSON：\n"
            + "{\n"
            + '  "pitfall_assessment": "坑位评估文字，无明显坑位则写\\"无明显坑位\\"",\n'
            + '  "match_score": 75,\n'
            + '  "strengths": ["匹配项1", "匹配项2", "匹配项3"],\n'
            + '  "gaps": ["缺口项1", "缺口项2", "缺口项3"],\n'
            + '  "resume_advice": "简历修改建议，3-5条用\\n分隔",\n'
            + '  "self_intro": "您好，看到贵司在招XX岗位，我有X年XX经验，熟悉JD中提到的XX和XX，做过XX项目，期待有机会沟通。"\n'
            + "}\n\n"
            + "注意：self_intro 必须提到JD中的具体技术栈，语气自然不做作。缺少任何字段都会导致错误。"
        )

        parsed = None
        raw = ""
        try:
            response = llm.invoke([HumanMessage(content=system_prompt)])
            content = response.content.strip()
            raw = content
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            parsed = json.loads(content.strip())
        except json.JSONDecodeError as e:
            print(f"[分析] JSON解析失败: {e}")
            if raw and not raw.rstrip().endswith('}'):
                print("[分析] JSON可能被截断（缺少闭合括号），尝试重新生成...")
                retry_prompt = system_prompt + (
                    "\n\n【重要】上一次输出被截断或格式错误。"
                    "请精简输出，确保match_score、strengths、gaps字段完整，输出严格JSON。"
                )
                try:
                    response2 = llm.invoke([HumanMessage(content=retry_prompt)])
                    content2 = response2.content.strip()
                    if content2.startswith("```json"):
                        content2 = content2[7:]
                    elif content2.startswith("```"):
                        content2 = content2[3:]
                    if content2.endswith("```"):
                        content2 = content2[:-3]
                    parsed = json.loads(content2.strip())
                    print("[分析] 重新生成成功")
                except Exception:
                    pass
        except Exception as e:
            print(f"[分析] LLM调用异常: {e}")

        if parsed is None:
            parsed = {}

        # 自动补全缺失字段（vision_analyzer 中同样的保护逻辑）
        from vision_analyzer import _ensure_job_analysis_fields
        parsed = _ensure_job_analysis_fields(parsed)

        result["pitfall_assessment"] = str(parsed.get("pitfall_assessment", ""))
        try:
            result["match_score"] = int(parsed.get("match_score", 0))
        except (ValueError, TypeError):
            result["match_score"] = 0
        result["strengths"] = parsed.get("strengths", [])
        if not isinstance(result["strengths"], list):
            result["strengths"] = []
        result["gaps"] = parsed.get("gaps", [])
        if not isinstance(result["gaps"], list):
            result["gaps"] = []
        result["resume_advice"] = str(parsed.get("resume_advice", ""))
        result["self_intro"] = str(parsed.get("self_intro", ""))

        # 类型安全检查：resume_advice 和 self_intro 必须是字符串
        if isinstance(result["resume_advice"], list):
            print(f"[分析] resume_advice 是 list，转为字符串 (len={len(result['resume_advice'])})")
            result["resume_advice"] = '\n'.join(str(s) for s in result["resume_advice"])
        elif not isinstance(result["resume_advice"], str):
            result["resume_advice"] = str(result["resume_advice"]) if result["resume_advice"] else ""

        if isinstance(result["self_intro"], list):
            print(f"[分析] self_intro 是 list，转为字符串 (len={len(result['self_intro'])})")
            result["self_intro"] = '\n'.join(str(s) for s in result["self_intro"])
        elif not isinstance(result["self_intro"], str):
            result["self_intro"] = str(result["self_intro"]) if result["self_intro"] else ""

    # 保存到数据库
    if company_id is not None:
        try:
            print(f"[分析] 保存岗位分析: company_id={company_id}, "
                  f"match_score={result['match_score']}, "
                  f"strengths={len(result['strengths'])}, gaps={len(result['gaps'])}")
            aid = save_job_analysis(
                company_id=company_id,
                jd_text=ocr_text,
                pitfall_assessment=result["pitfall_assessment"],
                match_score=result["match_score"],
                strengths=result["strengths"],
                gaps=result["gaps"],
                resume_advice=result["resume_advice"],
                self_intro=result["self_intro"]
            )
            result["analysis_id"] = aid
            print(f"[分析] 岗位分析已保存: analysis_id={aid}, company_id={company_id}")
        except Exception as e:
            print(f"警告: 保存岗位分析失败 - {e}")
            import traceback
            traceback.print_exc()
    else:
        print("[分析] 警告: company_id 为 None，无法保存岗位分析")

    result["company_id"] = company_id
    result["success"] = True
    return result


def fill_resume_placeholders(resume_text: str,
                             name: str = None,
                             phone: str = None,
                             email: str = None) -> str:
    """将简历中的占位符替换为用户真实信息。若未传入参数则从 config.json 读取。"""
    if name is None or phone is None or email is None:
        config_path = Path.home() / ".job_coach" / "config.json"
        user_cfg = {}
        if config_path.exists():
            try:
                user_cfg = json.loads(config_path.read_text(encoding='utf-8'))
            except Exception:
                pass
        name = name or user_cfg.get("user_name", "")
        phone = phone or user_cfg.get("user_phone", "")
        email = email or user_cfg.get("user_email", "")

    replacements = {
        "你的姓名": name, "你的名字": name,
        "您的姓名": name, "您的名字": name,
        "你的手机号": phone, "你的电话": phone, "你的手机": phone,
        "您的手机号": phone, "您的电话": phone, "您的手机": phone,
        "你的邮箱": email, "你的电子邮件": email,
        "您的邮箱": email, "您的电子邮件": email,
        "[手机号]": phone, "[电话]": phone,
        "[邮箱]": email, "[姓名]": name,
    }

    result = resume_text
    for placeholder, real_value in replacements.items():
        if real_value:
            result = result.replace(placeholder, real_value)

    # 清理残留的空占位符（值为空的 key）
    for placeholder, real_value in replacements.items():
        if not real_value:
            result = result.replace(placeholder, "")

    # 清理连续多余空行（3+ 空行 → 2 空行）
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")

    return result.strip()


def tailor_resume(job_analysis_id: int, original_resume_path: str = None) -> dict:
    """根据岗位分析结果，生成修改后的 Markdown 简历"""
    result = {
        "success": False, "changes_summary": "",
        "tailored_resume": "", "output_path": ""
    }

    # 加载岗位分析
    analysis = get_job_analysis(job_analysis_id)
    if not analysis:
        result["changes_summary"] = "未找到岗位分析记录"
        return result

    # 加载简历
    resume_text = load_resume_text(original_resume_path)
    if not resume_text:
        result["changes_summary"] = "未找到简历文本，请在设置中指定简历路径"
        return result

    # 替换简历中的占位符为用户真实信息
    resume_text = fill_resume_placeholders(resume_text)

    strengths = analysis["strengths"]
    gaps = analysis["gaps"]
    advice = analysis["resume_advice"]
    jd_text = analysis.get("jd_text", "")

    TAILOR_PROMPT = f"""你是简历优化专家。根据岗位 JD 和候选人原始简历，生成修改后的 Markdown 简历。

【严格要求 - 必须遵守】
1. 原始简历中的个人信息（姓名、电话、邮箱、地址、毕业院校）已经正确填写，你必须原样保留这些信息，一个字都不许改
2. 只调整以下内容：技能描述、项目经历描述、关键词排序、成果量化表述
3. 输出完整的简历，不要省略任何段落
4. 禁止使用任何占位符（如 "你的姓名"、"你的电话"、"你的邮箱"、"XXX"、"[待补充]"），简历中出现的所有个人信息必须是原始简历中的真实内容

【修改重点】
- 根据岗位 JD 的要求，在项目经验中补充或强化 JD 中的关键词
- 调整技能清单排序，把岗位明确要求的技能放在最前面
- 量化项目成果（如"将 QPS 从 500 提升到 2000"、"减少 30% 响应延迟"）
- 针对缺口项，用现有经验的侧面来描述相关能力
- 保持原简历的结构和 Markdown 格式

【输出格式】严格 JSON（不要 markdown 包裹）：
{{
  "changes_summary": "具体说明修改了哪些地方（3-5条）",
  "tailored_resume": "修改后的完整简历（个人信息原样保留，技能和项目描述已根据 JD 优化）"
}}

【岗位 JD 原文】
{jd_text if jd_text else '未记录'}

【匹配分析】
- 符合项：{', '.join(strengths) if strengths else '无'}
- 缺口项：{', '.join(gaps) if gaps else '无'}
- 简历建议：{advice}

【用户原始简历（个人信息已确认无误）】
{resume_text}"""

    try:
        response = llm.invoke([HumanMessage(content=TAILOR_PROMPT)])
        content = response.content.strip()
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        parsed = json.loads(content.strip())
    except Exception:
        parsed = {
            "changes_summary": "LLM响应解析失败",
            "tailored_resume": ""
        }

    # ── 验证：检查生成简历中是否有占位符 ──
    tailored = parsed.get("tailored_resume", "")
    placeholder_keywords = ["你的电话", "你的姓名", "你的手机", "你的邮箱",
                            "XXX", "[待补充]", "[请填写]", "【待补充】"]
    found_placeholders = [kw for kw in placeholder_keywords if kw in tailored]

    if found_placeholders and tailored:
        print(f"[Tailor] 检测到占位符: {found_placeholders}，尝试重新生成...")
        retry_prompt = TAILOR_PROMPT + (
            f"\n\n【重要警告】上一次生成的简历中出现了占位符：{', '.join(found_placeholders)}。"
            "请严格使用原始简历中的真实个人信息，绝对不要使用任何占位符。重新输出 JSON。"
        )
        try:
            response2 = llm.invoke([HumanMessage(content=retry_prompt)])
            content2 = response2.content.strip()
            if content2.startswith("```json"):
                content2 = content2[7:]
            elif content2.startswith("```"):
                content2 = content2[3:]
            if content2.endswith("```"):
                content2 = content2[:-3]
            parsed2 = json.loads(content2.strip())
            tailored2 = parsed2.get("tailored_resume", "")
            still_has = [kw for kw in placeholder_keywords if kw in tailored2]
            if not still_has:
                parsed = parsed2
                print("[Tailor] 重新生成成功，占位符已清除")
            else:
                print(f"[Tailor] 重新生成后仍有占位符: {still_has}")
        except Exception as e:
            print(f"[Tailor] 重新生成失败: {e}")

    result["changes_summary"] = parsed.get("changes_summary", "")
    result["tailored_resume"] = parsed.get("tailored_resume", "")

    # 保存到文件
    if result["tailored_resume"]:
        output_dir = Path.home() / ".job_coach" / "tailored"
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"resume_tailored_{timestamp}.md"
        output_path.write_text(result["tailored_resume"], encoding='utf-8')
        result["output_path"] = str(output_path)

        # 保存到数据库
        try:
            save_resume_version(
                job_analysis_id=job_analysis_id,
                original_resume=resume_text,
                tailored_resume=result["tailored_resume"],
                changes_summary=result["changes_summary"],
                output_path=str(output_path)
            )
        except Exception as e:
            print(f"警告: 保存简历版本失败 - {e}")

        result["success"] = True

    return result


# ---------- 监控模块 ----------
MONITOR_STATE_FILE = Path.home() / ".job_coach" / "monitor_state.json"

# 旧版目录监控（保留向后兼容）
WATCH_DIR = os.getenv("JOB_COACH_WATCH_DIR", "")
MONITOR_INTERVAL = float(os.getenv("JOB_COACH_MONITOR_INTERVAL", "5"))
_monitor_thread = None
_monitor_stop_event = None

# 新版窗口监控全局状态
_monitor_manager = None

def _read_state_file():
    if MONITOR_STATE_FILE.exists():
        try:
            return json.loads(MONITOR_STATE_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {"command": "none"}

def _write_state_file(state: dict):
    MONITOR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MONITOR_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')

def _clear_state_file():
    if MONITOR_STATE_FILE.exists():
        MONITOR_STATE_FILE.unlink()

def get_window_by_title(keyword: str):
    """根据标题关键字获取可见窗口列表"""
    try:
        windows = gw.getWindowsWithTitle(keyword)
        return [w for w in windows if w.visible and not w.isMinimized]
    except Exception:
        return []

def capture_window_region(window) -> Image.Image:
    """截取窗口区域，失败返回 None"""
    try:
        bbox = (window.left, window.top, window.right, window.bottom)
        return ImageGrab.grab(bbox=bbox, all_screens=True)
    except Exception:
        try:
            window.activate()
            time.sleep(0.2)
            bbox = (window.left, window.top, window.right, window.bottom)
            return ImageGrab.grab(bbox=bbox, all_screens=True)
        except Exception:
            return None

def hash_text(text: str) -> str:
    return hashlib.md5(text.encode('utf-8', errors='replace')).hexdigest()

# 预加载提示词模板
PRELOAD_PROMPT_TEMPLATE = """你是面试辅助助手。基于以下信息，生成 3 条简短建议。
每条建议不超过 20 字，直接输出 JSON 数组，不要有其他文字。

公司：{company_name}
历史洞察：{insights}
对方最新消息：{last_message}
最近对话摘要：{session_summary}

输出格式：["建议1", "建议2", "建议3"]"""

def analyze_for_overlay(ocr_text: str, company_id: int = None) -> list:
    """快速生成 3 条简短建议（用于悬浮窗）"""
    if not ocr_text.strip():
        return ["未识别到文字内容"]

    if company_id is not None:
        conn = get_db_connection()
        company_row = conn.execute(
            "SELECT name, notes FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        conn.close()
        company_name = company_row["name"] if company_row else "未知公司"
        insights = company_row["notes"] if company_row else "暂无"

        recent_turns = get_recent_context(company_id, limit=3)
        session_summary = ""
        if recent_turns:
            for i, turn in enumerate(recent_turns, 1):
                session_summary += f"第{i}轮-对方:{turn['raw_ocr_text'][:100]}... "

        prompt = PRELOAD_PROMPT_TEMPLATE.format(
            company_name=company_name,
            insights=insights,
            last_message=ocr_text[:500],
            session_summary=session_summary or "暂无历史对话"
        )
    else:
        prompt = f"""你是面试辅助助手。生成 3 条简短建议，每条不超过 20 字。
对方最新消息：{ocr_text[:500]}
输出格式：["建议1", "建议2", "建议3"]
直接输出 JSON 数组，不要有其他文字。"""

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        content = response.content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
        suggestions = json.loads(content)
        if isinstance(suggestions, list):
            return suggestions[:3]
        return [str(suggestions)[:20]]
    except Exception:
        return ["结合岗位经验回答", "展示核心项目成果", "准备反问技术栈"]


# ---------- 悬浮窗 ----------
class OverlayWindow:
    """无边框置顶半透明建议窗口"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()

        self.window = tk.Toplevel(self.root)
        self.window.overrideredirect(True)
        self.window.attributes('-topmost', True)
        self.window.attributes('-alpha', 0.85)
        self.window.configure(bg='#1e1e2e')
        self.window.geometry('340x180+100+100')

        self._drag_x = 0
        self._drag_y = 0
        self.window.bind('<Button-1>', self._on_drag_start)
        self.window.bind('<B1-Motion>', self._on_drag_motion)

        # 标题栏
        title_frame = tk.Frame(self.window, bg='#313244', height=30)
        title_frame.pack(fill='x')
        title_frame.pack_propagate(False)

        tk.Label(
            title_frame, text='面试助手 · 实时建议',
            fg='#cdd6f4', bg='#313244',
            font=tkfont.Font(size=10, weight='bold')
        ).pack(side='left', padx=10, pady=3)

        close_btn = tk.Label(
            title_frame, text='✕', fg='#f38ba8', bg='#313244',
            font=tkfont.Font(size=12), cursor='hand2'
        )
        close_btn.pack(side='right', padx=10, pady=3)
        close_btn.bind('<Button-1>', lambda e: self.hide())

        # 建议内容
        self.content_frame = tk.Frame(self.window, bg='#1e1e2e')
        self.content_frame.pack(fill='both', expand=True, padx=8, pady=5)

        self.suggestion_labels = []
        self.suggestion_texts = []

        for i in range(3):
            row = tk.Frame(self.content_frame, bg='#1e1e2e')
            row.pack(fill='x', pady=3)

            num_color = ['#89b4fa', '#a6e3a1', '#fab387'][i]
            tk.Label(
                row, text=f'{i+1}.', fg=num_color, bg='#1e1e2e',
                font=tkfont.Font(size=10), width=2
            ).pack(side='left')

            sug = tk.Label(
                row, text='等待分析...', fg='#cdd6f4', bg='#1e1e2e',
                font=tkfont.Font(size=10), anchor='w', wraplength=230
            )
            sug.pack(side='left', fill='x', expand=True, padx=3)

            copy_btn = tk.Label(
                row, text='📋', fg='#a6e3a1', bg='#1e1e2e',
                font=tkfont.Font(size=10), cursor='hand2'
            )
            copy_btn.pack(side='right', padx=3)
            copy_btn.bind('<Button-1>', lambda e, idx=i: self._copy(idx))

            self.suggestion_labels.append(sug)
            self.suggestion_texts.append('')

        self._fade_timer = None
        self.window.withdraw()

    def _on_drag_start(self, event):
        self._drag_x = event.x_root - self.window.winfo_x()
        self._drag_y = event.y_root - self.window.winfo_y()
        self._reset_fade()

    def _on_drag_motion(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.window.geometry(f'+{x}+{y}')

    def _reset_fade(self):
        if self._fade_timer:
            self.root.after_cancel(self._fade_timer)
        self.window.attributes('-alpha', 0.92)
        self._fade_timer = self.root.after(5000, lambda: self.window.attributes('-alpha', 0.55))

    def _copy(self, idx: int):
        text = self.suggestion_texts[idx]
        if text:
            try:
                pyperclip.copy(text)
                orig_fg = self.suggestion_labels[idx].cget('fg')
                orig_bg = self.suggestion_labels[idx].cget('bg')
                self.suggestion_labels[idx].configure(fg='#1e1e2e', bg='#a6e3a1')
                self.root.after(400, lambda: self.suggestion_labels[idx].configure(fg=orig_fg, bg=orig_bg))
            except Exception:
                pass

    def update_suggestions(self, suggestions: list):
        self._reset_fade()
        for i, sug in enumerate(suggestions[:3]):
            short = sug[:20] if len(sug) > 20 else sug
            self.suggestion_labels[i].configure(text=short)
            self.suggestion_texts[i] = sug
        if not self.window.winfo_viewable():
            self.window.deiconify()
        self.window.lift()

    def show(self):
        self.window.deiconify()
        self.window.lift()

    def hide(self):
        self.window.withdraw()

    def run_loop(self):
        self.root.mainloop()

    def stop(self):
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass


# ---------- 系统托盘 ----------
class TrayApp:
    def __init__(self, overlay: OverlayWindow = None):
        self.overlay = overlay
        self.icon = None
        self._icon_img = self._make_icon()

    def _make_icon(self):
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        d = PILDraw.Draw(img)
        d.rounded_rectangle([4, 12, 58, 46], radius=8, fill='#89b4fa', outline='#45475a', width=2)
        d.polygon([(20, 46), (14, 58), (28, 46)], fill='#89b4fa')
        return img

    def _toggle_overlay(self, icon, item):
        if self.overlay:
            try:
                if self.overlay.window.winfo_viewable():
                    self.overlay.hide()
                else:
                    self.overlay.show()
            except Exception:
                pass

    def _on_pause(self, icon, item):
        _write_state_file({"command": "pause"})

    def _on_resume(self, icon, item):
        _write_state_file({"command": "resume"})

    def _on_exit(self, icon, item):
        _write_state_file({"command": "stop"})
        icon.stop()
        if self.overlay:
            self.overlay.stop()

    def make_menu(self):
        items = [
            pystray.MenuItem('显示/隐藏悬浮窗', self._toggle_overlay, default=True),
            pystray.MenuItem('暂停监控', self._on_pause),
            pystray.MenuItem('恢复监控', self._on_resume),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('退出', self._on_exit),
        ]
        return pystray.Menu(*items)

    def run(self):
        if not HAS_PYSTRAY:
            return
        self.icon = pystray.Icon(
            'job_coach', self._icon_img, '面试助手 · 监控中', self.make_menu()
        )
        self.icon.run()

    def stop(self):
        if self.icon:
            self.icon.stop()


# ---------- 监控工作线程 ----------
class MonitorWorker:
    """单个窗口监控线程：截图 → 变化检测 → OCR → LLM → 推送"""

    def __init__(self, window_title: str, company_id: int,
                 overlay: OverlayWindow, sq: queue.Queue, interval: float = 1.5):
        self.window_title = window_title
        self.company_id = company_id
        self.overlay = overlay
        self.sq = sq
        self.interval = interval
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread = None
        self._last_hash = ""
        self._win = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._pause.set()

    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def _find_window(self):
        windows = get_window_by_title(self.window_title)
        if windows:
            self._win = max(windows, key=lambda w: w.width * w.height)
            return True
        return False

    def _run(self):
        while not self._stop.is_set():
            if self._pause.is_set():
                self._stop.wait(1.0)
                continue

            try:
                if self._win is None:
                    if not self._find_window():
                        self._stop.wait(1.5)
                        continue

                screenshot = capture_window_region(self._win)
                if screenshot is None:
                    self._win = None
                    self._stop.wait(1.5)
                    continue

                tmp = Path.home() / ".job_coach" / f"tmp_{hash_text(self.window_title)[:8]}.png"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                screenshot.save(str(tmp))

                ocr_text = extract_text_from_image(str(tmp))
                try:
                    tmp.unlink()
                except Exception:
                    pass

                if ocr_text.strip():
                    h = hash_text(ocr_text)
                    if h != self._last_hash:
                        self._last_hash = h
                        suggestions = analyze_for_overlay(ocr_text, self.company_id)
                        self.sq.put(suggestions)

                        if self.company_id is not None:
                            try:
                                sid = get_or_create_active_session(self.company_id)
                                save_conversation_turn(
                                    session_id=sid, role="interviewer",
                                    content="\n".join(suggestions),
                                    suggestions="\n".join(suggestions),
                                    raw_ocr_text=ocr_text
                                )
                            except Exception:
                                pass
            except Exception as e:
                print(f"[监控] {self.window_title} 出错: {e}")

            self._stop.wait(self.interval)


class MonitorManager:
    """统一管理：多窗口监控 + 悬浮窗 + 托盘"""

    def __init__(self):
        self.workers: List[MonitorWorker] = []
        self.overlay: Optional[OverlayWindow] = None
        self.tray: Optional[TrayApp] = None
        self.sq = queue.Queue()
        self._running = False

    def start(self, window_titles: list, company_id: int, interval: float = 1.5):
        if self._running:
            print("监控已在运行中")
            return
        self._running = True

        self.overlay = OverlayWindow()
        self.tray = TrayApp(self.overlay)

        for title in window_titles:
            w = MonitorWorker(title, company_id, self.overlay, self.sq, interval)
            w.start()
            self.workers.append(w)
            print(f"已启动窗口监控: {title}")

        _write_state_file({
            "command": "none", "pid": os.getpid(), "running": True,
            "windows": window_titles, "company_id": company_id
        })

        def _poll():
            if not self._running:
                return
            try:
                while True:
                    suggestions = self.sq.get_nowait()
                    if self.overlay:
                        self.overlay.update_suggestions(suggestions)
            except queue.Empty:
                pass

            state = _read_state_file()
            cmd = state.get("command", "none")
            if cmd == "pause":
                for w in self.workers:
                    w.pause()
                print("[监控] 已暂停")
                _write_state_file({"command": "none", "pid": os.getpid(), "running": True})
            elif cmd == "resume":
                for w in self.workers:
                    w.resume()
                print("[监控] 已恢复")
                _write_state_file({"command": "none", "pid": os.getpid(), "running": True})
            elif cmd == "stop":
                print("[监控] 正在停止...")
                self.stop()
                return

            if self.overlay:
                self.overlay.root.after(500, _poll)

        if self.overlay:
            self.overlay.root.after(500, _poll)

        if HAS_PYSTRAY:
            threading.Thread(target=self.tray.run, daemon=True).start()

        if self.overlay:
            self.overlay.run_loop()

    def stop(self):
        self._running = False
        for w in self.workers:
            w.stop()
        self.workers.clear()
        if self.tray:
            self.tray.stop()
        if self.overlay:
            self.overlay.stop()
        _clear_state_file()
        print("[监控] 已停止")


# ---------- 旧版目录监控（向后兼容）----------

def _watch_loop(watch_dir: str, company_id: int, interval: float, stop_event: threading.Event):
    known_files = set()
    if os.path.isdir(watch_dir):
        known_files = set(os.listdir(watch_dir))
    while not stop_event.is_set():
        try:
            if not os.path.isdir(watch_dir):
                stop_event.wait(interval)
                continue
            current_files = set(os.listdir(watch_dir))
            for filename in sorted(current_files - known_files):
                if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.tiff')):
                    filepath = os.path.join(watch_dir, filename)
                    time.sleep(0.5)
                    if not os.path.exists(filepath):
                        continue
                    print(f"[监控] 检测到新截图: {filename}")
                    try:
                        r = analyze_screenshot_core(filepath, company_id=company_id)
                        if r["success"]:
                            print(f"[监控] 分析完成, 已保存 turn #{r['turn_id']}")
                    except Exception as e:
                        print(f"[监控] 处理失败: {e}")
            known_files = current_files
        except Exception as e:
            print(f"[监控] 循环出错: {e}")
        stop_event.wait(interval)

def start_monitor(watch_dir: str = None, company_id: int = None, interval: float = None):
    global _monitor_thread, _monitor_stop_event
    wd = watch_dir or WATCH_DIR
    if not wd or not os.path.isdir(wd):
        print(f"错误: 监控目录不存在: {wd}")
        return False
    if _monitor_thread and _monitor_thread.is_alive():
        print("监控已在运行中。")
        return False
    iv = interval or MONITOR_INTERVAL
    _monitor_stop_event = threading.Event()
    _monitor_thread = threading.Thread(
        target=_watch_loop, args=(wd, company_id, iv, _monitor_stop_event), daemon=True
    )
    _monitor_thread.start()
    print(f"监控已启动, 监听目录: {wd} (间隔: {iv}s)")
    return True

def stop_monitor():
    global _monitor_thread, _monitor_stop_event
    if _monitor_thread and _monitor_thread.is_alive():
        _monitor_stop_event.set()
        _monitor_thread.join(timeout=5)
        print("监控已停止。")
        return True
    print("监控未运行。")
    return False


def import_jobs_json(filepath: str, dry_run: bool = False) -> dict:
    """从 BOSS-Auto-Job-Semi 扩展导出的 JSON 导入岗位分析"""
    import json
    import os

    if not os.path.exists(filepath):
        return {"success": False, "error": f"文件不存在: {filepath}", "imported": 0, "skipped": 0}

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    records = data if isinstance(data, list) else data.get("records", [])
    if not records:
        return {"success": False, "error": "JSON 中没有岗位记录", "imported": 0, "skipped": 0}

    imported = 0
    skipped = 0
    companies_created = 0

    for rec in records:
        title = (rec.get("title") or "").strip()
        href = (rec.get("href") or "").strip()
        if not title:
            skipped += 1
            continue

        # 尝试提取公司名：BOSS 直聘标题常见格式 "岗位 - 公司名"
        company_name = "待确认公司"
        if " - " in title:
            parts = title.rsplit(" - ", 1)
            company_name = parts[1].strip()
            if len(company_name) > 20:
                company_name = "待确认公司"

        jd_text = (rec.get("jdText") or "").strip()
        greeting = (rec.get("greeting") or "").strip()
        score = int(rec.get("score", 0))
        hits = rec.get("hits", [])
        negatives = rec.get("negatives", [])
        main_reason = (rec.get("mainReason") or "").strip()

        # 构建坑位评估
        pitfall_parts = []
        if negatives:
            pitfall_parts.append("风险点: " + "; ".join(negatives[:5]))
        if main_reason:
            pitfall_parts.append("判断依据: " + main_reason)
        pitfall = "\n".join(pitfall_parts) if pitfall_parts else "无明显坑位"

        # 构建简历建议
        resume_advice = ""
        if hits:
            resume_advice = "命中匹配点:\n" + "\n".join(f"- {h}" for h in hits[:5])
        if negatives:
            resume_advice += "\n\n需关注:\n" + "\n".join(f"- {n}" for n in negatives[:5])

        if dry_run:
            print(f"[DRY RUN] {title} | {company_name} | score={score} | greeting={greeting[:40]}...")
            imported += 1
            continue

        # 查重：同一公司 + 相似标题
        conn = get_db_connection()
        company_id = get_or_create_company(company_name)
        if company_id:
            companies_created += 1 if not _company_existed(company_name) else 0

        existing = conn.execute(
            "SELECT id FROM job_analyses WHERE company_id = ? AND jd_text = ?",
            (company_id, jd_text[:200])
        ).fetchone()
        conn.close()

        if existing:
            print(f"[跳过] 已存在: {title}")
            skipped += 1
            continue

        try:
            save_job_analysis(
                company_id=company_id,
                jd_text=jd_text or title,
                pitfall_assessment=pitfall,
                match_score=score,
                strengths=hits if hits else ["扩展导入"],
                gaps=negatives if negatives else ["待补充"],
                resume_advice=resume_advice or "待补充",
                self_intro=greeting or "待生成"
            )
            imported += 1
            print(f"[导入] {title[:50]} | score={score}")
        except Exception as e:
            print(f"[失败] {title[:50]}: {e}")
            skipped += 1

    return {
        "success": True,
        "imported": imported,
        "skipped": skipped,
        "total": len(records),
        "companies_created": companies_created
    }


def _company_existed(name: str) -> bool:
    """检查公司是否已存在"""
    conn = get_db_connection()
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row is not None


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "import-jobs":
        filepath = sys.argv[2] if len(sys.argv) >= 3 else None
        if not filepath:
            print("用法: python job_coach_cli.py import-jobs <jobs.json>")
            sys.exit(1)
        dry_run = "--dry-run" in sys.argv
        result = import_jobs_json(filepath, dry_run=dry_run)
        if result["success"]:
            print(f"\n导入完成: {result['imported']} 条, 跳过 {result['skipped']} 条重复")
            if result.get("companies_created"):
                print(f"新建公司: {result['companies_created']} 个")
        else:
            print(f"导入失败: {result.get('error')}")
            sys.exit(1)
    else:
        print("Job Coach CLI")
        print("  python job_coach_cli.py import-jobs <jobs.json>  导入岗位JSON")
        print("  python job_coach_cli.py import-jobs <jobs.json> --dry-run  预览模式")
