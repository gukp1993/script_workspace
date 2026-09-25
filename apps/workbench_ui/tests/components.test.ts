import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ControlBar from '../src/views/ControlBar.vue'
import TargetSelect from '../src/views/TargetSelect.vue'
import { useSessionStore } from '../src/stores/session'

const apiFetchMock = vi.fn()

vi.mock('../src/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/client')>()
  return { ...original, apiFetch: (...args: unknown[]) => apiFetchMock(...args) }
})

beforeEach(() => {
  setActivePinia(createPinia())
  apiFetchMock.mockReset()
})

describe('ControlBar（UI-012）', () => {
  it('无会话时停止按钮存在且禁用占位；有会话时停止永远可点', async () => {
    const wrapper = mount(ControlBar)
    expect(wrapper.text()).toContain('无运行会话')
    let stop = wrapper.findAll('button').find((b) => b.text() === '停止')
    expect(stop).toBeDefined()

    const store = useSessionStore()
    store.applySession({
      session_id: 'sess-9',
      project_id: 'demo',
      target_id: 'arena-lab',
      mode: 'shadow',
      state: 'running',
      gate: null,
    })
    await wrapper.vm.$nextTick()
    stop = wrapper.findAll('button').find((b) => b.text() === '停止')
    expect(stop?.attributes('disabled')).toBeUndefined() // 停止永远可点
    expect(wrapper.text()).toContain('影子') // 模式徽标可见（四色区分）
    // 启动不可用（已在运行），暂停可用
    const start = wrapper.findAll('button').find((b) => b.text() === '启动')
    expect(start?.attributes('disabled')).toBeDefined()
    const pause = wrapper.findAll('button').find((b) => b.text() === '暂停')
    expect(pause?.attributes('disabled')).toBeUndefined()
  })

  it('点击停止调用 store.stop 并展示清理文案', async () => {
    const store = useSessionStore()
    store.applySession({
      session_id: 'sess-9',
      project_id: 'demo',
      target_id: 'arena-lab',
      mode: 'dry_run',
      state: 'running',
      gate: null,
    })
    const stopSpy = vi.spyOn(store, 'stop').mockResolvedValue()
    const wrapper = mount(ControlBar)
    const stop = wrapper.findAll('button').find((b) => b.text() === '停止')
    await stop?.trigger('click')
    expect(stopSpy).toHaveBeenCalledOnce()
    // 模拟清理完成后的文案展示
    store.cleaning = '清理完成：会话已安全停止'
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('清理完成')
  })
})

describe('TargetSelect（UI-003）', () => {
  it('渲染四色模式选项；real_input 显示人工确认入口；窗口端点缺失时空态降级', async () => {
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path === '/api/v1/projects') {
        return { projects: [{ project_id: 'demo', name: 'Demo', description: '' }], count: 1 }
      }
      if (path === '/api/v1/projects/demo/targets') {
        return {
          project_id: 'demo',
          kind: 'targets',
          objects: [
            { target_id: 'arena-lab', executable: 'ArenaLab.exe', title_regex: '^ArenaLab', protected_online: false },
          ],
          count: 1,
        }
      }
      if (path === '/api/v1/windows') {
        throw Object.assign(new Error('请求失败（HTTP 404）'), { status: 404 })
      }
      throw new Error(`unexpected path: ${path}`)
    })

    const wrapper = mount(TargetSelect)
    await vi.waitFor(() => expect(apiFetchMock.mock.calls.length).toBeGreaterThanOrEqual(3))
    await wrapper.vm.$nextTick()

    // 四种模式徽标 + 四色圆点（不只靠文字）
    for (const label of ['观察', '影子', '演练', '真实输入']) {
      expect(wrapper.text()).toContain(label)
    }
    const dots = wrapper.findAll('.dot')
    const dotColors = new Set(dots.map((d) => d.attributes('style')).filter((s) => s?.includes('background')))
    expect(dotColors.size).toBeGreaterThanOrEqual(4)

    // 目标列表渲染
    expect(wrapper.text()).toContain('arena-lab')
    // 窗口端点 404 -> 明确降级文案（不空白、不报错崩页面）
    expect(wrapper.text()).toContain('窗口枚举端点未接入')

    // 默认模式 shadow：创建会话按钮可点；会话创建后（real_input 场景）出现确认按钮
    const session = useSessionStore()
    session.applySession({
      session_id: 'sess-1',
      project_id: 'demo',
      target_id: 'arena-lab',
      mode: 'real_input',
      state: 'created',
      gate: null,
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('人工确认放行')
  })

  it('RealInput 人工确认为两步制：首击仅二次提示，再击才放行，可取消', async () => {
    const wrapper = mount(TargetSelect)
    const session = useSessionStore()
    session.applySession({
      session_id: 'sess-2',
      project_id: 'demo',
      target_id: 'arena-lab',
      mode: 'real_input',
      state: 'created',
      gate: null,
    })
    await wrapper.vm.$nextTick()
    const confirmSpy = vi.spyOn(session, 'confirm').mockResolvedValue()

    // 第一击：只进入二次提示，不真正调用 confirm
    const first = wrapper.findAll('button').find((b) => b.text() === '人工确认放行 RealInput')
    await first?.trigger('click')
    await wrapper.vm.$nextTick()
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('二次确认')
    expect(wrapper.text()).toContain('真实键鼠输入')

    // 取消：回到初始单步按钮态
    const cancel = wrapper.findAll('button').find((b) => b.text() === '取消')
    await cancel?.trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('人工确认放行 RealInput')
    expect(confirmSpy).not.toHaveBeenCalled()

    // 再次两步：第二击才真正放行
    const again = wrapper.findAll('button').find((b) => b.text() === '人工确认放行 RealInput')
    await again?.trigger('click')
    await wrapper.vm.$nextTick()
    const armed = wrapper.findAll('button').find((b) => b.text() === '确认注入（二次确认）')
    await armed?.trigger('click')
    expect(confirmSpy).toHaveBeenCalledOnce()
  })
})
