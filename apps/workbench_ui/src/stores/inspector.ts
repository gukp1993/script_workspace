/**
 * 会话检视 store（UI-013）。
 *
 * 订阅控制面 WebSocket 事件流（VawSocket：last_seq 续传 + 自动重连），
 * 把事件流归约为检视视图状态：
 * - 最近感知字段表（perception_snapshot.payload.fields）；
 * - 当前状态与进入时刻（state_transition.payload.to / from）；
 * - 预算余量（executed.payload.budget_remaining_ms，缺省保持上次值）；
 * - 最近策略拒绝原因（policy_decision 且 allowed=false）。
 *
 * Dry/Shadow/Real 模式色带由视图层按 modeRisk 渲染，store 只保留 mode。
 */

import { defineStore } from 'pinia'

import { VawSocket, type WsEvent, type WsFactory, type WsState } from '../api/client'

/** 单个感知字段的最近值 */
export interface PerceptionField {
  name: string
  value: unknown
  /** 写入该值的事件 seq（展示新鲜度） */
  seq: number
}

/** 最近一次策略拒绝 */
export interface PolicyDenial {
  when: string
  reason: string
  seq: number
}

export function extractFields(event: WsEvent): PerceptionField[] {
  const fields = (event.payload as Record<string, unknown> | undefined)?.fields
  if (fields === null || typeof fields !== 'object' || Array.isArray(fields)) return []
  return Object.entries(fields as Record<string, unknown>).map(([name, value]) => ({
    name,
    value,
    seq: event.seq,
  }))
}

/** WS 订阅器：模块级保存，不进响应式状态（避免代理包装干扰回调） */
let socket: VawSocket | null = null

export const useInspectorStore = defineStore('inspector', {
  state: () => ({
    connected: false as boolean,
    wsState: 'closed' as WsState,
    mode: 'shadow',
    sessionId: null as string | null,
    fields: [] as PerceptionField[],
    currentState: null as string | null,
    /** 进入当前状态的事件 seq 与墙钟（用于显示"已进入 Xs"） */
    stateEnteredSeq: null as number | null,
    stateEnteredAt: 0,
    /** 预算余量（毫秒；尚未收到 executed 事件时为 null=未知） */
    budgetRemainingMs: null as number | null,
    lastDenial: null as PolicyDenial | null,
    executedCount: 0,
    lastSeq: 0,
    lastError: null as string | null,
  }),
  actions: {
    /** 事件流归约（也供 REST 兜底批量回放历史事件） */
    applyEvent(event: WsEvent): void {
      if (event.seq > this.lastSeq) this.lastSeq = event.seq
      switch (event.type) {
        case 'perception_snapshot': {
          const incoming = extractFields(event)
          if (incoming.length > 0) {
            // 同名字段用新值覆盖，保持字段首次出现顺序
            const byName = new Map(this.fields.map((f) => [f.name, { ...f }]))
            for (const f of incoming) byName.set(f.name, f)
            this.fields = [...byName.values()]
          }
          break
        }
        case 'state_transition': {
          const payload = event.payload as Record<string, unknown>
          const to = typeof payload.to === 'string' ? payload.to : null
          if (to) {
            this.currentState = to
            this.stateEnteredSeq = event.seq
            this.stateEnteredAt = Date.now()
          }
          break
        }
        case 'policy_decision': {
          const payload = event.payload as Record<string, unknown>
          if (payload.allowed === false) {
            this.lastDenial = {
              when: String(payload.when ?? '(未提供条件)'),
              reason: String(payload.reason ?? '策略拒绝'),
              seq: event.seq,
            }
          }
          break
        }
        case 'executed': {
          this.executedCount += 1
          const payload = event.payload as Record<string, unknown>
          if (typeof payload.budget_remaining_ms === 'number') {
            this.budgetRemainingMs = payload.budget_remaining_ms
          }
          break
        }
        default:
          break
      }
    },
    /** 会话上下文（模式色带与 WS 过滤） */
    bindSession(sessionId: string | null, mode: string): void {
      this.sessionId = sessionId
      this.mode = mode
      if (sessionId && this.currentState === null) {
        this.stateEnteredAt = Date.now()
      }
    },
    /** 打开事件订阅（幂等：已打开则先关闭重建） */
    start(
      options: {
        token?: string
        base?: string
        socketFactory?: WsFactory
        reconnectDelayMs?: number
        maxReconnectDelayMs?: number
      } = {},
    ): void {
      this.stop()
      const store = this
      socket = new VawSocket({
        token: options.token,
        base: options.base,
        socketFactory: options.socketFactory,
        reconnectDelayMs: options.reconnectDelayMs,
        maxReconnectDelayMs: options.maxReconnectDelayMs,
        getLastSeq: () => store.lastSeq,
        onEvent: (event) => store.applyEvent(event),
        onState: (state) => {
          store.wsState = state
          store.connected = state === 'open'
          if (state === 'resync') {
            // 缓冲溢出：下一次重连会从服务端最新 seq 续传；此处仅提示
            store.lastError = '事件缓冲溢出，正在按最新游标重新同步'
          }
        },
      })
      socket.open()
    },
    stop(): void {
      socket?.close()
      socket = null
      this.connected = false
      this.wsState = 'closed'
    },
    clearError(): void {
      this.lastError = null
    },
  },
})
