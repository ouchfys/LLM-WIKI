export type Citation = {
  card_id: string
  title: string
  page_type: string
  summary: string
  markdown_path: string
}

export type LearningResource = {
  category: string
  title: string
  url: string
  snippet?: string
}

export type ToolEventItem = {
  card_id?: string
  title?: string
  page_type?: string
  summary?: string
  markdown_path?: string
  score?: number
  match_reason?: string
  matched_sections?: Array<{ section?: string; snippet?: string; route?: string }>
  source_title?: string
  page?: number
  url?: string
  snippet?: string
  section?: string
  text?: string
  evidence_kind?: string
}

export type ToolEvent = {
  eventId?: string
  tool: string
  label: string
  status: 'running' | 'done' | 'error'
  detail?: string
  query?: string
  reason?: string
  items?: ToolEventItem[]
  arguments?: Record<string, unknown>
  output_preview?: unknown
  duration_ms?: number
  result_id?: string | number
}

export type ProgressEvent = {
  type: 'progress'
  event_id?: string
  text: string
  phase?: string
  timestamp?: string
}

export type ToolStatusEvent = Omit<ToolEvent, 'eventId'> & {
  type: 'tool_status'
  event_id?: string
  timestamp?: string
}

export type TraceTimelineEvent = ProgressEvent | ToolStatusEvent

export type ToolPlan = {
  intent?: string
  answer_mode?: string
  use_wiki?: boolean
  use_web?: boolean
  use_resources?: boolean
  open_cards?: boolean
  tools?: Array<{ name: string; query: string; reason?: string }>
}

export type TraceCard = {
  card_id: string
  title: string
  page_type: string
  summary: string
  markdown_path: string
  matched_chunks?: string[]
}

export type AgentTrace = {
  task_outcome?: {
    status: 'partial' | 'model_finished'
    stop_reason: string
    unfinished_plan_ids: string[]
    committed_wiki: Array<{ card_id: string; title?: string; repository?: string; revision_id?: string; verified_readback: boolean }>
  }
  thinking_effort?: 'none' | 'low' | 'high' | 'max'
  research_state?: {
    mode: 'chat' | 'research'
    status: 'researching' | 'complete' | 'insufficient' | 'conflicted' | 'budget_exhausted' | 'not_required'
    reason: string
    questions: Array<{ id: string; question: string; status: 'missing' | 'partial' | 'supported' | 'conflict'; answer: string }>
  }
  timeline?: TraceTimelineEvent[]
  context_budget?: {
    input_tokens_estimate?: number
    window?: number
    input_limit?: number
    counter?: string
    compactions?: Array<{ status: string }>
  }
  tool_plan?: ToolPlan
  tool_observations?: Array<{
    tool: string
    query?: string
    status: 'running' | 'done' | 'error'
    summary?: string
    items?: ToolEventItem[]
    event_id?: string
    arguments?: Record<string, unknown>
    output_preview?: unknown
    duration_ms?: number
    result_id?: string | number
  }>
  retrieved_cards?: TraceCard[]
  web_results?: Array<{ title: string; url: string; snippet?: string }>
  resources?: LearningResource[]
  diagnostics?: {
    wiki_card_count?: number
    wiki_page_count?: number
    web_result_count?: number
    resource_count?: number
  }
  runtime?: {
    run_id?: string
    status?: string
    current_state?: string
    wall_time_ms?: number
    model_calls?: number
    model_failures?: number
    model_duration_ms?: number
    tool_calls?: number
    tool_failures?: number
    tool_duration_ms?: number
    retry_count?: number
    token_usage?: {
      prompt_tokens?: number
      completion_tokens?: number
      total_tokens?: number
      estimated_calls?: number
      contains_estimates?: boolean
    }
  }
}

export type ChatMessage = {
  id: number
  role: 'user' | 'assistant'
  content: string
  citations?: Citation[]
  resources?: LearningResource[]
  toolEvents?: ToolEvent[]
  timeline?: TraceTimelineEvent[]
  toolPlan?: ToolPlan
  trace?: AgentTrace
  profileUpdates?: Array<{ signal_type: string; value: string }>
}

export type SlashCommand = {
  name: 'wiki' | 'compact' | 'purpose'
  argument: string
}

export type RunInput = {
  id: string
  run_id: string
  kind: 'interrupt' | 'followup' | 'command'
  content: string
  status: string
}

export type SseChunk =
  | { type: 'session_updated'; session_id: string; title: string }
  | ProgressEvent
  | ToolStatusEvent
  | { type: 'run_started'; run_id: string }
  | { type: 'heartbeat'; run_id: string }
  | { type: 'phase'; phase: string; detail: string }
  | { type: 'answer_reset'; reason: string; input_ids?: string[]; detail?: string }
  | { type: 'cancelled'; run_id: string; message: string }
  | { type: 'paused'; run_id: string; message: string }
  | { type: 'queue_update'; items: RunInput[] }
  | { type: 'card_list'; citations?: Citation[] }
  | { type: 'resource_list'; resources?: LearningResource[] }
  | { type: 'tool_plan'; plan?: ToolPlan }
  | { type: 'agent_trace'; trace?: AgentTrace }
  | { type: 'token'; text?: string }
  | { type: 'profile'; updates?: Array<{ signal_type: string; value: string }> }
  | { type: 'error'; message?: string }
  | { type: 'done'; cancelled?: boolean }

export type ChatSession = { id: string; title: string; created_at?: string }
export type StoredChatMessage = Omit<ChatMessage, 'toolEvents' | 'toolPlan' | 'profileUpdates'> & {
  tool_plan?: ToolPlan
  profile_updates?: Array<{ signal_type: string; value: string }>
}
export type ChatHistory = { session?: ChatSession; items?: StoredChatMessage[] }
export type RunInputList = { items?: RunInput[] }
