import { describe, expect, it } from 'vitest'

import {
  calibrationToDict,
  detectorsReferencing,
  emptyMachineForm,
  formToMachineDict,
  formToYaml,
  normalizeMachine,
  parseMachineYaml,
  shortSha,
  thresholdHint,
  validateCalibrationForm,
  validateDetectorForm,
  type MachineForm,
} from '../src/domain/machineYaml'
import { layoutStateGraph, reachableStates, type GraphMachine } from '../src/domain/stateGraph'

/** 两状态示例状态机（带 priority/timeout 扩展字段） */
function sampleForm(): MachineForm {
  return {
    machineId: 'main',
    initial: 'idle',
    states: [
      {
        name: 'idle',
        terminal: false,
        timeoutSeconds: 30,
        entry: [{ kind: 'notify', message: 'ready' }],
        exit: [],
        transitions: [{ when: 'health_ratio < 0.3', to: 'flee', priority: 5, timeoutSeconds: 2 }],
      },
      { name: 'flee', terminal: true, timeoutSeconds: null, entry: [], exit: [], transitions: [] },
    ],
  }
}

describe('machineYaml：表单 <-> YAML 双向同步（UI-009/010）', () => {
  it('formToYaml -> parseMachineYaml 往返一致（含 priority/timeout 扩展字段）', () => {
    const form = sampleForm()
    const yaml = formToYaml(form)
    expect(yaml).toContain('machine_id: main')
    expect(yaml).toContain('priority: 5')
    const result = parseMachineYaml(yaml)
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.form.machineId).toBe('main')
    expect(result.form.initial).toBe('idle')
    expect(result.form.states[0].transitions[0]).toMatchObject({
      when: 'health_ratio < 0.3',
      to: 'flee',
      priority: 5,
      timeoutSeconds: 2,
    })
    expect(result.form.states[0].entry).toEqual([{ kind: 'notify', message: 'ready' }])
    expect(result.form.states[0].timeoutSeconds).toBe(30)
    expect(result.form.states[1].terminal).toBe(true)
  })

  it('YAML 语法错误 -> ok:false + 错误信息（表单不被覆盖的前提）', () => {
    const result = parseMachineYaml('machine_id: [broken')
    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.error.length).toBeGreaterThan(0)
  })

  it('YAML 结构错误（initial 不在 states / 缺 machine_id）逐条拒绝', () => {
    const missingId = parseMachineYaml('initial: idle\nstates:\n  idle: {}')
    expect(missingId.ok).toBe(false)
    const badInitial = parseMachineYaml('machine_id: m\ninitial: ghost\nstates:\n  idle: {}')
    expect(badInitial.ok).toBe(false)
    if (!badInitial.ok) expect(badInitial.error).toContain('ghost')
    const emptyStates = parseMachineYaml('machine_id: m\ninitial: idle\nstates: {}')
    expect(emptyStates.ok).toBe(false)
    const badTransition = parseMachineYaml('machine_id: m\ninitial: idle\nstates:\n  idle:\n    transitions:\n      - to: x')
    expect(badTransition.ok).toBe(false)
  })

  it('parse 成功才回灌表单：失败时原表单引用保持不变', () => {
    const form = sampleForm()
    const before = formToMachineDict(form)
    const broken = parseMachineYaml('states: {')
    expect(broken.ok).toBe(false)
    // 失败分支不产生新表单，调用方（MachineEditor）只在 ok 分支赋值
    expect(formToMachineDict(form)).toEqual(before)
  })

  it('normalizeMachine 容错补默认值；formToMachineDict 省略空字段', () => {
    const normalized = normalizeMachine({ machine_id: 'm', initial: 'a', states: { a: { terminal: true } } })
    expect(normalized.states[0]).toMatchObject({
      name: 'a',
      terminal: true,
      timeoutSeconds: null,
      transitions: [],
      entry: [],
      exit: [],
    })
    const dict = formToMachineDict(normalized) as { states: Record<string, Record<string, unknown>> }
    expect(dict.states.a).toEqual({ terminal: true }) // 空字段不序列化
    expect(formToMachineDict(emptyMachineForm()).schema_version).toBe(1)
  })
})

describe('detector 校验（UI-007）', () => {
  it('合法表单零问题；阈值越界/稳定帧非法/ROI 越界就地报错', () => {
    const base = {
      detectorId: 'health-bar',
      type: 'color_bar_ratio',
      roi: [0.1, 0.1, 0.2, 0.2] as [number, number, number, number],
      threshold: 0.5,
      stableFrames: 2,
      fieldName: 'health_ratio',
      template: '',
    }
    expect(validateDetectorForm(base)).toEqual([])

    expect(validateDetectorForm({ ...base, threshold: 1.5 }).some((i) => i.field === 'threshold')).toBe(true)
    expect(validateDetectorForm({ ...base, stableFrames: 0 }).some((i) => i.field === 'stableFrames')).toBe(true)
    expect(validateDetectorForm({ ...base, roi: [0.9, 0, 0.5, 0.1] }).some((i) => i.message.includes('x + w'))).toBe(true)
    expect(validateDetectorForm({ ...base, detectorId: '9bad' }).some((i) => i.field === 'detectorId')).toBe(true)
  })

  it('template_match 必须指定模板；thresholdHint 输出离线校验文案', () => {
    const form = {
      detectorId: 'btn',
      type: 'template_match',
      roi: [0, 0, 0.3, 0.3] as [number, number, number, number],
      threshold: 0.8,
      stableFrames: 1,
      fieldName: 'btn_seen',
      template: '',
    }
    expect(validateDetectorForm(form).some((i) => i.field === 'template')).toBe(true)
    expect(validateDetectorForm({ ...form, template: 'assets/templates/btn.png' })).toEqual([])
    const hint = thresholdHint({ ...form, template: 't.png' })
    expect(hint).toContain('离线校验')
    expect(hint).toContain('0.8')
    expect(hint).toContain('btn_seen')
  })
})

describe('标定向导（UI-008 简化）', () => {
  it('calibrationToDict 生成 parse_calibration 兼容结构', () => {
    const dict = calibrationToDict({
      calibrationId: '1920x1080-100',
      resolutionWidth: 1920,
      resolutionHeight: 1080,
      dpiPercent: 100,
      uiScale: 1.25,
      anchors: [{ name: 'btn', x: 0.5, y: 0.25 }],
    })
    expect(dict).toMatchObject({
      schema_version: 1,
      calibration_id: '1920x1080-100',
      resolution: '1920x1080',
      dpi_percent: 100,
      ui_scale: 1.25,
    })
    expect(dict.anchors).toEqual({ btn: [0.5, 0.25] })
  })

  it('校验：分辨率/DPI 越界、锚点坐标越界、重复锚点名就地报错', () => {
    const base = {
      calibrationId: 'c1',
      resolutionWidth: 1920,
      resolutionHeight: 1080,
      dpiPercent: 100,
      uiScale: 1,
      anchors: [{ name: 'btn', x: 0.5, y: 0.5 }],
    }
    expect(validateCalibrationForm(base)).toEqual([])
    const issues = validateCalibrationForm({
      ...base,
      resolutionWidth: 100,
      dpiPercent: 40,
      uiScale: 9,
      anchors: [
        { name: 'a', x: 1.5, y: 0 },
        { name: 'a', x: 0.2, y: 0.2 },
        { name: '', x: 0.1, y: 0.1 },
      ],
    })
    expect(issues.map((i) => i.message).join('\n')).toContain('宽度')
    expect(issues.map((i) => i.message).join('\n')).toContain('DPI')
    expect(issues.map((i) => i.message).join('\n')).toContain('UI 缩放')
    expect(issues.map((i) => i.message).join('\n')).toContain('重复')
  })
})

describe('资产删除保护（UI-006）', () => {
  const asset = { asset_id: 'btn', path: 'assets/templates/btn.png', sha256: 'abcdef1234567890', version: 1, kind: 'template' }

  it('detectorsReferencing 按 template === path 计数；shortSha 取前 8 位', () => {
    const detectors = [
      { detector_id: 'd1', template: 'assets/templates/btn.png' },
      { detector_id: 'd2', template: 'assets/templates/btn.png' },
      { detector_id: 'd3', template: 'assets/templates/other.png' },
    ]
    expect(detectorsReferencing(asset, detectors)).toEqual(['d1', 'd2'])
    expect(detectorsReferencing(asset, [])).toEqual([])
    expect(shortSha(asset)).toBe('abcdef12')
  })
})

describe('stateGraph：可达性与布局（UI-011）', () => {
  const machine: GraphMachine = {
    machineId: 'main',
    initial: 'idle',
    states: [
      { name: 'idle', transitions: [{ when: 'go', to: 'run' }] },
      { name: 'run', transitions: [{ when: 'stop', to: 'idle' }] },
      { name: 'ghost', transitions: [] }, // 无人指向 -> 不可达
    ],
  }

  it('reachableStates 从 initial BFS；不可达状态被识别', () => {
    const reachable = reachableStates(machine)
    expect(reachable.has('idle')).toBe(true)
    expect(reachable.has('run')).toBe(true)
    expect(reachable.has('ghost')).toBe(false)
  })

  it('layoutStateGraph：环形布局、边带 when 标签、不可达标记在节点上', () => {
    const layout = layoutStateGraph(machine)
    expect(layout.nodes).toHaveLength(3)
    expect(layout.nodes.find((n) => n.name === 'ghost')?.unreachable).toBe(true)
    expect(layout.nodes.find((n) => n.name === 'idle')?.isInitial).toBe(true)
    // 坐标在画布内
    for (const node of layout.nodes) {
      expect(node.x).toBeGreaterThanOrEqual(0)
      expect(node.x).toBeLessThanOrEqual(layout.width)
      expect(node.y).toBeGreaterThanOrEqual(0)
      expect(node.y).toBeLessThanOrEqual(layout.height)
    }
    // 两条迁移边 + when 标签锚点
    expect(layout.edges.map((e) => `${e.from}->${e.to}`).sort()).toEqual(['idle->run', 'run->idle'])
    expect(layout.edges.every((e) => e.when.length > 0 && e.path.startsWith('M '))).toBe(true)
    // 悬空迁移画向底部并保留原始 to
    const dangling = layoutStateGraph({
      machineId: 'm',
      initial: 'a',
      states: [{ name: 'a', transitions: [{ when: 'x', to: 'missing' }] }],
    })
    expect(dangling.edges[0].to).toBe('missing')
  })
})
