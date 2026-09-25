/**
 * 控制面 API 客户端（UI-001/002/003/004 共用）。
 *
 * 约定（对齐 control_plane.security，CTL-001/002）：
 * - 所有 `/api/v1/*` 请求自动携带 `X-VAW-Token` 请求头；
 * - 令牌来源优先级：URL query `?token=`（桌面壳注入）> localStorage；
 *   query 中的令牌会持久化到 localStorage，刷新后仍可用；
 * - 不注册任何跨域凭据；生产模式与后端同源（桌面壳内加载 dist/），
 *   dev 模式经 Vite 代理转发（见 vite.config.ts）；
 * - WS 令牌无法走请求头，经 query 传递：`/api/v1/ws?token=&last_seq=`
 *   （CTL-007），断线自动重连并从 last_seq 续传，缓冲溢出时回调
 *   resync 提示上层走 REST 全量兜底。
 */

export const TOKEN_QUERY_KEY = 'token'
export const TOKEN_STORAGE_KEY = 'vaw_token'
export const TOKEN_HEADER = 'X-VAW-Token'

/** 统一错误体（{"detail": {"error", "message", ...}}） */
export interface ApiErrorDetail {
  error: string
  message: string
  [key: string]: unknown
}

/** 带 HTTP 状态与机器可读错误码的 API 异常 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly detail: ApiErrorDetail | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

type MinimalStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

/** 无 window 环境（测试/SSR）兜底存储 */
const memoryStorage: MinimalStorage = (() => {
  const map = new Map<string, string>()
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  }
})()

function defaultStorage(): MinimalStorage {
  return typeof localStorage !== 'undefined' ? localStorage : memoryStorage
}

function currentSearch(): string {
  return typeof window !== 'undefined' ? window.location.search : ''
}

/** 从 URL query 提取令牌（无则 null） */
export function tokenFromQuery(query: string): string | null {
  return new URLSearchParams(query).get(TOKEN_QUERY_KEY)
}

/** 令牌解析：query 优先并持久化；否则回退 localStorage；都没有返回空串 */
export function resolveToken(
  query: string = currentSearch(),
  storage: MinimalStorage = defaultStorage(),
): string {
  const fromQuery = tokenFromQuery(query)
  if (fromQuery) {
    storage.setItem(TOKEN_STORAGE_KEY, fromQuery)
    return fromQuery
  }
  return storage.getItem(TOKEN_STORAGE_KEY) ?? ''
}

/** apiFetch 选项（依赖均可注入，便于测试） */
export interface ApiFetchOptions {
  method?: string
  /** JSON 请求体（自动序列化并设置 Content-Type） */
  body?: unknown
  /** URL 查询参数（undefined 的项被忽略） */
  params?: Record<string, string | number | boolean | undefined>
  /** 代替 location.search 的令牌来源（测试用） */
  tokenQuery?: string
  storage?: MinimalStorage
  fetchImpl?: typeof fetch
  /** 缺省同源相对路径；也可传完整后端地址 */
  baseUrl?: string
}

/** 把统一错误体解析为 ApiError；解析失败也给出去掉敏感细节的异常 */
async function toApiError(response: Response): Promise<ApiError> {
  let detail: ApiErrorDetail | null = null
  try {
    const body = (await response.json()) as { detail?: ApiErrorDetail | string }
    if (typeof body.detail === 'string') {
      detail = { error: 'error', message: body.detail }
    } else if (body.detail && typeof body.detail === 'object') {
      detail = body.detail
    }
  } catch {
    // 响应体不是 JSON：保持 detail 为 null
  }
  const code = detail?.error ?? `http_${response.status}`
  const message = detail?.message ?? `请求失败（HTTP ${response.status}）`
  return new ApiError(response.status, code, message, detail)
}

/** 通用 JSON 请求：自动注入令牌头；非 2xx 抛 ApiError */
export async function apiFetch<T = unknown>(path: string, options: ApiFetchOptions = {}): Promise<T> {
  const {
    method = 'GET',
    body,
    params,
    tokenQuery,
    storage = defaultStorage(),
    fetchImpl = fetch,
    baseUrl = '',
  } = options

  const url = new URL(path, baseUrl || currentOrigin())
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) url.searchParams.set(key, String(value))
    }
  }

  const headers: Record<string, string> = { [TOKEN_HEADER]: resolveToken(tokenQuery, storage) }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const response = await fetchImpl(url.toString(), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) throw await toApiError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

function currentOrigin(): string {
  return typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1'
}

/** 抓取预览帧（二进制 JPEG；apiFetch 之外的旁路，同样带令牌） */
export async function fetchPreviewShot(path: string, options: ApiFetchOptions = {}): Promise<Blob> {
  return fetchBinary(path, options)
}

/** 通用带令牌二进制 GET（帧图/资产内容等 blob 旁路；非 2xx 抛 ApiError） */
export async function fetchBinary(path: string, options: ApiFetchOptions = {}): Promise<Blob> {
  const {
    params,
    tokenQuery,
    storage = defaultStorage(),
    fetchImpl = fetch,
    baseUrl = '',
  } = options
  const url = new URL(path, baseUrl || currentOrigin())
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) url.searchParams.set(key, String(value))
    }
  }
  const response = await fetchImpl(url.toString(), {
    method: 'GET',
    headers: { [TOKEN_HEADER]: resolveToken(tokenQuery, storage) },
  })
  if (!response.ok) throw await toApiError(response)
  return await response.blob()
}

// ---------------------------------------------------------------------------
// M3 E11 端点封装：轨迹 / 帧 / 回放报告（UI-013/014/015）与设置（UI-016）
// ---------------------------------------------------------------------------

/** 轨迹文件概览行（control_plane.traces.list_traces） */
export interface TraceSummary {
  name: string
  size_bytes: number
  modified_at: string
  event_count: number
}

export interface TraceListResponse {
  project_id: string
  traces: TraceSummary[]
  count: number
}

/** 轨迹事件（trace_format.TraceEvent.to_dict） */
export interface TraceEventRecord {
  seq: number
  ts_monotonic: number
  correlation_id: string
  session_id: string
  type: string
  payload: Record<string, unknown>
  prev_hash: string
  hash: string
}

export interface TraceEventsResponse {
  project_id: string
  trace: string
  events: TraceEventRecord[]
  returned: number
  matched: number
  total: number
  truncated_tail: boolean
  error_count: number
}

/** 事件过滤参数（语义与 control_plane.traces / query_timeline 对齐） */
export interface TraceEventsParams {
  since?: number
  until?: number
  /** 逗号分隔的事件类型 */
  types?: string
  state?: string
  limit?: number
}

/** 回放/差异报告概览行 */
export interface ReplayReportSummary {
  name: string
  rel_path: string
  size_bytes: number
  modified_at: string
  preview: string
}

export interface ReplayReportsResponse {
  project_id: string
  reports: ReplayReportSummary[]
  count: number
}

/** 工作区设置（CTL-008） */
export interface WorkbenchSettings {
  default_mode: string
  trace_retention_days: number
  unattended_schedule: string
}

/** 列出项目 traces/ 目录下的轨迹文件 */
export function listProjectTraces(projectId: string, options: ApiFetchOptions = {}): Promise<TraceListResponse> {
  return apiFetch<TraceListResponse>(`/api/v1/projects/${projectId}/traces`, options)
}

/** 读取轨迹事件（可带过滤参数） */
export function fetchTraceEvents(
  projectId: string,
  name: string,
  params: TraceEventsParams = {},
  options: ApiFetchOptions = {},
): Promise<TraceEventsResponse> {
  return apiFetch<TraceEventsResponse>(`/api/v1/projects/${projectId}/traces/${name}/events`, {
    ...options,
    params: { ...params },
  })
}

/** 读取轨迹事件关联帧（PNG blob；ref 为 64 位 sha256） */
export function fetchTraceFrame(
  projectId: string,
  name: string,
  ref: string,
  options: ApiFetchOptions = {},
): Promise<Blob> {
  return fetchBinary(`/api/v1/projects/${projectId}/traces/${name}/frame/${ref}`, options)
}

/** 读取资产内容（PNG/JPEG blob，用于资产库缩略图） */
export function fetchAssetBytes(projectId: string, assetId: string, options: ApiFetchOptions = {}): Promise<Blob> {
  return fetchBinary(`/api/v1/projects/${projectId}/assets/${assetId}/content`, options)
}

/** 列出项目 tests/replay 目录下的回放/差异报告 */
export function listReplayReports(projectId: string, options: ApiFetchOptions = {}): Promise<ReplayReportsResponse> {
  return apiFetch<ReplayReportsResponse>(`/api/v1/projects/${projectId}/replay-reports`, options)
}

/** 读取回放/差异报告全文 */
export function fetchReplayReportContent(
  projectId: string,
  name: string,
  options: ApiFetchOptions = {},
): Promise<{ project_id: string; name: string; content: string; size_bytes: number }> {
  return apiFetch(`/api/v1/projects/${projectId}/replay-reports/${name}/content`, options)
}

/** 读取工作区设置（文件缺失/损坏时后端回退安全默认值） */
export function fetchSettings(options: ApiFetchOptions = {}): Promise<WorkbenchSettings> {
  return apiFetch<WorkbenchSettings>('/api/v1/settings', options)
}

/** 局部更新工作区设置（受保护目标硬锁由后端 409 拒绝） */
export function updateSettings(patch: Partial<WorkbenchSettings>, options: ApiFetchOptions = {}): Promise<WorkbenchSettings> {
  return apiFetch<WorkbenchSettings>('/api/v1/settings', { ...options, method: 'PUT', body: patch })
}

// ---------------------------------------------------------------------------
// WebSocket（CTL-007）：last_seq 续传 + 指数退避重连 + resync 兜底
// ---------------------------------------------------------------------------

/** 服务端事件帧（events.Event.to_dict） */
export interface WsEvent {
  seq: number
  type: string
  payload: Record<string, unknown>
  session_id: string | null
  ts: string
}

export type WsState = 'connecting' | 'open' | 'closed' | 'resync'

/** 可注入的极简 WebSocket 形状（真实 WebSocket 天然满足） */
export interface WsLike {
  close(): void
  send(data: string): void
  onopen: ((ev: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  onclose: ((ev: unknown) => void) | null
  onerror: ((ev: unknown) => void) | null
}

export type WsFactory = (url: string) => WsLike

/** 拼接 WS 地址：/api/v1/ws?token=<t>&last_seq=<n>（http->ws、https->wss） */
export function buildWsUrl(base: string, token: string, lastSeq: number): string {
  const url = new URL('/api/v1/ws', base)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.searchParams.set(TOKEN_QUERY_KEY, token)
  url.searchParams.set('last_seq', String(Math.max(0, Math.trunc(lastSeq))))
  return url.toString()
}

export interface VawSocketOptions {
  /** 令牌（缺省用 resolveToken()） */
  token?: string
  /** WS 基地址（缺省当前 origin）；如 http://127.0.0.1:17653 */
  base?: string
  /** 取续传游标（通常读 store 里保存的最新 seq） */
  getLastSeq: () => number
  onEvent: (event: WsEvent) => void
  onState?: (state: WsState) => void
  /** 缓冲溢出（resync 帧）：上层应改用 GET /api/v1/events 全量兜底 */
  onResync?: (latestSeq: number) => void
  /** 注入 socket 工厂（测试用） */
  socketFactory?: WsFactory
  /** 初始重连延迟（默认 500ms，指数退避） */
  reconnectDelayMs?: number
  /** 重连延迟上限（默认 5000ms） */
  maxReconnectDelayMs?: number
}

/** 服务端控制帧类型（不作为事件派发） */
const CONTROL_FRAME_TYPES: ReadonlySet<string> = new Set(['pong', 'subscribed', 'error', 'resync'])

/** 事件订阅器：自动重连、last_seq 续传、resync 通知 */
export class VawSocket {
  private socket: WsLike | null = null
  private closedByUser = false
  private attempts = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null

  constructor(private readonly options: VawSocketOptions) {}

  open(): void {
    this.closedByUser = false
    this.connect()
  }

  close(): void {
    this.closedByUser = true
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.socket?.close()
    this.socket = null
    this.options.onState?.('closed')
  }

  /** 当前连接状态 */
  get state(): WsState {
    return this.socket ? 'open' : this.closedByUser ? 'closed' : 'connecting'
  }

  private connect(): void {
    if (this.closedByUser) return
    const token = this.options.token ?? resolveToken()
    const base = this.options.base ?? currentOrigin()
    const url = buildWsUrl(base, token, this.options.getLastSeq())
    this.options.onState?.('connecting')
    const socket = (this.options.socketFactory ?? defaultWsFactory)(url)
    this.socket = socket

    socket.onopen = () => {
      this.attempts = 0
      this.options.onState?.('open')
    }
    socket.onmessage = (ev) => this.handleMessage(ev.data)
    socket.onclose = () => {
      this.socket = null
      if (this.closedByUser) return
      this.options.onState?.('closed')
      this.scheduleReconnect()
    }
    socket.onerror = () => {
      // onerror 后通常伴随 onclose；此处不重复调度
    }
  }

  private handleMessage(data: unknown): void {
    if (typeof data !== 'string') return
    let parsed: unknown
    try {
      parsed = JSON.parse(data)
    } catch {
      return // 非 JSON 帧忽略
    }
    if (parsed === null || typeof parsed !== 'object') return
    const frame = parsed as Record<string, unknown>
    if (frame.type === 'resync') {
      // 缓冲窗口之外：通知上层全量兜底，游标跳到服务端最新 seq
      const latestSeq = typeof frame.seq === 'number' ? frame.seq : this.options.getLastSeq()
      this.options.onState?.('resync')
      this.options.onResync?.(latestSeq)
      return
    }
    if (typeof frame.seq === 'number' && typeof frame.type === 'string' && !CONTROL_FRAME_TYPES.has(frame.type)) {
      this.options.onEvent(frame as unknown as WsEvent)
    }
    // 其余控制帧（pong/subscribed/error）交由上层按需处理，这里不抛
  }

  private scheduleReconnect(): void {
    if (this.closedByUser || this.reconnectTimer !== null) return
    const base = this.options.reconnectDelayMs ?? 500
    const max = this.options.maxReconnectDelayMs ?? 5000
    const delay = Math.min(max, base * 2 ** this.attempts)
    this.attempts += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }
}

function defaultWsFactory(url: string): WsLike {
  if (typeof WebSocket === 'undefined') {
    throw new Error('当前环境不支持 WebSocket')
  }
  return new WebSocket(url) as WsLike
}
