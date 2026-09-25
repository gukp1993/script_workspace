import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AssetLibrary from '../src/views/AssetLibrary.vue'
import MachineEditor from '../src/views/MachineEditor.vue'
import SettingsView from '../src/views/SettingsView.vue'
import TestCenter from '../src/views/TestCenter.vue'
import TimelineView from '../src/views/TimelineView.vue'
import { useProjectStore } from '../src/stores/project'

const apiFetchMock = vi.fn()

vi.mock('../src/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/client')>()
  return { ...original, apiFetch: (...args: unknown[]) => apiFetchMock(...args) }
})

const MACHINE = {
  schema_version: 1,
  machine_id: 'main',
  initial: 'idle',
  meta: { version: 3 },
  states: {
    idle: {
      timeout_seconds: 30,
      transitions: [{ when: 'health_ratio < 0.3', to: 'flee', priority: 5 }],
    },
    flee: { terminal: true },
  },
}

function route(path: string, method: string | undefined): string {
  return `${method ?? 'GET'} ${path}`
}

beforeEach(() => {
  setActivePinia(createPinia())
  apiFetchMock.mockReset()
})

/** 预置当前项目（绕过 loadProjects 的网络依赖） */
async function presetProject() {
  const projects = useProjectStore()
  projects.projects = [{ project_id: 'demo', name: 'Demo', description: '' }]
  projects.currentProjectId = 'demo'
  return projects
}

describe('AssetLibrary（UI-006 删除保护）', () => {
  it('被引用资产的删除按钮禁用并显示引用明细；未引用资产可点', async () => {
    await presetProject()
    apiFetchMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === '/api/v1/projects/demo/assets') {
        return {
          assets: [
            { asset_id: 'btn', path: 'assets/templates/btn.png', sha256: 'abcdef1234567890', version: 1, kind: 'template' },
            { asset_id: 'spare', path: 'assets/templates/spare.yaml', sha256: '1234ffffffffffff', version: 2, kind: 'template' },
          ],
        }
      }
      if (path === '/api/v1/projects/demo/detectors') {
        return { objects: [{ detector_id: 'd1', template: 'assets/templates/btn.png' }] }
      }
      throw new Error(`unexpected: ${path}`)
    })

    const wrapper = mount(AssetLibrary)
    await vi.waitFor(() => expect(apiFetchMock.mock.calls.length).toBeGreaterThanOrEqual(2))
    await wrapper.vm.$nextTick()

    const text = wrapper.text()
    expect(text).toContain('btn')
    expect(text).toContain('sha abcdef12') // sha 前 8 位
    expect(text).toContain('被 1 个检测器引用')
    expect(text).toContain('未被检测器引用')

    const buttons = wrapper.findAll('button').filter((b) => b.text() === '删除')
    expect(buttons).toHaveLength(2)
    expect(buttons[0].attributes('disabled')).toBeDefined() // 被引用 -> 禁用
    expect(buttons[1].attributes('disabled')).toBeUndefined()

    // 未引用资产点击删除：控制面无删除端点 -> 404 降级说明（不假成功）
    apiFetchMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (route(path, init?.method as string) === 'DELETE /api/v1/projects/demo/assets/spare') {
        throw Object.assign(new Error('请求失败（HTTP 404）'), { status: 404 })
      }
      if (path === '/api/v1/projects/demo/assets') return { assets: [] }
      throw new Error(`unexpected: ${path}`)
    })
    await buttons[1].trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('暂未提供资产删除端点'))
  })
})

describe('MachineEditor（UI-009/010 表单 <-> YAML 同步）', () => {
  async function mountEditor() {
    await presetProject()
    apiFetchMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === '/api/v1/projects/demo/machines') {
        return { objects: [MACHINE] }
      }
      throw new Error(`unexpected: ${path}`)
    })
    const wrapper = mount(MachineEditor)
    // 注意：YAML 预览初始就含 "machine_id: main"，不能用它判断清单加载完成；
    // 直接等清单按钮出现
    await vi.waitFor(() => expect(wrapper.find('.list .item').exists()).toBe(true))
    await wrapper.find('.list .item').trigger('click')
    await wrapper.vm.$nextTick()
    return wrapper
  }

  it('表单改动 -> YAML 只读预览实时更新', async () => {
    const wrapper = await mountEditor()
    const preview = () => wrapper.find('[data-testid="yaml-preview"]').text()
    expect(preview()).toContain('health_ratio < 0.3')
    expect(preview()).toContain('priority: 5')
    expect(preview()).toContain('timeout_seconds: 30')

    const whenInput = wrapper.find('input[aria-label="when 表达式"]')
    await whenInput.setValue('mana_ratio < 0.1')
    expect(preview()).toContain('mana_ratio < 0.1')
    expect(preview()).not.toContain('health_ratio')
  })

  it('YAML 粘贴：解析成功回灌表单；解析失败就地标红且表单不被覆盖', async () => {
    const wrapper = await mountEditor()
    const whenInput = () => wrapper.find('input[aria-label="when 表达式"]').element as HTMLInputElement

    // 进入 YAML 编辑模式
    await wrapper.findAll('button').find((b) => b.text() === '编辑 YAML')!.trigger('click')
    await wrapper.vm.$nextTick()

    const editArea = wrapper.find('textarea[aria-label="YAML 编辑区"]')
    // 1) 非法 YAML：标红报错，表单保持原值
    await editArea.setValue('states: {broken')
    await wrapper.vm.$nextTick()
    const err = wrapper.find('[data-testid="yaml-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('YAML 解析失败')
    expect(err.text()).toContain('表单未被修改')
    expect(whenInput().value).toBe('health_ratio < 0.3')

    // 2) 合法 YAML：回灌表单（when 变化、错误消失）
    await editArea.setValue(
      [
        'schema_version: 1',
        'machine_id: main',
        'initial: idle',
        'states:',
        '  idle:',
        '    transitions:',
        '      - when: mana_full',
        '        to: flee',
      ].join('\n'),
    )
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="yaml-error"]').exists()).toBe(false)
    expect(whenInput().value).toBe('mana_full')
  })

  it('entry 动作 JSON 文本域：非法 JSON 就地报错且不改数据', async () => {
    const wrapper = await mountEditor()
    const textarea = wrapper.find('textarea[aria-label]').exists()
      ? wrapper.findAll('textarea')[0]
      : wrapper.findAll('textarea')[0]
    await textarea.setValue('not-json')
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('动作 JSON 无法解析')
  })
})

describe('TimelineView（UI-014 过滤与点击选择）', () => {
  it('事件点点击 -> 详情面板显示 payload JSON；无 frame_ref 时给出说明', async () => {
    await presetProject()
    const events = [
      { seq: 0, ts_monotonic: 0, correlation_id: '', session_id: 's1', type: 'perception_snapshot', payload: { fields: { hp: 0.5 } }, prev_hash: '0', hash: 'h' },
      { seq: 1, ts_monotonic: 4, correlation_id: '', session_id: 's1', type: 'state_transition', payload: { from: 'idle', to: 'flee' }, prev_hash: 'h', hash: 'h' },
    ]
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path === '/api/v1/projects/demo/traces') {
        return { traces: [{ name: 'trace-a.jsonl', size_bytes: 9, modified_at: '', event_count: 2 }], count: 1 }
      }
      if (typeof path === 'string' && path.startsWith('/api/v1/projects/demo/traces/trace-a.jsonl/events')) {
        return { events, returned: 2, matched: 2, total: 2, truncated_tail: true, error_count: 0 }
      }
      throw new Error(`unexpected: ${path}`)
    })

    const wrapper = mount(TimelineView)
    await vi.waitFor(() => expect(wrapper.find('[data-testid="lanes"]').exists()).toBe(true))
    expect(wrapper.text()).toContain('尾部损坏已截断')

    const dots = wrapper.findAll('.dot')
    expect(dots.length).toBe(2)
    await dots[1].trigger('click')
    await wrapper.vm.$nextTick()

    const detail = wrapper.find('[data-testid="event-detail"]')
    expect(detail.exists()).toBe(true)
    expect(detail.text()).toContain('state_transition')
    expect(detail.text()).toContain('"to": "flee"')
    expect(detail.text()).toContain('该事件未关联帧')

    // 关闭详情
    await detail.find('button').trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="event-detail"]').exists()).toBe(false)
  })
})

describe('SettingsView（UI-016 高风险二次确认）', () => {
  it('改默认模式为 real_input：红色警示 + 勾选前保存禁用，勾选后保存成功', async () => {
    await presetProject()
    let putBody: Record<string, unknown> | null = null
    apiFetchMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (route(path, init?.method as string) === 'GET /api/v1/settings') {
        return { default_mode: 'shadow', trace_retention_days: 7, unattended_schedule: 'disabled' }
      }
      if (route(path, init?.method as string) === 'PUT /api/v1/settings') {
        putBody = init?.body as unknown as Record<string, unknown>
        return { default_mode: 'real_input', trace_retention_days: 7, unattended_schedule: 'disabled' }
      }
      throw new Error(`unexpected: ${path}`)
    })

    const wrapper = mount(SettingsView)
    await vi.waitFor(() => expect(wrapper.text()).toContain('默认执行模式'))

    // 选择 real_input（第 4 个模式按钮）
    const modeButtons = wrapper.findAll('[role="radio"]')
    expect(modeButtons).toHaveLength(4)
    await modeButtons[3].trigger('click')
    await wrapper.vm.$nextTick()

    // 红色警示（含图标与文字，不只靠颜色）
    const alert = wrapper.find('[role="alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('高风险变更')
    expect(alert.text()).toContain('真实输入')

    // 未勾选确认 -> 保存禁用；勾选 -> 可保存
    const save = () => wrapper.findAll('button').find((b) => b.text() === '保存设置')!
    expect(save().attributes('disabled')).toBeDefined()
    await wrapper.find('input[type="checkbox"]').setValue(true)
    expect(save().attributes('disabled')).toBeUndefined()
    await save().trigger('click')
    await vi.waitFor(() => expect(putBody).not.toBeNull())
    expect(putBody).toEqual({ default_mode: 'real_input' })
    expect(wrapper.text()).toContain('设置已保存')
  })

  it('普通修改（保留期）无需二次确认即可保存', async () => {
    await presetProject()
    let putBody: Record<string, unknown> | null = null
    apiFetchMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (route(path, init?.method as string) === 'GET /api/v1/settings') {
        return { default_mode: 'shadow', trace_retention_days: 7, unattended_schedule: 'disabled' }
      }
      if (route(path, init?.method as string) === 'PUT /api/v1/settings') {
        putBody = init?.body as unknown as Record<string, unknown>
        return { default_mode: 'shadow', trace_retention_days: 30, unattended_schedule: 'disabled' }
      }
      throw new Error(`unexpected: ${path}`)
    })

    const wrapper = mount(SettingsView)
    await vi.waitFor(() => expect(wrapper.text()).toContain('轨迹保留期'))
    await wrapper.find('input[type="number"]').setValue('30')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false) // 无高风险警示
    const save = wrapper.findAll('button').find((b) => b.text() === '保存设置')!
    expect(save.attributes('disabled')).toBeUndefined()
    await save.trigger('click')
    await vi.waitFor(() => expect(putBody).toEqual({ trace_retention_days: 30 }))
    // 固定说明可见
    expect(wrapper.text()).toContain('受保护在线目标')
  })
})

describe('TestCenter（UI-015）', () => {
  it('空态说明与报告全文渲染', async () => {
    await presetProject()
    // 空目录 -> 空态
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path === '/api/v1/projects/demo/replay-reports') return { reports: [], count: 0 }
      throw new Error(`unexpected: ${path}`)
    })
    const empty = mount(TestCenter)
    await vi.waitFor(() => expect(empty.text()).toContain('暂无回放/差异报告'))
    empty.unmount()

    // 有报告 -> 点击查看全文
    apiFetchMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === '/api/v1/projects/demo/replay-reports') {
        return {
          reports: [{ name: 'diff.md', rel_path: 'diff.md', size_bytes: 20, modified_at: '', preview: '# 差异报告' }],
        }
      }
      if (path === '/api/v1/projects/demo/replay-reports/diff.md/content') {
        return { content: '# 差异报告\n\n全部一致', name: 'diff.md' }
      }
      throw new Error(`unexpected: ${path}`)
    })
    const wrapper = mount(TestCenter)
    await vi.waitFor(() => expect(wrapper.text()).toContain('diff.md'))
    await wrapper.find('.list .item').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('[data-testid="report-content"]').exists()).toBe(true))
    expect(wrapper.find('[data-testid="report-content"]').text()).toContain('全部一致')
  })
})
