/**
 * 状态图布局与可达性计算（UI-011，纯函数便于单测）。
 *
 * - reachableStates：从 initial 出发沿 transitions[].to BFS，得到可达集合；
 *   不在集合内的状态即"不可达状态"（与后端静态分析 state_unreachable 同义，
 *   前端本地计算，无后端时也可用）；
 * - layoutStateGraph：环形布局——状态均匀分布在圆周上（initial 排首位），
 *   迁移用二次贝塞尔曲线 + when 条件标签；
 * - 布局输出为纯数据（节点坐标/边路径），视图层只做 SVG 渲染。
 */

/** 状态机编辑器/图形共用的最小状态机形状（与 MachineEditor 表单模型对齐） */
export interface GraphTransition {
  when: string
  to: string
  priority?: number
  timeoutSeconds?: number | null
}

export interface GraphState {
  name: string
  terminal?: boolean
  timeoutSeconds?: number | null
  transitions: GraphTransition[]
  entry?: unknown[]
  exit?: unknown[]
}

export interface GraphMachine {
  machineId: string
  initial: string
  states: GraphState[]
}

export interface GraphNode {
  name: string
  x: number
  y: number
  unreachable: boolean
  terminal: boolean
  isInitial: boolean
}

export interface GraphEdge {
  from: string
  to: string
  when: string
  /** 贝塞尔曲线路径（SVG d 属性） */
  path: string
  /** 标签锚点（曲线中点） */
  labelX: number
  labelY: number
}

export interface StateGraphLayout {
  nodes: GraphNode[]
  edges: GraphEdge[]
  width: number
  height: number
}

/** 画布尺寸与环形半径 */
export const GRAPH_WIDTH = 720
export const GRAPH_HEIGHT = 420
const RADIUS_X = 260
const RADIUS_Y = 150
const CENTER_X = GRAPH_WIDTH / 2
const CENTER_Y = GRAPH_HEIGHT / 2

/** 从 initial 沿迁移 BFS 计算可达状态集合（未知目标状态忽略） */
export function reachableStates(machine: GraphMachine): Set<string> {
  const names = new Set(machine.states.map((s) => s.name))
  const initial = machine.initial
  const reachable = new Set<string>()
  if (!names.has(initial)) return reachable
  reachable.add(initial)
  const queue: string[] = [initial]
  while (queue.length > 0) {
    const current = queue.shift() as string
    const state = machine.states.find((s) => s.name === current)
    for (const tr of state?.transitions ?? []) {
      const target = typeof tr.to === 'string' ? tr.to : ''
      if (target && names.has(target) && !reachable.has(target)) {
        reachable.add(target)
        queue.push(target)
      }
    }
  }
  return reachable
}

/** 二次贝塞尔曲线：从 source 指向 target，控制点垂直偏移制造弧度 */
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
  // 控制点在中垂线方向偏移（自环与短线弧度更明显）
  const bend = Math.min(80, len * 0.25)
  const cx = mx + (-dy / len) * bend
  const cy = my + (dx / len) * bend
  return {
    path: `M ${from.x} ${from.y} Q ${cx} ${cy} ${to.x} ${to.y}`,
    labelX: (from.x + 2 * cx + to.x) / 4,
    labelY: (from.y + 2 * cy + to.y) / 4,
  }
}

/** 生成环形布局：节点圆周均布（initial 首位在正上方），边为带标签曲线 */
export function layoutStateGraph(machine: GraphMachine): StateGraphLayout {
  const reachable = reachableStates(machine)
  const ordered = [...machine.states].sort((a, b) => {
    if (a.name === machine.initial) return -1
    if (b.name === machine.initial) return 1
    return a.name.localeCompare(b.name)
  })
  const count = Math.max(1, ordered.length)
  const positions = new Map<string, { x: number; y: number }>()
  const nodes: GraphNode[] = ordered.map((state, i) => {
    const angle = (2 * Math.PI * i) / count - Math.PI / 2
    const x = Math.round(CENTER_X + RADIUS_X * Math.cos(angle))
    const y = Math.round(CENTER_Y + RADIUS_Y * Math.sin(angle))
    positions.set(state.name, { x, y })
    return {
      name: state.name,
      x,
      y,
      unreachable: !reachable.has(state.name),
      terminal: state.terminal === true,
      isInitial: state.name === machine.initial,
    }
  })

  const edges: GraphEdge[] = []
  for (const state of ordered) {
    const from = positions.get(state.name)
    if (!from) continue
    for (const tr of state.transitions) {
      if (typeof tr.to !== 'string' || !tr.to) continue
      const to = positions.get(tr.to) ?? {
        // 目标状态未定义：画到画布底部中央，便于看出悬空迁移
        x: CENTER_X,
        y: GRAPH_HEIGHT - 10,
      }
      const { path, labelX, labelY } = bezier(from, to)
      edges.push({ from: state.name, to: tr.to, when: String(tr.when ?? ''), path, labelX, labelY })
    }
  }
  return { nodes, edges, width: GRAPH_WIDTH, height: GRAPH_HEIGHT }
}
