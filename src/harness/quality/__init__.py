"""P2 质量门（Quality Gate）—— 玄镜 Harness 化第四块。

目标：把 AI 输出的「格式合规 / 孤立编号 / 幻觉越界」退化变成可量化、可回归的指标，
在 PR / 定时任务 / 发布前自动拦截格式漂移与幻觉关键词。

设计：纯标准库，无外部依赖；指标函数可单测，gate 可批量评估 golden set。
孤立编号率对应前端 textDedup.renumberStandaloneNumberedHeadings 的 Python 化度量。
"""
