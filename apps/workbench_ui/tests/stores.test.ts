import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiFetch, fetchPreviewShot } from '../src/api/client'
import { usePreviewStore } from '../src/stores/preview'
import { objectLabel, useProjectStore } from '../src/stores/project'
import { useSessionStore } from '../src/stores/session'

/** 构造按路径响应的 apiFetch 替身 */
function routerFetch(routes: Record<string, (init: RequestInit) => unknown>) {
  return vi.fn(async (path: string, init?: RequestInit) => {
    const handler = routes[path]
    if (!handler) throw new Error(`unexpected path: ${path}`)
    return handler(init ?? {})
  }) as unknown as typeof apiFetch
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('projectStore（UI-002 对象树）', () => {
  it('加载项目并自动选中第一个；loadObjects 拉取对象列表', async () => {
    const store = useProjectStore()
    const fetchImpl = routerFetch({
      '/api/v1/projects': () => ({
        projects: [
          { project_id: 'demo', name: 'Demo', description: '' },
          { project_id: 'other', name: 'Other', description: '' },
        ],
        count: 2,
      }),
      '/api/v1/projects/demo/targets': () => ({
        project_id: 'demo',
        kind: 'targets',
        objects: [{ target_id: 'arena-lab', executable: 'ArenaLab.exe' }],
        count: 1,
      }),
    })

    await store.loadProjects(fetchImpl)
    expect(store.projects).toHaveLength(2)
    expect(store.currentProjectId).toBe('demo')
    expect(store.error).toBeNull()

    await store.loadObjects('targets', fetchImpl)
    expect(store.objects['targets']).toHaveLength(1)

    // 切换项目后对象缓存被清空
    store.selectProject('other')
    expect(store.objects['targets']).toBeUndefined()
  })

  it('创建项目后刷新列表并选中；失败写入 error', async () => {
    const store = useProjectStore()
    let created = false
    const fetchImpl = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === '/api/v1/projects' && init?.method === 'POST') {
        created = true
        return {}
      }
      if (path === '/api/v1/projects') {
        return created
          ? { projects: [{ project_id: 'new-proj', name: '新项目', description: '' }], count: 1 }
          : { projects: [], count: 0 }
      }
      throw new Error(`unexpected: ${path}`)
    }) as unknown as typeof apiFetch

    await store.createProject('new-proj', '新项目', fetchImpl)
    expect(created).toBe(true)
    expect(store.currentProjectId).toBe('new-proj')
    expect(store.error).toBeNull()
  })

  it('objectLabel 按 kind 取对应 id 字段', () => {
    expect(objectLabel('targets', { target_id: 't1' })).toBe('t1')
    expect(objectLabel('policies', { policy_id: 'p1' })).toBe('p1')
    expect(objectLabel('machines', {})).not.toBe('')
  })
})

describe('sessionStore（UI-012 控制条 / UI-003 闸门）', () => {
  const SESSION = {
    session_id: 'sess-1',
    project_id: 'demo',
    target_id: 'arena-lab',
    mode: 'real_input',
    state: 'created',
    gate: null,
  }

  it('创建 real_input 会话后等待人工确认，confirm 后可启动', async () => {
    const store = useSessionStore()
    let confirmed = false
    const fetchImpl = routerFetch({
      '/api/v1/sessions': () => SESSION,
      '/api/v1/sessions/sess-1/confirm': () => {
        confirmed = true // 人工闸门通过后才允许 start 成功
        return { ...SESSION, gate: { operator: 'op' } }
      },
      '/api/v1/sessions/sess-1/start': (init) => {
        void init
        expect(confirmed).toBe(true) // 未确认前不会被调用
        return { ...SESSION, gate: { operator: 'op' }, state: 'running' }
      },
    })

    await store.create('demo', 'arena-lab', 'real_input', fetchImpl)
    expect(store.current?.state).toBe('created')
    expect(store.awaitingConfirm).toBe(true)
    expect(store.canStart).toBe(true) // 后端允许 start 请求发出（未确认会被 409）

    await store.confirm('op', fetchImpl)
    expect(store.awaitingConfirm).toBe(false)

    await store.start(fetchImpl)
    expect(store.current?.state).toBe('running')
    expect(store.canPause).toBe(true)
  })

  it('生命周期：running 可暂停，paused 可恢复为 resumed', async () => {
    const store = useSessionStore()
    const base = { ...SESSION, mode: 'shadow', gate: null, state: 'running' }
    let state = 'running'
    const fetchImpl = routerFetch({
      '/api/v1/sessions': () => base,
      '/api/v1/sessions/sess-1/pause': () => {
        state = 'paused'
        return { ...base, state }
      },
      '/api/v1/sessions/sess-1/resume': () => {
        state = 'resumed'
        return { ...base, state }
      },
    })
    await store.create('demo', 'arena-lab', 'shadow', fetchImpl)
    expect(store.canPause).toBe(true)

    await store.pause(fetchImpl)
    expect(store.canResume).toBe(true)
    expect(store.canPause).toBe(false)

    await store.resume(fetchImpl)
    expect(store.current?.state).toBe('resumed')
  })

  it('停止永远可用：进行中显示清理文案，完成保留"清理完成"', async () => {
    const store = useSessionStore()
    let releaseStop!: (record: object) => void
    const gate = new Promise<object>((resolve) => {
      releaseStop = resolve
    })
    const fetchImpl = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === '/api/v1/sessions') return SESSION
      if (path === '/api/v1/sessions/sess-1/stop') {
        await gate
        return { ...SESSION, state: 'stopped' }
      }
      throw new Error(`unexpected: ${path}`)
    }) as unknown as typeof apiFetch

    await store.create('demo', 'arena-lab', 'shadow', fetchImpl)
    expect(store.stopAlwaysEnabled).toBe(true)

    const stopping = store.stop(fetchImpl)
    // 请求在途：清理文案已出现
    expect(store.cleaning).toContain('正在清理')
    releaseStop({ ...SESSION, state: 'stopped' })
    await stopping
    expect(store.current?.state).toBe('stopped')
    expect(store.cleaning).toBe('清理完成：会话已安全停止')
  })

  it('停止失败时清理文案复位并记录错误（不静默）', async () => {
    const store = useSessionStore()
    const fetchImpl = vi.fn(async (path: string) => {
      if (path === '/api/v1/sessions') return SESSION
      throw Object.assign(new Error('服务已停止'), { status: 503 })
    }) as unknown as typeof apiFetch

    await store.create('demo', 'arena-lab', 'shadow', fetchImpl)
    await store.stop(fetchImpl)
    expect(store.cleaning).toBeNull()
    expect(store.error).toContain('服务已停止')
  })
})

describe('previewStore（UI-004 预览）', () => {
  it('抓帧成功：状态 streaming、fps 按 EMA 更新、frameUrl 刷新', async () => {
    // jsdom 没有 createObjectURL：打桩
    const createdUrls: string[] = []
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      writable: true,
      value: (blob: Blob) => {
        void blob
        const url = `blob:fake-${createdUrls.length}`
        createdUrls.push(url)
        return url
      },
    })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, writable: true, value: () => {} })

    const store = usePreviewStore()
    const blob = new Blob(['jpeg-bytes'], { type: 'image/jpeg' })
    const fetchImpl = vi.fn(async () => blob) as unknown as typeof fetchPreviewShot
    let clock = 0
    const now = () => clock

    expect(await store.captureOnce(fetchImpl, now)).toBe(true)
    expect(store.status).toBe('streaming')
    expect(store.fps).toBe(0) // 首帧无间隔，不计算

    clock = 250 // 250ms -> 4 fps
    expect(await store.captureOnce(fetchImpl, now)).toBe(true)
    expect(store.fps).toBeCloseTo(4, 5)
    expect(store.frameUrl).toBe('blob:fake-1')
    expect(store.frameCount).toBe(2)
    expect(store.error).toBeNull()
  })

  it('抓帧失败：进入 error 态并记录信息，startPolling 幂等且 stopPolling 收敛', async () => {
    const store = usePreviewStore()
    const fetchImpl = vi.fn(async () => {
      throw Object.assign(new Error('屏幕采集不可用'), { status: 503 })
    }) as unknown as typeof fetchPreviewShot

    expect(await store.captureOnce(fetchImpl)).toBe(false)
    expect(store.status).toBe('error')
    expect(store.error).toContain('屏幕采集不可用')

    expect(store.startPolling(fetchImpl)).toBe(true)
    expect(store.startPolling(fetchImpl)).toBe(false) // 幂等
    // 等一拍让 in-flight capture 落地（error 保持）
    await vi.waitFor(() => expect(store.frameCount > 0 || store.status === 'error').toBe(true))
    store.stopPolling()
    expect(store.status).toBe('error') // 错误态不被静默覆盖为 idle
    store.clearError()
    expect(store.status).toBe('idle')
  })
})
