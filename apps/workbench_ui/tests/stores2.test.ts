import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiFetch, type WsEvent, type WsLike } from '../src/api/client'
import { useInspectorStore } from '../src/stores/inspector'
import { isHighRiskPatch, useSettingsStore } from '../src/stores/settings'
import { laneForEvent, useTracesStore } from '../src/stores/traces'

/** 构造按路径响应的 apiFetch 替身 */
function routerFetch(routes: Record<string, (init: RequestInit) => unknown>) {
  return vi.fn(async (path: string, init?: RequestInit) => {
    const handler = routes[path]
    if (!handler) throw new Error(`unexpected path: ${path}`)
    return handler(init ?? {})
  }) as unknown as typeof apiFetch
}

function wsEvent(seq: number, type: string, payload: Record<string, unknown> = {}): WsEvent {
  return { seq, type, payload, session_id: 'sess-1', ts: '2026-09-25T00:00:00Z' }
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('tracesStore（UI-014 时间轴）', () => {
  const TRACE_LIST = {
    project_id: 'demo',
    traces: [{ name: 'trace-a.jsonl', size_bytes: 10, modified_at: '2026-09-25T00:00:00+00:00', event_count: 4 }],
    count: 1,
  }

  it('loadTraces 自动选中第一条；loadEvents 携带过滤参数并记录 matched/total', async () => {
    const store = useTracesStore()
    const seenUrls: string[] = []
    const EVENTS = {
      project_id: 'demo',
      trace: 'trace-a.jsonl',
      events: [
        { seq: 0, ts_monotonic: 0, correlation_id: '', session_id: 's', type: 'perception_snapshot', payload: {}, prev_hash: '0'.repeat(64), hash: 'h0' },
        { seq: 1, ts_monotonic: 1, correlation_id: '', session_id: 's', type: 'state_transition', payload: {}, prev_hash: 'h0', hash: 'h1' },
      ],
      returned: 2,
      matched: 2,
      total: 9,
      truncated_tail: false,
      error_count: 0,
    }
    const fetchImpl = vi.fn(async (path: string) => {
      seenUrls.push(path)
      if (path === '/api/v1/projects/demo/traces') return TRACE_LIST
      if (path.startsWith('/api/v1/projects/demo/traces/trace-a.jsonl/events')) return EVENTS
      throw new Error(`unexpected path: ${path}`)
    }) as unknown as typeof apiFetch

    await store.loadTraces('demo', fetchImpl)
    expect(store.traces).toHaveLength(1)
    expect(store.selectedTrace).toBe('trace-a.jsonl')

    store.setFilter({ since: 0.5, types: 'state_transition', limit: 50 })
    await store.loadEvents(fetchImpl)
    const eventsUrl = seenUrls.find((u) => u.includes('/events')) ?? ''
    expect(eventsUrl).toContain('since=0.5')
    expect(eventsUrl).toContain('types=state_transition')
    expect(eventsUrl).toContain('limit=50')
    expect(store.events).toHaveLength(2)
    expect(store.total).toBe(9)
    expect(store.error).toBeNull()
  })

  it('lanes 泳道分组：四行、ratio 归一化、laneForEvent 兜底', async () => {
    const store = useTracesStore()
    const EVENTS = {
      project_id: 'demo',
      trace: 'trace-a.jsonl',
      events: [
        { seq: 0, ts_monotonic: 0, correlation_id: '', session_id: 's', type: 'perception_snapshot', payload: {}, prev_hash: 'h', hash: 'h' },
        { seq: 1, ts_monotonic: 5, correlation_id: '', session_id: 's', type: 'state_transition', payload: {}, prev_hash: 'h', hash: 'h' },
        { seq: 2, ts_monotonic: 10, correlation_id: '', session_id: 's', type: 'policy_decision', payload: {}, prev_hash: 'h', hash: 'h' },
        { seq: 3, ts_monotonic: 15, correlation_id: '', session_id: 's', type: 'executed', payload: {}, prev_hash: 'h', hash: 'h' },
      ],
      returned: 4,
      matched: 4,
      total: 4,
      truncated_tail: false,
      error_count: 0,
    }
    const fetchImpl = vi.fn(async (path: string) => {
      if (path === '/api/v1/projects/demo/traces') return TRACE_LIST
      if (path.startsWith('/api/v1/projects/demo/traces/trace-a.jsonl/events')) {
        // 尊重 types 过滤参数（store 侧清空选中逻辑依赖过滤结果变化）
        const types = new URL(path, 'http://localhost').searchParams.get('types')
        const events = types ? EVENTS.events.filter((e) => e.type === types) : EVENTS.events
        return { ...EVENTS, events, returned: events.length, matched: events.length }
      }
      throw new Error(`unexpected path: ${path}`)
    }) as unknown as typeof apiFetch
    await store.loadTraces('demo', fetchImpl)
    await store.loadEvents(fetchImpl)

    const lanes = store.lanes
    expect(lanes.map((l) => l.key)).toEqual(['perception', 'state', 'policy', 'executed'])
    expect(lanes[0].items[0].ratio).toBe(0)
    expect(lanes[3].items[0].ratio).toBe(1)
    // 点击选择 -> selectedEvent；过滤掉选中事件时清空选择
    store.selectEvent(1)
    expect(store.selectedEvent?.seq).toBe(1)
    store.setFilter({ types: 'executed' })
    await store.loadEvents(fetchImpl)
    expect(store.selectedSeq).toBeNull()
    expect(laneForEvent('estop')).toBe('executed')
    expect(laneForEvent('frame_captured')).toBe('perception')
  })
})

describe('settingsStore（UI-016 二次确认逻辑）', () => {
  it('isHighRiskPatch：real_input 默认模式 / 开启无人值守算高风险', () => {
    expect(isHighRiskPatch({ default_mode: 'real_input' })).toBe(true)
    expect(isHighRiskPatch({ unattended_schedule: 'enabled' })).toBe(true)
    expect(isHighRiskPatch({ default_mode: 'shadow', trace_retention_days: 3 })).toBe(false)
    expect(isHighRiskPatch({})).toBe(false)
  })

  it('高风险补丁未二次确认 -> 拒绝且不发请求；armHighRisk 后放行并解除 armed', async () => {
    const store = useSettingsStore()
    let saved: Record<string, unknown> | null = null
    const fetchImpl = routerFetch({
      'GET /api/v1/settings': () => ({ default_mode: 'shadow', trace_retention_days: 7, unattended_schedule: 'disabled' }),
      'PUT /api/v1/settings': (init) => {
        saved = init.body as unknown as Record<string, unknown>
        return { default_mode: 'real_input', trace_retention_days: 7, unattended_schedule: 'disabled' }
      },
    })
    const routed = vi.fn(async (path: string, init?: RequestInit) =>
      (fetchImpl as unknown as (p: string, i?: RequestInit) => unknown)(`${init?.method ?? 'GET'} ${path}`, init),
    ) as unknown as typeof apiFetch

    await store.load(routed)
    expect(store.settings.default_mode).toBe('shadow')

    // 未确认：save 直接拒绝（不发 PUT）
    const ok1 = await store.save({ default_mode: 'real_input' }, routed)
    expect(ok1).toBe(false)
    expect(saved).toBeNull()
    expect(store.error).toContain('二次确认')

    // 确认后放行；armed 一次性（保存后自动解除）
    store.armHighRisk()
    const ok2 = await store.save({ default_mode: 'real_input' }, routed)
    expect(ok2).toBe(true)
    expect(saved).toEqual({ default_mode: 'real_input' })
    expect(store.settings.default_mode).toBe('real_input')
    expect(store.isHighRiskMode).toBe(true)
    expect(store.highRiskArmed).toBe(false)

    // 下一次高风险仍需重新确认
    const ok3 = await store.save({ unattended_schedule: 'enabled' }, routed)
    expect(ok3).toBe(false)
  })

  it('普通补丁无需确认；后端 409（受保护目标硬锁）错误透出', async () => {
    const store = useSettingsStore()
    const fetchImpl = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === '/api/v1/settings' && init?.method === 'PUT') {
        const body = init.body as unknown as Record<string, unknown>
        // 只拒绝高风险补丁（受保护目标硬锁 409）
        if (body.default_mode !== undefined) {
          throw Object.assign(new Error('工作区存在受保护在线目标，禁止把默认模式改为 real_input'), { status: 409 })
        }
        return { default_mode: 'shadow', trace_retention_days: body.trace_retention_days, unattended_schedule: 'disabled' }
      }
      return { default_mode: 'shadow', trace_retention_days: 7, unattended_schedule: 'disabled' }
    }) as unknown as typeof apiFetch

    const ok = await store.save({ trace_retention_days: 30 }, fetchImpl)
    expect(ok).toBe(true)
    expect(store.settings.trace_retention_days).toBe(30)

    store.armHighRisk()
    const denied = await store.save({ default_mode: 'real_input' }, fetchImpl)
    expect(denied).toBe(false)
    expect(store.error).toContain('受保护在线目标')
  })
})

describe('inspectorStore（UI-013 会话检视）', () => {
  it('事件流归约：感知字段表 / 当前状态与进入时刻 / 策略拒绝 / 预算余量', () => {
    const store = useInspectorStore()
    store.bindSession('sess-1', 'dry_run')
    expect(store.mode).toBe('dry_run')

    store.applyEvent(wsEvent(1, 'perception_snapshot', { fields: { health_ratio: 0.9 } }))
    store.applyEvent(wsEvent(2, 'state_transition', { from: 'idle', to: 'scan' }))
    store.applyEvent(wsEvent(3, 'policy_decision', { allowed: false, when: 'runtime > limit', reason: '超时风险' }))
    store.applyEvent(wsEvent(4, 'executed', { budget_remaining_ms: 8.5 }))
    store.applyEvent(wsEvent(5, 'perception_snapshot', { fields: { health_ratio: 0.8, mana: 55 } }))

    expect(store.fields.map((f) => f.name)).toEqual(['health_ratio', 'mana'])
    expect(store.fields[0]).toMatchObject({ value: 0.8, seq: 5 })
    expect(store.currentState).toBe('scan')
    expect(store.stateEnteredSeq).toBe(2)
    expect(store.stateEnteredAt).toBeGreaterThan(0)
    expect(store.lastDenial).toMatchObject({ reason: '超时风险', when: 'runtime > limit', seq: 3 })
    expect(store.budgetRemainingMs).toBe(8.5)
    expect(store.executedCount).toBe(1)
    expect(store.lastSeq).toBe(5)
  })

  it('WS 订阅：连接成功置 connected；resync 提示；事件经 socket 派发归约', () => {
    vi.useFakeTimers()
    try {
      const store = useInspectorStore()
      const sockets: Array<WsLike & { url: string }> = []
      const factory = vi.fn((url: string): WsLike => {
        const socket: WsLike & { url: string } = {
          url,
          close: vi.fn(),
          send: vi.fn(),
          onopen: null,
          onmessage: null,
          onclose: null,
          onerror: null,
        }
        sockets.push(socket)
        return socket
      })

      store.applyEvent(wsEvent(41, 'executed', {}))
      store.start({ socketFactory: factory, reconnectDelayMs: 100 })
      expect(sockets[0].url).toContain('last_seq=41')
      expect(store.wsState).toBe('connecting')

      sockets[0].onopen?.({})
      expect(store.connected).toBe(true)

      sockets[0].onmessage?.({ data: JSON.stringify(wsEvent(42, 'state_transition', { to: 'flee' })) })
      expect(store.currentState).toBe('flee')

      sockets[0].onmessage?.({ data: JSON.stringify({ type: 'resync', seq: 99 }) })
      expect(store.wsState).toBe('resync')
      expect(store.lastError).toContain('重新同步')

      store.stop()
      expect(store.connected).toBe(false)
      expect(store.wsState).toBe('closed')
    } finally {
      vi.useRealTimers()
    }
  })
})
