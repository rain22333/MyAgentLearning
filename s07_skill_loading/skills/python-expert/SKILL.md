---
name: python-expert
description: Python 编程专家指南，涵盖类型提示、异步编程和最佳实践。编写或审查 Python 代码时加载。
---

# Python 专家指南

## 代码风格
- 使用 Type Hints 标注所有函数签名
- 遵循 PEP 8 命名规范（snake_case 函数，PascalCase 类）
- 每行不超过 88 字符（Black 格式化工具标准）

## 最佳实践
- 优先使用 `pathlib.Path` 而非 `os.path`
- 使用 `subprocess.run()` 而非 `os.system()`
- 上下文管理器 (`with`) 管理资源
- 使用 f-string 格式化字符串

## 项目结构
- 每个模块应该有 `if __name__ == "__main__"` 入口
- 配置使用环境变量或 `.env` 文件
- 类型注解用 `from __future__ import annotations` 延迟求值
