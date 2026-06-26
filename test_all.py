#!/usr/bin/env python3
"""全面测试求职助手的所有功能"""

import sys
import os
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))

def section(title):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}")


# ── 1. 模块导入检查 ──
section("1. 模块导入检查")

try:
    import vector_memory
    check("vector_memory 导入成功", True)
except Exception as e:
    check("vector_memory 导入", False, str(e))

try:
    import job_coach_cli
    check("job_coach_cli 导入成功", True)
except Exception as e:
    check("job_coach_cli 导入", False, str(e))

try:
    import tray_app
    check("tray_app 导入成功", True)
except Exception as e:
    check("tray_app 导入", False, str(e))


# ── 2. 关键函数存在性检查 ──
section("2. 关键函数存在性检查")

required_cli_funcs = [
    "init_db", "get_db_connection",
    "extract_text_from_image",
    "get_or_create_company", "get_or_create_active_session",
    "save_conversation_turn", "get_recent_context",
    "analyze_screenshot_core", "detect_content_type",
    "analyze_job_screenshot", "tailor_resume",
    "save_job_analysis", "save_resume_version", "load_resume_text",
    "analyze_resume", "match_job",
    "build_enhanced_prompt", "end_active_session",
    "HAS_PYSTRAY", "llm",
]
for name in required_cli_funcs:
    check(f"job_coach_cli.{name}", hasattr(job_coach_cli, name))

required_tray_attrs = [
    "TrayApplication", "SelectionOverlay",
    "load_regions", "save_regions",
    "get_active_region_name", "set_active_region_name", "get_current_region",
    "capture_selection", "show_selection_overlay",
    "set_region_interactive", "prompt_for_region_name",
    "show_result_popup", "show_job_result_popup", "show_tailor_result_popup",
    "show_settings_window", "show_notification",
    "HistoryWindow", "auto_match_company",
    "load_config", "save_config",
]
for name in required_tray_attrs:
    check(f"tray_app.{name}", hasattr(tray_app, name))

# 检查 SelectionOverlay 方法
check("SelectionOverlay.run", hasattr(tray_app.SelectionOverlay, "run"))
check("SelectionOverlay.run_bbox", hasattr(tray_app.SelectionOverlay, "run_bbox"))


# ── 3. sqlite3.Row 安全性检查 ──
section("3. sqlite3.Row .get() 安全性检查")

def scan_file_for_row_get(filepath):
    import ast
    with open(filepath, encoding='utf-8') as f:
        source = f.read()
    if "row.get(" in source:
        lines = [f"{i+1}:{l}" for i, l in enumerate(source.split('\n')) if "row.get(" in l]
        return False, lines
    return True, []

for fname in ["tray_app.py", "job_coach_cli.py"]:
    ok, lines = scan_file_for_row_get(fname)
    check(f"{fname} 无 row.get()", ok, "; ".join(lines) if lines else "OK")

# 同时检查 Python 语法
for fname in ["tray_app.py", "job_coach_cli.py", "vector_memory.py"]:
    try:
        import ast
        with open(fname, encoding='utf-8') as f:
            ast.parse(f.read())
        check(f"{fname} 语法正确", True)
    except SyntaxError as e:
        check(f"{fname} 语法", False, str(e))


# ── 4. 数据库检查 ──
section("4. 数据库检查")

try:
    job_coach_cli.init_db()
    conn = job_coach_cli.get_db_connection()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [t['name'] for t in tables]
    expected = ['companies', 'conversation_turns', 'interview_sessions',
                'job_analyses', 'jobs', 'resume_versions', 'user_preferences']
    for t in expected:
        check(f"表 {t} 存在", t in table_names)

    # 检查各表可正常查询
    for t in expected:
        try:
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            check(f"表 {t} 可查询", True)
        except Exception as e:
            check(f"表 {t} 可查询", False, str(e))
    conn.close()
except Exception as e:
    check("数据库初始化", False, str(e))


# ── 5. 向量记忆检查 ──
section("5. 向量记忆检查")

try:
    vm = vector_memory.VectorMemory()
    check("VectorMemory 初始化", True, f"存储路径: {vm.persist_dir}")
    check("is_available 属性", True, str(vm.is_available))
    # 所有操作在模型不可用时应该安全返回
    vm.add_turn(99999, 1, "测试文本", "interviewer")
    results = vm.search_similar(99999, "测试查询")
    check("search_similar 安全返回", isinstance(results, list))
    vm.delete_company_collection(99999)
    check("delete_company_collection 安全返回", True)
    count = vm.rebuild_from_db(99999)
    check("rebuild_from_db 安全返回", isinstance(count, int))
except Exception as e:
    check("向量记忆模块", False, str(e))
    traceback.print_exc()


# ── 6. LLM 连接检查 ──
section("6. LLM 连接检查")

try:
    from langchain_core.messages import HumanMessage
    llm = job_coach_cli.llm
    response = llm.invoke([HumanMessage(content="回复'OK'，不要其他内容")])
    check("LLM 调用成功", "OK" in response.content.upper(), response.content[:80])
except Exception as e:
    check("LLM 调用", False, str(e))


# ── 7. 分析函数检查 ──
section("7. 分析函数检查")

try:
    result = job_coach_cli.analyze_resume("精通Python和Django框架，3年后端开发经验")
    check("analyze_resume", isinstance(result, dict), str(result.get('tech_tags', []))[:60])
except Exception as e:
    check("analyze_resume", False, str(e))

try:
    result = job_coach_cli.match_job("精通Python", "招聘Python工程师，有Django经验")
    check("match_job", isinstance(result, dict), f"得分: {result.get('match_score', 'N/A')}")
except Exception as e:
    check("match_job", False, str(e))

try:
    result = job_coach_cli.detect_content_type("岗位职责：负责后端开发，要求3年Python经验")
    check("detect_content_type", result in ('job', 'interview'), f"结果: {result}")
except Exception as e:
    check("detect_content_type", False, str(e))

try:
    result = job_coach_cli.build_enhanced_prompt(
        "测试问题", "测试公司", "备注",
        {"recent_turns": [], "similar_turns": []}
    )
    check("build_enhanced_prompt", isinstance(result, str) and len(result) > 50)
except Exception as e:
    check("build_enhanced_prompt", False, str(e))


# ── 8. 数据库写入/读取检查 ──
section("8. 数据库 CRUD 检查")

try:
    company_id = job_coach_cli.get_or_create_company("_测试公司_")
    check("get_or_create_company", company_id > 0, f"ID={company_id}")

    session_id = job_coach_cli.get_or_create_active_session(company_id)
    check("get_or_create_active_session", session_id > 0, f"ID={session_id}")

    turn_id = job_coach_cli.save_conversation_turn(
        session_id=session_id, role="interviewer", content="分析结果",
        suggestions="建议1\n建议2", raw_ocr_text="请介绍一下你的项目经验"
    )
    check("save_conversation_turn", turn_id > 0, f"turn_id={turn_id}")

    ctx = job_coach_cli.get_recent_context(company_id, limit=3, current_query="项目经验")
    check("get_recent_context 返回 dict", isinstance(ctx, dict))
    check("get_recent_context 有 recent_turns", "recent_turns" in ctx)
    check("get_recent_context 有 similar_turns", "similar_turns" in ctx)

    job_coach_cli.end_active_session(company_id)
    check("end_active_session", True)

    # 清理测试数据
    conn = job_coach_cli.get_db_connection()
    conn.execute("DELETE FROM conversation_turns WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM interview_sessions WHERE id = ?", (session_id,))
    conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    conn.commit()
    conn.close()
    check("清理测试数据", True)
except Exception as e:
    check("数据库 CRUD", False, str(e))
    traceback.print_exc()
    # 尝试清理
    try:
        conn = job_coach_cli.get_db_connection()
        conn.execute("DELETE FROM conversation_turns WHERE session_id IN (SELECT id FROM interview_sessions WHERE company_id IN (SELECT id FROM companies WHERE name LIKE '_测试%'))")
        conn.execute("DELETE FROM interview_sessions WHERE company_id IN (SELECT id FROM companies WHERE name LIKE '_测试%')")
        conn.execute("DELETE FROM companies WHERE name LIKE '_测试%'")
        conn.commit()
        conn.close()
    except:
        pass


# ── 9. 配置文件检查 ──
section("9. 配置文件检查")

try:
    from pathlib import Path
    config_dir = Path.home() / ".job_coach"
    check("配置目录存在", config_dir.exists(), str(config_dir))

    config_file = config_dir / "config.json"
    if config_file.exists():
        import json
        cfg = json.loads(config_file.read_text(encoding='utf-8'))
        check("config.json 可读", True, f"热键: {cfg.get('hotkey', 'N/A')}")
    else:
        check("config.json 不存在(使用默认)", True)

    regions_file = config_dir / "screenshot_regions.json"
    if regions_file.exists():
        import json
        regions = json.loads(regions_file.read_text(encoding='utf-8'))
        check("screenshot_regions.json 可读", True, f"区域数: {len(regions)}")
    else:
        check("screenshot_regions.json 不存在(使用默认)", True)
except Exception as e:
    check("配置文件", False, str(e))


# ── 10. 托盘应用结构检查 ──
section("10. 托盘应用结构检查")

try:
    for method in [
        "_start_hotkey", "_on_hotkey", "_do_capture_workflow",
        "_analyze_in_background", "_poll_results",
        "_on_history", "_on_settings", "_on_exit",
        "_on_set_resume", "_on_open_resume_folder",
        "_on_set_region", "_on_new_region", "_on_overwrite_region",
        "_on_switch_region", "_on_reset_region",
        "_make_tray_menu", "_make_icon_image", "run",
    ]:
        check(f"TrayApplication.{method}", hasattr(tray_app.TrayApplication, method))
except Exception as e:
    check("托盘应用结构", False, str(e))


# ── 汇总 ──
section("测试结果汇总")
print(f"  通过: {PASS}")
print(f"  失败: {FAIL}")
print(f"  总计: {PASS + FAIL}")
if FAIL == 0:
    print(f"\n  [ALL PASS] 所有测试通过！")
else:
    print(f"\n  [WARN] 有 {FAIL} 项测试失败，请检查上方详情。")
