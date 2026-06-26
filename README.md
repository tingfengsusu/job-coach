# 求职面试助手 (Job Coach)

Windows 系统托盘应用，实时截图分析岗位 JD 和面试对话，提供 AI 驱动的求职辅导。

## 功能

- **岗位分析** — 截图 JD → 匹配度评分、优劣势、简历修改建议、自荐话术
- **面试辅助** — 截图面试对话 → 意图分析 + 回复策略建议
- **面试反馈回路** — 原始建议 → 面试官视角评估 → 优化后建议
- **简历定制** — 根据 JD 自动生成针对性简历
- **多公司管理** — 支持同时进行多家公司面试，数据库隔离

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+Shift+Z` | 截图分析（岗位 JD） |
| `Ctrl+Shift+X` | 截图分析（面试对话） |
| `Ctrl+Shift+C` | 显示/隐藏主窗口 |

## 安装

```bash
pip install -r requirements.txt
```

## 配置

1. 在项目根目录创建 `.env` 文件：
```
DEEPSEEK_API_KEY=your_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
```

2. 运行 `python tray_app.py` 启动系统托盘应用

## 技术栈

- DeepSeek V4 Pro 多模态 API（视觉分析）
- EasyOCR（中文 OCR 降级方案）
- tkinter（GUI 弹窗）
- SQLite（本地数据持久化）
