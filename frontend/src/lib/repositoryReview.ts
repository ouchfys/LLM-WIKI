import type { RepositoryReview } from '../api'

export function repositoryReviewLabel(review?: RepositoryReview | null): string {
  // This policy requests author self-checking in the writing prompt; no verifier
  // runs, so a saved card must not acquire a review or self-check guarantee.
  if (review?.policy === 'author_self_check') {
    return ''
  }
  if (review?.status === 'content_changed') {
    return '正文已变更，当前版本未复核'
  }
  if (review?.status === 'complete'
    && review.independent_status === 'passed'
    && review.current_version_reviewed === true) {
    return '独立复核通过'
  }
  if (review?.status === 'author_revised' && review.independent_status === 'changes_requested') {
    return '已按复核意见修正'
  }
  if (review?.status === 'author_revised' && review.independent_status === 'passed') {
    return '主模型修订（当前版本未独立复核）'
  }
  if (review?.status === 'author_checked') {
    return '主模型自查（独立复核未完成）'
  }
  if (review?.status === 'incomplete') {
    return '独立复核未完成'
  }
  return '复核状态未确认'
}
