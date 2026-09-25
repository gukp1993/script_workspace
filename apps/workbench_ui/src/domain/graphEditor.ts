/**
 * 节点图编辑逻辑（EDT-001/002 基础版，纯函数便于单测）。
 *
 * 单一语义原则（M4 通过条件）：
 * - 图的唯一数据源是 MachineForm（与表单/YAML 同源，见 machineYaml.ts）；
 *   结构操作（连线/改属性/删边）都是 MachineForm 的纯变换，随后经
 *   formToYaml/formToMachineDict 回写 —— 图不保存任何独立业务语义；
 * - 布局位置（节点 x/y）是**纯本地视图状态**：只存 localStorage，
 *   永不进入 DSL/YAML（测试断言 formToMachineDict 输出无坐标字段）；
 * - 撤销栈只覆盖结构操作（快照式 undo/redo）；拖拽位置不入栈。
 */

import {
  formToMachineDict,
  type MachineForm,
  type TransitionForm,
} from './machineYaml'
import {
  GRAPH_HEIGHT,
  GRAPH_WIDTH,
  layoutStateGraph,
  reachableStates,
  type GraphMachine,
  type GraphNode,
  type StateGraphLayout,
} from './stateGraph'

// ---------------------------------------------------------------------------
// 布局持久化（本地视图状态，不入 DSL）
// ---------------------------------------------------------------------------

/** 布局位置：状态名 -> 画布坐标 */
export type LayoutMap = Record<string, { x: number; y: number }>

/** 可注入的本地存储接口（localStorage 或测试桩） */
export interface KeyValueStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem?(key: string): void
}

/** localStorage 布局键前缀（项目+状态机隔离） */
export const LAYOUT_KEY_PREFIX = 'vaw.nodegraph.layout'

/** 布局持久化键：vaw.nodegraph.layout.<projectId>.<machineId> */
export function layoutStorageKey(projectId: string, machineId: string): string {
  return `${LAYOUT_KEY_PREFIX}.${projectId}.${machineId}`
}

/** 保存布局到本地存储（序列化失败静默忽略——布局丢失不影响语义） */
export function saveLayout(storage: KeyValueStorage, key: string, layout: LayoutMap): void {
  try {
    storage.setItem(key, JSON.stringify(layout))
  } catch {
    /* 存储不可用（隐私模式/超限）时放弃持久化 */
  }
}

/** 读取布局；缺失/损坏/形状非法返回 null（回退到自动环形布局） */
export function loadLayout(storage: KeyValueStorage, key: string): LayoutMap | null {
  let text: string | null
  try {
    text = storage.getItem(key)
  } catch {
    return null
  }
  if (!text) return null
  try {
    const data: unknown = JSON.parse(text)
    if (data === null || typeof data !== 'object' || Array.isArray(data)) return null
    const out: LayoutMap = {}
    for (const [name, pos] of Object.entries(data as Record<string, unknown>)) {
      const p = pos as { x?: unknown; y?: unknown } | null
      if (
        p !== null && typeof p === 'object' &&
        typeof p.x === 'number' && Number.isFinite(p.x) &&
        typeof p.y === 'number' && Number.isFinite(p.y)
      ) {
        out[name] = { x: p.x, y: p.y }
      }
    }
    return Object.keys(out).length > 0 ? out : null
  } catch {
    return null
  }
}

/** 清除布局（"重置布局"按钮） */
export function clearLayout(storage: KeyValueStorage, key: string): void {
  try {
    storage.removeItem?.(key)
  } catch {
    /* 忽略 */
  }
}

// ---------------------------------------------------------------------------
// 带自定义位置的布局渲染（拖拽后边随节点移动）
// ---------------------------------------------------------------------------

/** 二次贝塞尔曲线（与 stateGraph.layoutStateGraph 同一几何，几何复制不构成第二套语义） */
function bezier(from: { x: number; y: number }, to: { x: number; y: number }): {
  path: string
  labelX: number
  labelY: number
} {
  const mx = (from.x + to.x) / 2
  const my = (from.y + to.y) / 2
  const dx = to.x - from.x
  const dy = to.y - from.y
  const len = Math.max(1, Math.hypot(dx, dy))
  const bend = Math.min(80, len * 0.25)
  const cx = mx + (-dy / len) * bend
  const cy = my + (dx / len) * bend
  return {
    path: `M ${from.x} ${from.y} Q ${cx} ${cy} ${to.x} ${to.y}`,
    labelX: (from.x + 2 * cx + to.x) / 4,
    labelY: (from.y + 2 * cy + to.y) / 4,
  }
}

/** 环形布局 + 本地位置覆盖：节点坐标取 positions，边重新求路径 */
export function layoutWithPositions(machine: GraphMachine, positions: LayoutMap | null): StateGraphLayout {
  const layout = layoutStateGraph(machine)
  if (!positions || Object.keys(positions).length === 0) return layout
  const moved = new Map<string, { x: number; y: number }>()
  const nodes: GraphNode[] = layout.nodes.map((node) => {
    const pos = positions[node.name]
    const next = pos ? { ...node, x: Math.round(pos.x), y: Math.round(pos.y) } : node
    moved.set(node.name, { x: next.x, y: next.y })
    return next
  })
  const point = (name: string): { x: number; y: number } =>
    moved.get(name) ?? { x: GRAPH_WIDTH / 2, y: GRAPH_HEIGHT - 10 }
  const edges = layout.edges.map((edge) => {
    const { path, labelX, labelY } = bezier(point(edge.from), point(edge.to))
    return { ...edge, path, labelX, labelY }
  })
  return { nodes, edges, width: layout.width, height: layout.height }
}

// ---------------------------------------------------------------------------
// 结构操作（MachineForm 纯变换 —— 图 -> 表单 -> YAML 单一语义）
// ---------------------------------------------------------------------------

/** 深拷贝表单（快照/不可变变换的基础；表单是纯 JSON 数据） */
export function cloneForm(form: MachineForm): MachineForm {
  return JSON.parse(JSON.stringify(form)) as MachineForm
}

/** 从 from 状态向 to 状态追加一条迁移（默认 when='true'，priority 追加在末尾） */
export function connectTransition(form: MachineForm, from: string, to: string): MachineForm {
  const next = cloneForm(form)
  const state = next.states.find((s) => s.name === from)
  if (!state) return form
  const transition: TransitionForm = {
    when: 'true',
    to,
    priority: state.transitions.length,
    timeoutSeconds: null,
  }
  state.transitions.push(transition)
  return next
}

/** 编辑指定状态的某条迁移（when/to/priority/timeoutSeconds 局部覆盖） */
export function editTransition(
  form: MachineForm,
  stateName: string,
  index: number,
  patch: Partial<TransitionForm>,
): MachineForm {
  const next = cloneForm(form)
  const state = next.states.find((s) => s.name === stateName)
  const transition = state?.transitions[index]
  if (!transition) return form
  Object.assign(transition, patch)
  return next
}

/** 删除指定状态的某条迁移 */
export function removeTransitionAt(form: MachineForm, stateName: string, index: number): MachineForm {
  const next = cloneForm(form)
  const state = next.states.find((s) => s.name === stateName)
  if (!state || index < 0 || index >= state.transitions.length) return form
  state.transitions.splice(index, 1)
  return next
}

/** 结构是否等价（撤销栈避免推入无变化快照） */
export function formsEqual(a: MachineForm, b: MachineForm): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

// ---------------------------------------------------------------------------
// 撤销栈（快照式，仅结构操作入栈；容量有界防内存膨胀）
// ---------------------------------------------------------------------------

export class GraphUndoStack {
  private past: MachineForm[] = []
  private future: MachineForm[] = []

  constructor(
    private current: MachineForm,
    private readonly limit = 50,
  ) {}

  /** 提交一次结构变更：当前值入 past，清空 redo 分支 */
  commit(next: MachineForm): void {
    if (formsEqual(this.current, next)) return
    this.past.push(cloneForm(this.current))
    if (this.past.length > this.limit) this.past.shift()
    this.current = cloneForm(next)
    this.future = []
  }

  /** 撤销：返回上一个结构；无可撤销返回 null */
  undo(): MachineForm | null {
    const prev = this.past.pop()
    if (!prev) return null
    this.future.push(cloneForm(this.current))
    this.current = prev
    return this.current
  }

  /** 重做：返回下一个结构；无可重做返回 null */
  redo(): MachineForm | null {
    const nextForm = this.future.pop()
    if (!nextForm) return null
    this.past.push(cloneForm(this.current))
    this.current = nextForm
    return this.current
  }

  /** 重置为全新结构（切换状态机时清空历史） */
  reset(form: MachineForm): void {
    this.current = cloneForm(form)
    this.past = []
    this.future = []
  }

  get value(): MachineForm {
    return this.current
  }

  get canUndo(): boolean {
    return this.past.length > 0
  }

  get canRedo(): boolean {
    return this.future.length > 0
  }
}

// ---------------------------------------------------------------------------
// 画布常量与拖拽坐标换算（视图层辅助）
// ---------------------------------------------------------------------------

/** 画布尺寸（与 stateGraph 默认布局一致） */
export const EDITOR_WIDTH = GRAPH_WIDTH
export const EDITOR_HEIGHT = GRAPH_HEIGHT

/** 把 SVG 内的指针事件坐标换算为 viewBox 坐标（拖拽用） */
export function toCanvasCoords(
  svg: SVGSVGElement,
  clientX: number,
  clientY: number,
): { x: number; y: number } {
  const rect = svg.getBoundingClientRect()
  const scaleX = EDITOR_WIDTH / Math.max(1, rect.width)
  const scaleY = EDITOR_HEIGHT / Math.max(1, rect.height)
  return {
    x: Math.max(0, Math.min(EDITOR_WIDTH, (clientX - rect.left) * scaleX)),
    y: Math.max(0, Math.min(EDITOR_HEIGHT, (clientY - rect.top) * scaleY)),
  }
}

/** 可达性提示（复用 stateGraph 的 BFS，供属性面板徽标） */
export function unreachableStates(machine: GraphMachine): string[] {
  const reachable = reachableStates(machine)
  return machine.states.map((s) => s.name).filter((name) => !reachable.has(name))
}

/** 结构 -> 后端领域对象（保存前断言无布局字段泄漏进 DSL） */
export function formToMachinePayload(form: MachineForm): Record<string, unknown> {
  return formToMachineDict(form)
}
