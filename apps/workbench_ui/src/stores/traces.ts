/**
 * 轨迹/时间轴 store（UI-014）。
 *
 * 负责轨迹列表、事件拉取（带过滤参数）与选中事件；泳道分组是纯 getter
 * （perception / state / intent+policy / executed 四行），视图只做渲染。
 * 所有 action 支持注入 fetch（沿用 project/session store 风格），便于单测。
 */

import { defineStore } from 'pinia'

import { apiFetch, type TraceEventRecord, type TraceSummary } from '../api/client'

type Fetcher = typeof apiFetch

/** 时间轴泳道定义：标题 + 归入该泳道的事件类型集合 */
export const TIMELINE_LANES: ReadonlyArray<{ key: string; label: string; types: ReadonlySet<string> }> = [
  { key: 'perception', label: '感知', types: new Set(['perception_snapshot', 'frame_captured']) },
  { key: 'state', label: '状态', types: new Set(['state_transition']) },
  { key: 'policy', label: '意图/策略', types: new Set(['intent_issued', 'policy_decision']) },
  { key: 'executed', label: '执行', types: new Set(['executed', 'estop', 'anomaly']) },
]

/** 事件 -> 泳道 key（未归类的进入 executed 泳道兜底） */
export function laneForEvent(type: string): string {
  for (const lane of TIMELINE_LANES) {
    if (lane.types.has(type)) return lane.key
  }
  return 'executed'
}

/** 泳道视图模型：事件 + 归一化横坐标（0~1，按事件时间范围） */
export interface LaneItem {
  event: TraceEventRecord
  ratio: number
}

export interface LaneView {
  key: string
  label: string
  items: LaneItem[]
}

export interface TraceFilters {
  since: number | undefined
  until: number | undefined
  types: string
  state: string
  limit: number
}

interface TraceListResponse {
  project_id: string
  traces: TraceSummary[]
  count: number
}

interface TraceEventsResponse {
  project_id: string
  trace: string
  events: TraceEventRecord[]
  returned: number
  matched: number
  total: number
  truncated_tail: boolean
  error_count: number
}

function buildQuery(filters: TraceFilters): string {
  const params = new URLSearchParams()
  if (filters.since !== undefined) params.set('since', String(filters.since))
  if (filters.until !== undefined) params.set('until', String(filters.until))
  if (filters.types) params.set('types', filters.types)
  if (filters.state) params.set('state', filters.state)
  params.set('limit', String(filters.limit))
  return params.toString()
}

export const useTracesStore = defineStore('traces', {
  state: () => ({
    projectId: null as string | null,
    traces: [] as TraceSummary[],
    selectedTrace: null as string | null,
    events: [] as TraceEventRecord[],
    matched: 0,
    total: 0,
    truncatedTail: false,
    filters: {
      since: undefined,
      until: undefined,
      types: '',
      state: '',
      limit: 500,
    } as TraceFilters,
    selectedSeq: null as number | null,
    loading: false,
    loadingEvents: false,
    error: null as string | null,
  }),
  getters: {
    selectedEvent(state): TraceEventRecord | null {
      if (state.selectedSeq === null) return null
      return state.events.find((e) => e.seq === state.selectedSeq) ?? null
    },
    /** 四行泳道视图（items 按 seq 排序，ratio 为 0~1 的归一化横坐标） */
    lanes(state): LaneView[] {
      const ts = state.events.map((e) => e.ts_monotonic)
      const lo = ts.length > 0 ? Math.min(...ts) : 0
      const hiRaw = ts.length > 0 ? Math.max(...ts) : 1
      const hi = hiRaw > lo ? hiRaw : lo + 1
      return TIMELINE_LANES.map((lane) => ({
        key: lane.key,
        label: lane.label,
        items: state.events
          .filter((e) => lane.types.has(e.type))
          .slice()
          .sort((a, b) => a.seq - b.seq)
          .map((event) => ({ event, ratio: (event.ts_monotonic - lo) / (hi - lo) })),
      }))
    },
  },
  actions: {
    selectEvent(seq: number | null): void {
      this.selectedSeq = seq
    },
    setFilter(patch: Partial<TraceFilters>): void {
      this.filters = { ...this.filters, ...patch }
    },
    async loadTraces(projectId: string, fetch: Fetcher = apiFetch): Promise<void> {
      this.projectId = projectId
      this.loading = true
      this.error = null
      try {
        const data = await fetch<TraceListResponse>(`/api/v1/projects/${projectId}/traces`)
        this.traces = data.traces
        if (this.selectedTrace === null || !data.traces.some((t) => t.name === this.selectedTrace)) {
          this.selectedTrace = data.traces[0]?.name ?? null
        }
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
      } finally {
        this.loading = false
      }
    },
    async loadEvents(fetch: Fetcher = apiFetch): Promise<void> {
      const pid = this.projectId
      const name = this.selectedTrace
      if (!pid || !name) {
        this.events = []
        return
      }
      this.loadingEvents = true
      this.error = null
      try {
        const data = await fetch<TraceEventsResponse>(
          `/api/v1/projects/${pid}/traces/${name}/events?${buildQuery(this.filters)}`,
        )
        this.events = data.events
        this.matched = data.matched
        this.total = data.total
        this.truncatedTail = data.truncated_tail
        // 选中事件被过滤掉时清空选择（详情面板随之关闭）
        if (this.selectedSeq !== null && !data.events.some((e) => e.seq === this.selectedSeq)) {
          this.selectedSeq = null
        }
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
      } finally {
        this.loadingEvents = false
      }
    },
  },
})
