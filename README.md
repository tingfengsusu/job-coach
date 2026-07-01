# 求职面试助手 (Job Coach)

Windows 系统托盘应用，截图分析岗位 JD 和面试对话，提供 AI 求职辅导。

## 功能

- **岗位分析** — 截图 JD → 匹配度评分、坑位评估、优劣势、简历建议、自荐话术
- **面试辅助** — 截图面试对话 → 生成可直接发送的完整回复（含策略说明）+ 面试官视角评估
- **面试反馈回路** — 原始回复 → 面试官评分评估 → 优化后回复
- **多模态视觉分析** — DeepSeek Chat 视觉模型直接识图分析，秒级响应
- **OCR 降级方案** — 视觉分析失败时自动回退到 EasyOCR + LLM
- **多公司管理** — 支持同时进行多家公司面试，数据库隔离
- **截图区域记忆** — 每个模式独立记忆截图区域，切换 Alt+Z/X/C 自动切换区域

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Alt+Z` | 自动判断场景 |
| `Alt+X` | 岗位分析 |
| `Alt+C` | 面试辅助 |

右键托盘图标查看更多选项：设置截图区域、切换模式、查看分析历史等。

## 安装

```bash
pip install -r requirements.txt
```

### 额外依赖

- **EasyOCR**（OCR 降级方案）：`pip install easyocr`
- **pystray**（系统托盘）：`pip install pystray`
- **keyboard**（全局热键）：`pip install keyboard`

## 配置

在项目根目录创建 `.env` 文件：

```env
DEEPSEEK_API_KEY=your_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
```

## 使用

```bash
python tray_app.py
```

首次使用建议：
1. 按 `Alt+X` 截图岗位 JD → 自动分析并生成匹配度报告
2. 右键托盘 → 设置截图区域 → 为岗位窗口框选一个区域
3. 按 `Alt+C` 截图面试对话 → 获取可直接复制的回复方案
4. 岗位和面试区域各自独立记忆，一次设置即可

## 项目结构

```
job-coach/
├── tray_app.py              # 系统托盘主程序（GUI + 热键 + 弹窗）
├── vision_analyzer.py        # DeepSeek Chat 多模态视觉分析
├── job_coach_cli.py          # 核心分析逻辑（OCR + LLM + 数据库）
├── vector_memory.py          # 向量语义检索（面试上下文关联）
├── resume_tailor.py          # 简历定制（根据 JD 生成针对性简历）
├── job_matcher.py            # 岗位匹配评分
├── screenshot_analyzer.py    # 截图场景判断
├── job_board.py              # 岗位看板
├── user_profile.py           # 用户档案管理
├── communicator.py           # 沟通话术生成
├── config.py                 # 配置管理
├── utils.py                  # 通用工具
├── test_all.py               # 集成测试
└── test_multi_company_integration.py  # 多公司隔离测试
```

## 技术栈

- **DeepSeek Chat** — 多模态视觉 API，`image_data` 字段传递图片（纯 base64，无 MIME 前缀）
- **EasyOCR** — 中文 OCR 降级方案
- **LangChain** — LLM 调用抽象层（OCR + LLM 路径使用）
- **tkinter** — GUI 弹窗和设置面板
- **pystray** — 系统托盘
- **SQLite** — 本地数据持久化
- **ChromaDB** — 向量相似度检索
