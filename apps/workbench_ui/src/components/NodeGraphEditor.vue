<script setup lang="ts">
/**
 * 节点图编辑器（EDT-001/002 基础版，M4）。
 *
 * ADR：不引入 Rete.js——节点图只是 DSL 的一种编辑视图（M4 通过条件），
 * 基于 M3 的 stateGraph 只读布局扩展为可编辑 SVG：
 * - 结构操作（点选源状态 -> 加迁移 -> 点选目标；属性面板编辑 when/to/
 *   priority；删除迁移）都提交到 domain/graphEditor 的纯变换，与
 *   MachineEditor 表单共用 machineYaml 同一数据源 -> formToYaml 实时
 *   预览 / formToMachinePayload 保存，图不产生第二套语义；
 * - 节点拖拽只更新本地布局（localStorage），不入 DSL、不入撤销栈；
 * - 撤销/重做仅覆盖结构操作；
 * - 保存经 control_plane API 回写 machine store -> YAML（乐观锁版本）。
 */
import { computed, onMounted, ref, watch } from 'vue'

import { apiFetch } from '../api/client'
import {
  clearLayout,
  cloneForm,
  connectTransition,
  EDITOR_HEIGHT,
  EDITOR_WIDTH,
  editTransition,
  formToMachinePayload,
  GraphUndoStack,
  layoutStorageKey,
  layoutWithPositions,
  loadLayout,
  removeTransitionAt,
  saveLayout,
  toCanvasCoords,
  unreachableStates,
  type KeyValueStorage,
  type LayoutMap,
} from '../domain/graphEditor'
import {
  emptyMachineForm,
  formToGraphMachine,
  formToYaml,
  normalizeMachine,
  type MachineForm,
} from '../domain/machineYaml'
import { useProjectStore } from '../stores/project'

const projects = useProjectStore()

/** 本地存储（可注入替换；默认 window.localStorage） */
const storage: KeyValueStorage = {
  getItem: (k) => window.localStorage.getItem(k),
  setItem: (k, v) => window.localStorage.setItem(k, v),
  removeItem: (k) => window.localStorage.removeItem(k),
}

/** 撤销栈（非响应式）+ 响应式镜像表单 */
const stack = new GraphUndoStack(emptyMachineForm())
const form = ref<MachineForm>(emptyMachineForm())
const canUndo = ref(false)
const canRedo = ref(false)

const selectedId = ref('')
const currentVersion = ref<number | null>(null)
const saveNotice = ref<string | null>(null)
const saveError = ref<string | null>(null)

/** 本地布局位置（仅视图状态，不入 DSL） */
const positions = ref<LayoutMap>({})
const selectedState = ref<string | null>(null)
/** 连线流程：点选源状态 -> 「加迁移」 -> 点选目标状态 */
const linkSource = ref<string | null>(null)
/** 拖拽中的节点与偏移（仅本地布局） */
const drag = ref<{ name: string; dx: number; dy: number; moved: boolean } | null>(null)
const svgEl = ref<SVGSVGElement | null>(null)

const machines = computed(() => projects.objects['machines'] ?? [])
const graphMachine = computed(() => formToGraphMachine(form.value))
const layout = computed(() => layoutWithPositions(graphMachine.value, positions.value))
const yamlPreview = computed(() => formToYaml(form.value))
const stateNames = computed(() => form.value.states.map((s) => s.name))
const unreachable = computed(() => unreachableStates(graphMachine.value))
const selectedTransitions = computed(() => {
  const state = form.value.states.find((s) => s.name === selectedState.value)
  return state?.transitions ?? []
})

function layoutKey(): string {
  return layoutStorageKey(projects.currentProjectId ?? 'default', selectedId.value || 'main')
}

function loadLocalPositions(): void {
  positions.value = loadLayout(storage, layoutKey()) ?? {}
}

function syncForm(): void {
  form.value = cloneForm(stack.value)
  canUndo.value = stack.canUndo
  canRedo.value = stack.canRedo
}

function selectMachine(id: string): void {
  const found = machines.value.find((m) => (m as { machine_id?: string }).machine_id === id)
  selectedId.value = id
  saveNotice.value = null
  saveError.value = null
  selectedState.value = null
  linkSource.value = null
  if (found) {
    stack.reset(normalizeMachine(found as Record<string, unknown>))
    currentVersion.value = Number((found as { meta?: { version?: number } }).meta?.version ?? 1)
  } else {
    stack.reset(emptyMachineForm())
    currentVersion.value = null
  }
  syncForm()
  loadLocalPositions()
}

/** 提交一次结构变更（入撤销栈） */
function commit(next: MachineForm): void {
  stack.commit(next)
  syncForm()
}

// -- 连线流程 ---------------------------------------------------------------

function onNodeClick(name: string, event: MouseEvent): void {
  event.stopPropagation()
  if (drag.value?.moved) return // 拖拽结束后的 click 不当作选择
  if (linkSource.value !== null && linkSource.value !== name) {
    const source = linkSource.value
    linkSource.value = null
    commit(connectTransition(form.value, source, name))
    selectedState.value = source
    return
  }
  selectedState.value = selectedState.value === name ? null : name
}

function beginLink(): void {
  if (!selectedState.value) return
  linkSource.value = selectedState.value
}

function cancelLink(): void {
  linkSource.value = null
}

// -- 属性面板编辑（迁移 when/to/priority） ------------------------------------

function onEditTransition(
  index: number,
  patch: { when?: string; to?: string; priority?: number; timeoutSeconds?: number | null },
): void {
  if (!selectedState.value) return
  commit(editTransition(form.value, selectedState.value, index, patch))
}

function onRemoveTransition(index: number): void {
  if (!selectedState.value) return
  commit(removeTransitionAt(form.value, selectedState.value, index))
}

// -- 撤销/重做（仅结构操作） ---------------------------------------------------

function onUndo(): void {
  if (stack.undo() !== null) syncForm()
}

function onRedo(): void {
  if (stack.redo() !== null) syncForm()
}

// -- 本地布局拖拽（不入 DSL、不入撤销栈） --------------------------------------

function onNodePointerDown(name: string, event: PointerEvent): void {
  const svg = svgEl.value
  if (!svg) return
  const pos = layout.value.nodes.find((n) => n.name === name)
  if (!pos) return
  const point = toCanvasCoords(svg, event.clientX, event.clientY)
  drag.value = { name, dx: point.x - pos.x, dy: point.y - pos.y, moved: false }
  ;(event.target as Element).setPointerCapture?.(event.pointerId)
}

function onCanvasPointerMove(event: PointerEvent): void {
  const svg = svgEl.value
  const state = drag.value
  if (!svg || !state) return
  const point = toCanvasCoords(svg, event.clientX, event.clientY)
  const x = point.x - state.dx
  const y = point.y - state.dy
  if (Math.abs(x - (positions.value[state.name]?.x ?? x)) > 1 || Math.abs(y - (positions.value[state.name]?.y ?? y)) > 1) {
    state.moved = true
  }
  positions.value = { ...positions.value, [state.name]: { x, y } }
}

function onCanvasPointerUp(): void {
  if (!drag.value) return
  const moved = drag.value.moved
  drag.value = null
  if (moved) saveLayout(storage, layoutKey(), positions.value)
}

function resetLayout(): void {
  clearLayout(storage, layoutKey())
  positions.value = {}
}

// -- 保存（与 MachineEditor 同一 API 约定，回写 machine store -> YAML） --------

async function refresh(): Promise<void> {
  if (!projects.currentProjectId) return
  await projects.loadObjects('machines')
  if (selectedId.value && !machines.value.some((m) => (m as { machine_id?: string }).machine_id === selectedId.value)) {
    selectMachine('')
  }
}

async function save(): Promise<void> {
  const pid = projects.currentProjectId
  if (!pid) return
  saveError.value = null
  saveNotice.value = null
  const body = formToMachinePayload(form.value)
  try {
    if (currentVersion.value !== null) {
      await apiFetch(`/api/v1/projects/${pid}/machines/${form.value.machineId}`, {
        method: 'PUT',
        body: { ...body, meta: { version: currentVersion.value } },
      })
    } else {
      await apiFetch(`/api/v1/projects/${pid}/machines`, { method: 'POST', body })
      selectedId.value = form.value.machineId
    }
    saveNotice.value = `已保存：${form.value.machineId}（节点图与 YAML 同源）`
    await refresh()
    selectMachine(selectedId.value)
  } catch (exc) {
    saveError.value = exc instanceof Error ? exc.message : String(exc)
  }
}

onMounted(async () => {
  await projects.loadProjects()
  if (projects.currentProjectId) {
    await projects.loadObjects('machines')
    if (selectedId.value === '' && machines.value.length > 0) {
      selectMachine(String((machines.value[0] as { machine_id?: string }).machine_id))
    }
  }
})

// 项目切换后重新选择首个状态机
watch(
  () => projects.currentProjectId,
  async (pid, prev) => {
    if (pid && pid !== prev) {
      selectedId.value = ''
      await projects.loadObjects('machines')
      if (machines.value.length > 0) {
        selectMachine(String((machines.value[0] as { machine_id?: string }).machine_id))
      } else {
        selectMachine('')
      }
    }
  },
)
</script>

<template>
  <section class="node-graph" aria-label="节点图编辑器">
    <header class="bar">
      <h2>节点图编辑器</h2>
      <select v-model="selectedId" aria-label="选择状态机" @change="selectMachine(selectedId)">
        <option v-for="m in machines" :key="String(m.machine_id)" :value="String(m.machine_id)">
          {{ m.machine_id }}
        </option>
      </select>
      <button :disabled="!canUndo" data-testid="undo" @click="onUndo">撤销</button>
      <button :disabled="!canRedo" data-testid="redo" @click="onRedo">重做</button>
      <button :disabled="!selectedState || linkSource !== null" data-testid="add-transition" @click="beginLink">
        加迁移
      </button>
      <button v-if="linkSource" data-testid="cancel-link" @click="cancelLink">取消连线</button>
      <button @click="resetLayout">重置布局</button>
      <button :disabled="!projects.currentProjectId" data-testid="save" @click="save">保存到项目</button>
    </header>

    <p v-if="!projects.currentProjectId" class="empty">请先在左侧选择一个项目</p>
    <p v-else class="hint">
      点选源状态 →「加迁移」→ 点选目标状态完成连线；拖拽节点仅调整本地布局（不写入 DSL）。
    </p>

    <div class="layout">
      <div class="canvas-col">
        <p v-if="linkSource" class="linking" role="status">
          连线中：从「{{ linkSource }}」出发，点选目标状态
        </p>
        <svg
          ref="svgEl"
          data-testid="canvas"
          :viewBox="`0 0 ${EDITOR_WIDTH} ${EDITOR_HEIGHT}`"
          role="application"
          aria-label="状态机可编辑图形"
          @pointermove="onCanvasPointerMove"
          @pointerup="onCanvasPointerUp"
          @pointerleave="onCanvasPointerUp"
        >
          <defs>
            <marker id="edge-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
            </marker>
          </defs>

          <g v-for="(edge, i) in layout.edges" :key="`e${i}`">
            <path :d="edge.path" fill="none" stroke="#94a3b8" stroke-width="1.4" marker-end="url(#edge-arrow)" />
            <text :x="edge.labelX" :y="edge.labelY - 4" text-anchor="middle" class="edge-label">
              {{ edge.when || '(无条件)' }}
            </text>
          </g>

          <g
            v-for="node in layout.nodes"
            :key="node.name"
            class="node"
            :class="{
              selected: node.name === selectedState,
              linking: node.name === linkSource,
            }"
            :data-testid="`node-${node.name}`"
            @click="onNodeClick(node.name, $event)"
            @pointerdown="onNodePointerDown(node.name, $event)"
          >
            <rect
              :x="node.x - 52"
              :y="node.y - 22"
              width="104"
              height="44"
              rx="10"
              :stroke="node.unreachable ? '#ef4444' : node.name === linkSource ? '#38bdf8' : '#64748b'"
              :stroke-width="node.name === selectedState || node.name === linkSource || node.unreachable ? 2.5 : 1.4"
              :fill="node.isInitial ? '#1d4ed855' : '#111c31'"
            />
            <text :x="node.x" :y="node.y + 5" text-anchor="middle" class="node-label">{{ node.name }}</text>
            <text v-if="node.isInitial" :x="node.x" :y="node.y - 28" text-anchor="middle" class="node-tag">初始</text>
            <text v-else-if="node.terminal" :x="node.x" :y="node.y - 28" text-anchor="middle" class="node-tag">终态</text>
          </g>
        </svg>
      </div>

      <aside class="panel" aria-label="属性面板">
        <h3>属性面板</h3>
        <p v-if="!selectedState" class="empty">点选一个状态节点以编辑其迁移（when / to / priority）。</p>
        <template v-else>
          <h4>状态「{{ selectedState }}」的迁移</h4>
          <p v-if="unreachable.includes(selectedState)" class="warn" role="alert">⚠ 该状态从初始不可达</p>
          <p v-if="selectedTransitions.length === 0" class="empty">无迁移 — 用「加迁移」连线或表单编辑器添加。</p>
          <div v-for="(tr, i) in selectedTransitions" :key="i" class="transition-row" :data-testid="`tr-${i}`">
            <input
              v-model="tr.when"
              aria-label="when 表达式"
              placeholder="when（如 ready.present）"
              @change="onEditTransition(i, { when: String(tr.when) })"
            />
            <select v-model="tr.to" aria-label="目标状态" @change="onEditTransition(i, { to: String(tr.to) })">
              <option v-for="name in stateNames" :key="name" :value="name">{{ name }}</option>
            </select>
            <input
              v-model.number="tr.priority"
              type="number"
              step="1"
              aria-label="优先级"
              title="priority（数字越大越优先）"
              @change="onEditTransition(i, { priority: Number(tr.priority) })"
            />
            <button class="mini" :aria-label="`删除迁移 ${i + 1}`" @click="onRemoveTransition(i)">✕</button>
          </div>
        </template>

        <h4>YAML（与图同源）</h4>
        <pre class="yaml" data-testid="yaml-preview">{{ yamlPreview }}</pre>
        <div class="actions">
          <span v-if="saveNotice" role="status">{{ saveNotice }}</span>
          <span v-if="saveError" class="issue" role="alert">⚠ {{ saveError }}</span>
        </div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.node-graph {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
h2 {
  margin: 0;
  font-size: 18px;
}
h3,
h4 {
  margin: 6px 0;
  font-size: 13px;
  color: #94a3b8;
}
select,
input {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 6px;
  color: inherit;
  padding: 6px 8px;
}
.hint {
  margin: 0;
  color: #64748b;
  font-size: 12px;
}
.linking {
  margin: 0 0 6px;
  color: #38bdf8;
  font-size: 13px;
}
.layout {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
  gap: 16px;
  align-items: start;
}
.canvas-col svg {
  width: 100%;
  height: auto;
  background: #020617;
  border: 1px solid #1e293b;
  border-radius: 8px;
  touch-action: none;
  user-select: none;
}
.node {
  cursor: grab;
}
.node.selected rect {
  filter: brightness(1.35);
}
.edge-label {
  fill: #cbd5e1;
  font-size: 11px;
  paint-order: stroke;
  stroke: #020617;
  stroke-width: 3px;
}
.node-label {
  fill: #e2e8f0;
  font-size: 13px;
  font-weight: 600;
  pointer-events: none;
}
.node-tag {
  fill: #93c5fd;
  font-size: 10px;
  pointer-events: none;
}
.panel {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.transition-row {
  display: grid;
  grid-template-columns: 2fr 1fr 64px auto;
  gap: 6px;
  margin-bottom: 6px;
}
.mini {
  padding: 2px 8px;
}
.yaml {
  background: #020617;
  border: 1px solid #1e293b;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  overflow: auto;
  margin: 0;
  max-height: 320px;
}
.actions {
  display: flex;
  gap: 10px;
}
.warn,
.issue {
  margin: 0;
  color: #f87171;
  font-size: 13px;
}
.empty {
  color: #64748b;
  font-size: 12px;
}
</style>
