/**
 * 节点图编辑逻辑单测（EDT-001/002，M4）。
 *
 * 覆盖：布局持久化键与本地存储回路、连线/编辑/删除的 YAML 回写（单一语义）、
 * 布局不入 DSL、撤销/重做、带位置的布局渲染、画布坐标换算、可达性提示。
 */
import { describe, expect, it } from 'vitest'

import {
  cloneForm,
  connectTransition,
  editTransition,
  formsEqual,
  GraphUndoStack,
  layoutStorageKey,
  layoutWithPositions,
  LAYOUT_KEY_PREFIX,
  loadLayout,
  removeTransitionAt,
  saveLayout,
  toCanvasCoords,
  unreachableStates,
  type KeyValueStorage,
  type LayoutMap,
} from '../src/domain/graphEditor'
import { formToGraphMachine, formToMachineDict, formToYaml, normalizeMachine, parseMachineYaml } from '../src/domain/machineYaml'

/** 内存存储桩（同 localStorage 接口） */
function fakeStorage(initial: Record<string, string> = {}): KeyValueStorage & { store: Map<string, string> } {
  const store = new Map(Object.entries(initial))
  return {
    store,
    getItem: (k) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k, v) => void store.set(k, v),
    removeItem: (k) => void store.delete(k),
  }
}

function sampleFormText(): string {
  return `
machine_id: main
initial: idle
states:
  idle:
    transitions:
      - when: "ready.present"
        to: running
  running:
    timeout_seconds: 10
    transitions:
      - when: "done.present"
        to: stopped
  stopped:
    terminal: true
`
}

const sampleForm = () => {
  const parsed = parseMachineYaml(sampleFormText())
  if (!parsed.ok) throw new Error(parsed.error)
  return parsed.form
}

describe('graphEditor 布局持久化（本地视图状态）', () => {
  it('布局键格式为 vaw.nodegraph.layout.<project>.<machine>', () => {
    expect(LAYOUT_KEY_PREFIX).toBe('vaw.nodegraph.layout')
    expect(layoutStorageKey('demo', 'main')).toBe('vaw.nodegraph.layout.demo.main')
  })

  it('saveLayout/loadLayout 往返一致；损坏 JSON 与非法形状返回 null', () => {
    const storage = fakeStorage()
    const key = layoutStorageKey('demo', 'main')
    const layout: LayoutMap = { idle: { x: 10.4, y: 20 }, running: { x: 300, y: 120 } }
    saveLayout(storage, key, layout)
    expect(loadLayout(storage, key)).toEqual(layout)

    storage.store.set(key, '{not json')
    expect(loadLayout(storage, key)).toBeNull()
    storage.store.set(key, JSON.stringify({ idle: { x: 'a', y: 1 } }))
    expect(loadLayout(storage, key)).toBeNull()
    expect(loadLayout(storage, 'missing-key')).toBeNull()
  })
})

describe('graphEditor 结构操作与 YAML 同源（EDT-002）', () => {
  it('连线回写：connectTransition 后 YAML 出现新迁移，再解析可还原', () => {
    const form = sampleForm()
    const next = connectTransition(form, 'running', 'idle')
    const yaml = formToYaml(next)
    expect(yaml).toContain('to: idle')
    // 回读校验：YAML 能还原出同一条迁移（单一语义、双向一致）
    const reparsed = parseMachineYaml(yaml)
    expect(reparsed.ok).toBe(true)
    if (reparsed.ok) {
      const running = reparsed.form.states.find((s) => s.name === 'running')
      expect(running?.transitions.some((t) => t.to === 'idle' && t.when === 'true')).toBe(true)
    }
    // 源状态不存在时保持原状
    expect(connectTransition(form, 'no-such-state', 'idle')).toBe(form)
  })

  it('属性编辑回写：editTransition 修改 when/to/priority 后 YAML 反映', () => {
    const form = sampleForm()
    const next = editTransition(form, 'idle', 0, { when: 'ready.value > 0.5', priority: 3 })
    const yaml = formToYaml(next)
    expect(yaml).toContain('ready.value > 0.5')
    expect(yaml).toContain('priority: 3')
    const dict = formToMachineDict(next)
    const idle = (dict.states as Record<string, { transitions: Array<Record<string, unknown>> }>).idle
    expect(idle.transitions[0]).toMatchObject({ when: 'ready.value > 0.5', priority: 3, to: 'running' })
    // 越界索引不改数据
    expect(editTransition(form, 'idle', 9, { when: 'x' })).toBe(form)
  })

  it('删除迁移回写：removeTransitionAt 后 YAML 不再包含该边', () => {
    const form = sampleForm()
    const next = removeTransitionAt(form, 'idle', 0)
    const yaml = formToYaml(next)
    expect(yaml).not.toContain('ready.present')
    expect(cloneForm(next)).toEqual(next)
    expect(formsEqual(form, cloneForm(form))).toBe(true)
  })

  it('布局位置永不进入 DSL：拖拽坐标只存本地，结构载荷无坐标字段', () => {
    const form = sampleForm()
    const positions: LayoutMap = { idle: { x: 500, y: 80 }, running: { x: 120, y: 300 } }
    const positioned = layoutWithPositions(formToGraphMachine(form), positions)
    // 节点坐标被覆盖
    expect(positioned.nodes.find((n) => n.name === 'idle')).toMatchObject({ x: 500, y: 80 })
    // 但结构载荷保持纯净（无任何坐标/布局字段）
    const dict = formToMachineDict(form)
    expect(JSON.stringify(dict)).not.toContain('500')
    expect(JSON.stringify(dict)).not.toContain('"x"')
    for (const state of Object.values(dict.states as Record<string, object>)) {
      expect(Object.keys(state)).not.toContain('x')
      expect(Object.keys(state)).not.toContain('y')
    }
  })
})

describe('graphEditor 撤销/重做（仅结构操作）', () => {
  it('commit/undo/redo 往返；无变化提交与空栈边界', () => {
    const stack = new GraphUndoStack(sampleForm())
    expect(stack.canUndo).toBe(false)
    expect(stack.undo()).toBeNull()

    const edited = editTransition(stack.value, 'idle', 0, { priority: 7 })
    stack.commit(edited)
    expect(stack.canUndo).toBe(true)
    expect(formsEqual(stack.value, edited)).toBe(true)

    // 无变化的提交不入栈
    stack.commit(cloneForm(edited))
    expect(stack.canUndo).toBe(true)

    const restored = stack.undo() // 回到初始
    expect(formsEqual(restored as NonNullable<typeof restored>, sampleForm())).toBe(true)
    expect(stack.canUndo).toBe(false)
    expect(stack.canRedo).toBe(true)

    const redone = stack.redo()
    expect(redone?.states.find((s) => s.name === 'idle')?.transitions[0]?.priority).toBe(7)
    expect(stack.canRedo).toBe(false)
    expect(stack.redo()).toBeNull()

    stack.reset(sampleForm())
    expect(stack.canUndo).toBe(false)
    expect(stack.canRedo).toBe(false)
  })

  it('撤销栈容量有界（limit）', () => {
    let form = sampleForm()
    const stack = new GraphUndoStack(form, 3)
    for (let i = 0; i < 6; i++) {
      form = editTransition(form, 'idle', 0, { priority: i })
      stack.commit(form)
    }
    let steps = 0
    while (stack.canUndo) {
      stack.undo()
      steps++
    }
    expect(steps).toBe(3)
  })
})

describe('graphEditor 视图辅助', () => {
  it('layoutWithPositions：未知状态的位置被忽略，边路径随节点移动', () => {
    const machine = formToGraphMachine(sampleForm())
    const base = layoutWithPositions(machine, null)
    const moved = layoutWithPositions(machine, {
      idle: { x: 111, y: 222 },
      ghost: { x: 1, y: 1 }, // 不存在的状态：忽略
    })
    expect(moved.nodes.find((n) => n.name === 'idle')).toMatchObject({ x: 111, y: 222 })
    expect(moved.nodes.find((n) => n.name === 'ghost')).toBeUndefined()
    const edge = moved.edges.find((e) => e.from === 'idle' && e.to === 'running')
    const baseEdge = base.edges.find((e) => e.from === 'idle' && e.to === 'running')
    expect(edge?.path).not.toBe(baseEdge?.path) // 边随节点重绘
  })

  it('toCanvasCoords：按 viewBox 比例换算并夹取到画布内', () => {
    const svg = {
      getBoundingClientRect: () => ({ left: 10, top: 20, width: 360, height: 210 }),
    } as unknown as SVGSVGElement
    // 画布 720x420，SVG 显示 360x210 → 2 倍缩放
    expect(toCanvasCoords(svg, 190, 220)).toEqual({ x: 360, y: 400 })
    // 越界夹取
    const clamped = toCanvasCoords(svg, -50, 1000)
    expect(clamped.x).toBe(0)
    expect(clamped.y).toBe(420)
  })

  it('unreachableStates：与 stateGraph BFS 同语义', () => {
    const machine = formToGraphMachine(sampleForm())
    expect(unreachableStates(machine)).toEqual([])
    const broken = normalizeMachine({
      machine_id: 'broken',
      initial: 'a',
      states: { a: { transitions: [] }, orphan: { transitions: [] } },
    })
    expect(unreachableStates(formToGraphMachine(broken))).toEqual(['orphan'])
  })
})
