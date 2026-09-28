# 玄镜 AI 输出质量门报告（P2 Harness 化）

- 总体判定：**通过 ✅**
- 期望不符样本数：0

| 样本 | 模块 | 通过 | 期望 | 符合 | 评分 |
|---|---|---|---|---|---|
| tarot_good | tarot | ✅ | True | ✅ | 1.0 |
| tarot_bad_orphan | tarot | ❌ | False | ✅ | 0.66 |
| tarot_bad_hallu | tarot | ❌ | False | ✅ | 0.66 |
| bazi_good | bazi | ✅ | True | ✅ | 1.0 |
| bazi_bad_missing | bazi | ❌ | False | ✅ | 0.66 |
| generic_good |  | ✅ | True | ✅ | 1.0 |

## 指标说明
- format_compliance：必含章节齐备性（格式合规率）
- orphaned_heading_rate：孤立编号标题占比（孤立编号率，阈值上限 0.1）
- hallucination_keyword：越界 / 幻觉关键词命中（0 容忍）

## 各样本检查明细
### tarot_good（tarot）
- format_compliance: ✅ {'missing': []}
- orphaned_heading_rate: ✅ {'total': 0, 'orphaned': 0, 'rate': 0.0}
- hallucination_keyword: ✅ {'total_hits': 0, 'hits': []}

### tarot_bad_orphan（tarot）
- format_compliance: ✅ {'missing': []}
- orphaned_heading_rate: ❌ {'total': 1, 'orphaned': 1, 'rate': 1.0}
- hallucination_keyword: ✅ {'total_hits': 0, 'hits': []}

### tarot_bad_hallu（tarot）
- format_compliance: ✅ {'missing': []}
- orphaned_heading_rate: ✅ {'total': 0, 'orphaned': 0, 'rate': 0.0}
- hallucination_keyword: ❌ {'total_hits': 1, 'hits': [('作为人工智能', 1)]}

### bazi_good（bazi）
- format_compliance: ✅ {'missing': []}
- orphaned_heading_rate: ✅ {'total': 0, 'orphaned': 0, 'rate': 0.0}
- hallucination_keyword: ✅ {'total_hits': 0, 'hits': []}

### bazi_bad_missing（bazi）
- format_compliance: ❌ {'missing': ['大运']}
- orphaned_heading_rate: ✅ {'total': 0, 'orphaned': 0, 'rate': 0.0}
- hallucination_keyword: ✅ {'total_hits': 0, 'hits': []}

### generic_good（）
- orphaned_heading_rate: ✅ {'total': 0, 'orphaned': 0, 'rate': 0.0}
- hallucination_keyword: ✅ {'total_hits': 0, 'hits': []}
