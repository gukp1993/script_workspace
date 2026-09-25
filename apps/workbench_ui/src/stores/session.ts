/**
 * 会话生命周期 store（UI-012 控制条 / UI-003 会话确认）。
 *
 * 约定（CTL-005/006、POL-002）：
 * - 模式明显区分：state 里保留 mode，UI 用 modeRisk() 渲染四色；
 * - real_input 需人工闸门：创建后处于 gate_wait，调用 confirm() 显式放行；
 * - **停止永远可用**（stopAlwaysEnabled 恒真），停止期间展示清理进度文案，
 *   完成后保留最终文案供 UI 显示；
 * - 事件游标：latestSeq 供 VawSocket 断线续传。
 */

import { defineStore } from 'pinia'

import { apiFetch } from '../api/client'

export interface SessionRecord {
  session_id: string
  project_id: string
  target_id: string
  mode: string
  state: string
  gate?: { status?: string; confirmed_by?: string | null } | null
  [key: string]: unknown
}

type Fetcher = typeof apiFetch

interface SessionResponse extends SessionRecord {
  [key: string]: unknown
}

export const useSessionStore = defineStore('session', {
  state: () => ({
    current: null as SessionRecord | null,
    latestSeq: 0,
    /** 任一变更请求进行中（用于按钮防抖） */
    busy: false,
    /** 清理进度文案：停止请求进行中显示"正在清理…"，完成后保留"清理完成" */
    cleaning: null as string | null,
    error: null as string | null,
  }),
  getters: {
    /** UI-012：停止按钮永远可点 */
    stopAlwaysEnabled: (): boolean => true,
    canStart(state): boolean {
      // 后端约定：confirm 只填 gate 不改 state，start 允许 created（real_input 需 gate）
      return state.current?.state === 'created' && !state.busy
    },
    canPause(state): boolean {
      return (state.current?.state === 'running' || state.current?.state === 'resumed') && !state.busy
    },
    canResume(state): boolean {
      return state.current?.state === 'paused' && !state.busy
    },
    canCreate(state): boolean {
      return state.current === null && !state.busy
    },
    /** 是否等待人工确认（real_input 闸门：created 且尚未确认） */
    awaitingConfirm(state): boolean {
      const cur = state.current
      return cur !== null && cur.mode === 'real_input' && cur.state === 'created' && !cur.gate
    },
  },
  actions: {
    applySession(record: SessionRecord): void {
      this.current = record
    },
    async create(projectId: string, targetId: string, mode: string, fetch: Fetcher = apiFetch): Promise<SessionRecord | null> {
      this.error = null
      this.busy = true
      try {
        const record = await fetch<SessionResponse>('/api/v1/sessions', {
          method: 'POST',
          body: { project_id: projectId, target_id: targetId, mode },
        })
        this.applySession(record)
        return record
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
        return null
      } finally {
        this.busy = false
      }
    },
    async confirm(operator: string, fetch: Fetcher = apiFetch): Promise<void> {
      const sid = this.current?.session_id
      if (!sid) return
      this.error = null
      this.busy = true
      try {
        const record = await fetch<SessionResponse>(`/api/v1/sessions/${sid}/confirm`, {
          method: 'POST',
          body: { operator },
        })
        this.applySession(record)
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
      } finally {
        this.busy = false
      }
    },
    async start(fetch: Fetcher = apiFetch): Promise<void> {
      await this._transition('start', fetch)
    },
    async pause(fetch: Fetcher = apiFetch): Promise<void> {
      await this._transition('pause', fetch)
    },
    async resume(fetch: Fetcher = apiFetch): Promise<void> {
      await this._transition('resume', fetch)
    },
    /**
     * 停止会话（UI-012）：无论当前状态都可调用；进行中展示清理文案。
     */
    async stop(fetch: Fetcher = apiFetch): Promise<void> {
      const sid = this.current?.session_id
      if (!sid) return
      this.error = null
      this.cleaning = '正在清理：停止输入注入、回收采集与调度资源…'
      this.busy = true
      try {
        const record = await fetch<SessionResponse>(`/api/v1/sessions/${sid}/stop`, { method: 'POST' })
        this.applySession(record)
        this.cleaning = '清理完成：会话已安全停止'
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
        this.cleaning = null
      } finally {
        this.busy = false
      }
    },
    async _transition(action: 'start' | 'pause' | 'resume', fetch: Fetcher): Promise<void> {
      const sid = this.current?.session_id
      if (!sid) return
      this.error = null
      this.busy = true
      try {
        const record = await fetch<SessionResponse>(`/api/v1/sessions/${sid}/${action}`, { method: 'POST' })
        this.applySession(record)
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
      } finally {
        this.busy = false
      }
    },
  },
})
