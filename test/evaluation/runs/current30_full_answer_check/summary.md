# Agentic Wiki Eval Summary

- Overall final_score: 0.8287
- Overall answer_confidence: 0.8632
- Overall citation_grounding: 1.0
- Pass rate: 0.8333
- Retrieval hit rate: 1.0
- Top1 hit rate: 0.7667
- Wrong web usage rate: 0.2667
- No citation rate: 0.0

## Splits

### current_papers_30
- count: 30
- overall_final_score: 0.8287
- overall_answer_confidence: 0.8632
- overall_citation_grounding: 1.0

## Top 10 failed cases

- paper_028 | final_score=0.66 | answer_confidence=0.7 | tool_routing_wrong
- paper_016 | final_score=0.696 | answer_confidence=0.78 | tool_routing_wrong
- paper_018 | final_score=0.696 | answer_confidence=0.78 | tool_routing_wrong
- paper_013 | final_score=0.7383 | answer_confidence=0.8 | tool_routing_wrong
- paper_024 | final_score=0.7383 | answer_confidence=0.8 | tool_routing_wrong
- paper_006 | final_score=0.7743 | answer_confidence=0.88 | tool_routing_wrong
- paper_010 | final_score=0.7743 | answer_confidence=0.88 | tool_routing_wrong
- paper_009 | final_score=0.776 | answer_confidence=0.78 | wiki_card_content_too_weak
- paper_017 | final_score=0.776 | answer_confidence=0.78 | wiki_card_content_too_weak
- paper_019 | final_score=0.776 | answer_confidence=0.78 | wiki_card_content_too_weak

## Recommended fixes

- retrieval_alias_missing: add aliases or expected topic rewrites
- retrieval_rank_wrong: improve wiki_search ranking or query rewrite
- wiki_card_content_too_weak: strengthen distilled card fields
- tool_routing_wrong: adjust tool router or deterministic fallback
- answer_synthesis_unfaithful: tighten answer prompt and citation usage
- citation_grounding_weak: improve citation discipline and card selection
