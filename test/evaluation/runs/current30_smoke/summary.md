# Agentic Wiki Eval Summary

- Overall final_score: 0.9016
- Overall answer_confidence: 0.9233
- Overall citation_grounding: 1.0
- Pass rate: 1.0
- Retrieval hit rate: 1.0
- Top1 hit rate: 1.0
- Wrong web usage rate: 0.0
- No citation rate: 0.0

## Splits

### current_papers_30
- count: 6
- overall_final_score: 0.9016
- overall_answer_confidence: 0.9233
- overall_citation_grounding: 1.0

## Top 10 failed cases

- paper_016 | final_score=0.776 | answer_confidence=0.78 | wiki_card_content_too_weak
- paper_026 | final_score=0.776 | answer_confidence=0.78 | wiki_card_content_too_weak
- paper_021 | final_score=0.9327 | answer_confidence=0.98 | wiki_card_content_too_weak
- paper_001 | final_score=0.975 | answer_confidence=1.0 | wiki_card_content_too_weak
- paper_006 | final_score=0.975 | answer_confidence=1.0 | wiki_card_content_too_weak
- paper_011 | final_score=0.975 | answer_confidence=1.0 | wiki_card_content_too_weak

## Recommended fixes

- retrieval_alias_missing: add aliases or expected topic rewrites
- retrieval_rank_wrong: improve wiki_search ranking or query rewrite
- wiki_card_content_too_weak: strengthen distilled card fields
- tool_routing_wrong: adjust tool router or deterministic fallback
- answer_synthesis_unfaithful: tighten answer prompt and citation usage
- citation_grounding_weak: improve citation discipline and card selection
