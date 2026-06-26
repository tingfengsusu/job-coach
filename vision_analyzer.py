#!/usr/bin/env python3
"""
多模态分析模块 - 使用 DeepSeek V4 Flash 视觉模型直接分析截图
使用 image_data 字段传递图片（纯 base64，无前缀），完全跳过 OCR
"""

import os
import base64
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
VISION_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def _encode_image(image_path: str) -> str:
    """将图片转换为纯 base64（无 data:image/... 前缀）"""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode('utf-8')


def _call_text_api(prompt: str, max_tokens: int = 1024) -> dict:
    """
    Pure text LLM call (no image). Used for assessment/optimization steps.
    Returns: {"success": bool, "content": str} or {"success": False, "error": str}
    """
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": VISION_MODEL,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2
    }

    try:
        response = requests.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )
        if response.status_code == 200:
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            return {"success": True, "content": content}
        else:
            return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
    except requests.Timeout:
        return {"success": False, "error": "请求超时（30秒）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _call_vision_api(image_path: str, prompt: str, max_tokens: int = 2048) -> dict:
    """
    调用 DeepSeek 多模态 API。
    返回: {"success": bool, "content": str} 或 {"success": False, "error": str}
    """
    file_size_kb = Path(image_path).stat().st_size / 1024
    print(f"[Vision] 图片尺寸: {file_size_kb:.0f}KB")

    image_b64 = _encode_image(image_path)
    b64_kb = len(image_b64) / 1024
    print(f"[Vision] base64 编码: {b64_kb:.0f}KB")

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "image_data": image_b64
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2
    }

    try:
        response = requests.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            return {"success": True, "content": content}
        else:
            return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
    except requests.Timeout:
        return {"success": False, "error": "请求超时（30秒）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _parse_json_response(content: str) -> dict:
    """解析 LLM 返回的 JSON，处理 markdown 代码块包裹"""
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    return json.loads(content.strip())


def analyze_job_with_vision(image_path: str, resume_text: str = None) -> dict:
    """
    使用多模态模型分析岗位 JD 截图
    返回格式与 analyze_job_screenshot 兼容
    """
    # 用字符串拼接而非 f-string，避免简历中的 {} 被误解析
    lines = [
        "你是专业求职顾问。分析这张岗位JD截图，输出严格JSON（不要markdown包裹）:",
        "{",
        '  "pitfall_assessment": "坑位评估文字，无明显坑位则写\\"无明显坑位\\"",',
        '  "match_score": 75,',
        '  "strengths": ["匹配项1", "匹配项2", "匹配项3"],',
        '  "gaps": ["缺口项1", "缺口项2", "缺口项3"],',
        '  "resume_advice": "简历修改建议，3-5条用\\n分隔",',
        '  "self_intro": "您好，看到贵司在招XX岗位，我有X年XX经验，熟悉JD中提到的XX和XX，做过XX项目，期待有机会沟通。"',
        "}",
        "",
        "注意：self_intro 是一句完整的打招呼消息，像BOSS直聘上发给HR的第一句话。必须提到JD中的具体技术栈或要求，语气自然不做作。",
    ]

    if resume_text:
        lines.insert(1, "\n候选人简历：\n" + resume_text[:2000])

    prompt = "\n".join(lines)

    api_result = _call_vision_api(image_path, prompt, max_tokens=4096)

    if not api_result.get("success"):
        print(f"[Vision] 岗位分析失败: {api_result.get('error')}")
        return {"success": False, "error": api_result.get("error", "未知错误")}

    try:
        raw = api_result["content"]
        print(f"[Vision] 原始返回(前300字): {raw[:300]}")
        result = _parse_json_response(raw)

        result.setdefault("match_score", 0)
        result.setdefault("pitfall_assessment", "")
        result.setdefault("strengths", [])
        result.setdefault("gaps", [])
        result.setdefault("resume_advice", "")
        result.setdefault("self_intro", "")
        result["success"] = True
        print(f"[Vision] 解析成功: match_score={result['match_score']}, "
              f"strengths={len(result['strengths'])}, gaps={len(result['gaps'])}")
        return result
    except json.JSONDecodeError as e:
        print(f"[Vision] JSON解析失败: {e}")
        print(f"[Vision] 原始返回(后100字): {raw[-100:]}")
        return {"success": False, "error": f"JSON解析失败: {e}", "raw_content": raw[:500]}


def analyze_interview_with_vision(image_path: str) -> dict:
    """
    使用多模态模型分析面试对话截图
    返回格式与 analyze_screenshot_core 兼容
    """
    prompt = """你是面试辅助助手。分析这张聊天截图，输出严格JSON（不要markdown包裹）:
{
  "intent_analysis": "面试官问题的意图分析（50字以内）",
  "suggestions": "回复策略建议（2-3条要点，字符串，用换行符分隔）",
  "analysis": "详细分析和建议的反问问题"
}"""

    api_result = _call_vision_api(image_path, prompt)

    if not api_result.get("success"):
        print(f"[Vision] 面试分析失败: {api_result.get('error')}")
        return {"success": False, "error": api_result.get("error", "未知错误")}

    try:
        raw = api_result["content"]
        print(f"[Vision] 原始返回(前300字): {raw[:300]}")
        result = _parse_json_response(raw)
        result["success"] = True
        print(f"[Vision] 面试解析成功: suggestions={len(result.get('suggestions', ''))}字")
        return result
    except json.JSONDecodeError as e:
        print(f"[Vision] JSON解析失败: {e}")
        return {
            "success": True,
            "intent_analysis": "无法解析LLM响应",
            "suggestions": raw[:500],
            "analysis": raw[:500]
        }


def analyze_interview_with_feedback(image_path: str) -> dict:
    """
    面试分析 + 面试官视角评估 + 优化建议（3步流水线）。
    Step 1: 视觉分析原始回答建议
    Step 2: 面试官视角评估（纯文本）
    Step 3: 基于评估优化建议（纯文本）
    """
    # ── Step 1: 原始视觉分析 ──
    initial = analyze_interview_with_vision(image_path)
    if not initial.get("success"):
        return initial

    suggestions_text = initial.get("suggestions", "")
    intent = initial.get("intent_analysis", "")

    # ── Step 2: 面试官视角评估 ──
    assess_prompt = f"""你是资深面试官。评估以下面试回答建议：

面试官问题意图：{intent}
回答建议：{suggestions_text}

从面试官视角评估这个回答（语气是否自信得体、逻辑是否清晰直接、深度是否足够有案例支撑），输出严格JSON（不要markdown包裹）：
{{
  "assessment": "总体评价（一句话，50字以内）",
  "score": 0到100的整数,
  "strengths": ["优点1", "优点2"],
  "weaknesses": ["问题1", "问题2"]
}}"""

    assess_result = _call_text_api(assess_prompt)
    perspective = None
    if assess_result.get("success"):
        try:
            perspective = _parse_json_response(assess_result["content"])
        except json.JSONDecodeError:
            perspective = {
                "assessment": "评估解析失败",
                "score": 0,
                "strengths": [],
                "weaknesses": []
            }
    else:
        perspective = {
            "assessment": f"评估失败: {assess_result.get('error', '')}",
            "score": 0,
            "strengths": [],
            "weaknesses": []
        }

    # ── Step 3: 优化建议 ──
    optimize_prompt = f"""你是面试辅导专家。根据面试官评估优化回答建议：

原始回答建议：{suggestions_text}
面试官评估：{json.dumps(perspective, ensure_ascii=False)}

请输出优化后的回答建议，严格JSON（不要markdown包裹）：
{{
  "intent_analysis": "面试官问题意图（50字以内）",
  "optimized_suggestions": "优化后的回复策略建议（2-3条要点，字符串，用换行符分隔）"
}}"""

    optimize_result = _call_text_api(optimize_prompt)
    optimized_suggestions = suggestions_text
    if optimize_result.get("success"):
        try:
            optimized = _parse_json_response(optimize_result["content"])
            optimized_suggestions = optimized.get("optimized_suggestions", suggestions_text)
        except json.JSONDecodeError:
            pass

    return {
        "success": True,
        "intent_analysis": intent,
        "original_suggestions": suggestions_text,
        "analysis": initial.get("analysis", ""),
        "interviewer_perspective": perspective,
        "optimized_suggestions": optimized_suggestions,
    }
