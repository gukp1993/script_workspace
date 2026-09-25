<script setup lang="ts">
/**
 * 状态机编辑器（UI-009/010 核心版）。
 *
 * 同一数据源（MachineForm）双向同步：
 * - 表单改动 -> 右侧 YAML 只读预览实时重算（formToYaml）；
 * - YAML 编辑区粘贴 -> parseMachineYaml 即时解析：成功则回灌表单；
 *   失败只就地标红显示错误，**绝不覆盖表单**（表单保持原值）；
 * - 状态列表 + 选中状态表单（transitions：when 文本框 + to 下拉 +
 *   priority + timeout；entry/exit 动作 JSON 文本域）；
 * - 保存：新建 POST / 修改 PUT（乐观锁 meta.version）。
 */
import { computed, onMounted, ref } from 'vue'

import { apiFetch } from '../api/client'
import {
  emptyMachineForm,
  formToMachineDict,
  formToYaml,
  normalizeMachine,
  parseMachineYaml,
  type MachineForm,
} from '../domain/machineYaml'
import { useProjectStore } from '../stores/project'

const projects = useProjectStore()

const selectedId = ref('')
const form = ref<MachineForm>(emptyMachineForm())
const currentVersion = ref<number | null>(null)
const selectedStateIndex = ref(0)
/** YAML 双向区 */
const yamlMode = ref<'preview' | 'edit'>('preview')
const yamlDraft = ref('')
const yamlError = ref<string | null>(null)
const saveNotice = ref<string | null>(null)
const saveError = ref<string | null>(null)

const machines = computed(() => projects.objects['machines'] ?? [])

const currentState = computed(() => form.value.states[selectedStateIndex.value] ?? null)

/** 状态名集合（迁移 to 下拉选项） */
const stateNames = computed(() => form.value.states.map((s) => s.name))

/** entry/exit 文本域 <-> 动作数组（解析失败就地标红，不改数据） */
const entryText = computed({
  get: () => JSON.stringify(currentState.value?.entry ?? [], null, 2),
  set: (text: string) => applyActions('entry', text),
})
const exitText = computed({
  get: () => JSON.stringify(currentState.value?.exit ?? [], null, 2),
  set: (text: string) => applyActions('exit', text),
})
const entryError = ref<string | null>(null)
const exitError = ref<string | null>(null)

function applyActions(kind: 'entry' | 'exit', text: string): void {
  const state = currentState.value
  if (!state) return
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (exc) {
    const target = kind === 'entry' ? entryError : exitError
    target.value = `动作 JSON 无法解析：${exc instanceof Error ? (exc as Error).message : String(exc)}`
    return
  }
  if (!Array.isArray(parsed)) {
    const target = kind === 'entry' ? entryError : exitError
    target.value = '动作必须是数组（如 [{"kind":"notify","message":"ok"}]）'
    return
  }
  if (kind === 'entry') entryError.value = null
  else exitError.value = null
  state[kind] = parsed
}

function selectMachine(id: string): void {
  const found = machines.value.find((m) => (m as { machine_id?: string }).machine_id === id)
  selectedId.value = id
  saveNotice.value = null
  saveError.value = null
  yamlError.value = null
  if (found) {
    form.value = normalizeMachine(found as Record<string, unknown>)
    currentVersion.value = Number((found as { meta?: { version?: number } }).meta?.version ?? 1)
  } else {
    form.value = emptyMachineForm()
    currentVersion.value = null
  }
  selectedStateIndex.value = 0
}

function addState(): void {
  const name = `state_${form.value.states.length + 1}`
  form.value.states.push({ name, terminal: false, timeoutSeconds: null, transitions: [], entry: [], exit: [] })
  selectedStateIndex.value = form.value.states.length - 1
}

function removeState(index: number): void {
  if (form.value.states.length <= 1) return
  const removed = form.value.states[index].name
  form.value.states.splice(index, 1)
  // 清理指向被删状态的迁移
  for (const state of form.value.states) {
    state.transitions = state.transitions.filter((tr) => tr.to !== removed)
  }
  selectedStateIndex.value = Math.min(selectedStateIndex.value, form.value.states.length - 1)
}

function addTransition(): void {
  const state = currentState.value
  if (!state) return
  state.transitions.push({ when: 'true', to: stateNames.value[0] ?? state.name, priority: state.transitions.length, timeoutSeconds: null })
}

function removeTransition(index: number): void {
  currentState.value?.transitions.splice(index, 1)
}

/** YAML 预览（表单改动实时反映） */
const yamlPreview = computed(() => formToYaml(form.value))

/** 切到 YAML 编辑：以当前预览为草稿 */
function startEditYaml(): void {
  yamlDraft.value = yamlPreview.value
  yamlError.value = null
  yamlMode.value = 'edit'
}

/** YAML 输入：成功回灌表单；失败就地标红、表单不动 */
function onYamlInput(): void {
  const result = parseMachineYaml(yamlDraft.value)
  if (result.ok) {
    form.value = result.form
    yamlError.value = null
    selectedStateIndex.value = Math.min(selectedStateIndex.value, Math.max(0, form.value.states.length - 1))
  } else {
    yamlError.value = result.error
  }
}

function cancelEditYaml(): void {
  yamlMode.value = 'preview'
  yamlError.value = null
}

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
  const body = formToMachineDict(form.value)
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
    saveNotice.value = `已保存：${form.value.machineId}`
    await refresh()
    selectMachine(selectedId.value)
  } catch (exc) {
    saveError.value = exc instanceof Error ? exc.message : String(exc)
  }
}

onMounted(async () => {
  await projects.loadProjects()
  if (projects.currentProjectId) {
    await refresh()
  }
})
</script>

<template>
  <section class="machine-editor" aria-label="状态机编辑器">
    <header class="bar">
      <h2>状态机编辑器</h2>
      <button @click="selectMachine('')">新建状态机</button>
    </header>

    <p v-if="!projects.currentProjectId" class="empty">请先在左侧选择一个项目</p>

    <div v-else class="layout">
      <aside class="list">
        <h3>状态机清单</h3>
        <p v-if="machines.length === 0" class="empty">暂无状态机 — 点「新建状态机」开始</p>
        <ul>
          <li v-for="m in machines" :key="String(m.machine_id)">
            <button class="item" :class="{ active: selectedId === m.machine_id }" @click="selectMachine(String(m.machine_id))">
              {{ m.machine_id }}
            </button>
          </li>
        </ul>
      </aside>

      <div class="form-col">
        <h3>{{ currentVersion !== null ? `编辑：${selectedId}` : '新建状态机' }}</h3>

        <div class="row">
          <label>
            状态机 ID
            <input v-model="form.machineId" :disabled="currentVersion !== null" />
          </label>
          <label>
            初始状态
            <select v-model="form.initial">
              <option v-for="name in stateNames" :key="name" :value="name">{{ name }}</option>
            </select>
          </label>
        </div>

        <div class="states">
          <h4>状态列表</h4>
          <ul>
            <li v-for="(state, i) in form.states" :key="state.name">
              <button
                class="item"
                :class="{ active: selectedStateIndex === i }"
                @click="selectedStateIndex = i"
              >
                {{ state.name }}
                <span v-if="state.name === form.initial" class="tag">初始</span>
                <span v-if="state.terminal" class="tag">终态</span>
              </button>
              <button class="mini" :disabled="form.states.length <= 1" :aria-label="`删除状态 ${state.name}`" @click="removeState(i)">✕</button>
            </li>
          </ul>
          <button @click="addState">添加状态</button>
        </div>

        <template v-if="currentState">
          <h4>状态「{{ currentState.name }}」</h4>
          <label class="inline">
            <input v-model="currentState.terminal" type="checkbox" />
            终态（terminal）
          </label>
          <label>
            状态级超时 timeout_seconds（秒，可空）
            <input v-model.number="currentState.timeoutSeconds" type="number" step="1" min="0" placeholder="不设置" />
          </label>

          <h4>迁移（transitions）</h4>
          <p v-if="currentState.transitions.length === 0" class="empty">无迁移（非终态且无超时可能卡死，静态分析会报 state_no_exit）</p>
          <div v-for="(tr, i) in currentState.transitions" :key="i" class="transition-row">
            <input v-model="tr.when" placeholder="when 表达式（如 health_ratio < 0.3）" aria-label="when 表达式" />
            <select v-model="tr.to" aria-label="目标状态">
              <option v-for="name in stateNames" :key="name" :value="name">{{ name }}</option>
            </select>
            <input v-model.number="tr.priority" type="number" step="1" aria-label="优先级" title="priority（数字越大越优先）" />
            <input v-model.number="tr.timeoutSeconds" type="number" step="1" min="0" aria-label="超时秒数" title="timeout（秒，可空）" placeholder="超时" />
            <button class="mini" :aria-label="`删除迁移 ${i + 1}`" @click="removeTransition(i)">✕</button>
          </div>
          <button @click="addTransition">添加迁移</button>

          <div class="json-row">
            <label>
              entry 动作（JSON 数组）
              <textarea v-model="entryText" rows="3" spellcheck="false" />
            </label>
            <p v-if="entryError" class="issue" role="alert">⚠ {{ entryError }}</p>
          </div>
          <div class="json-row">
            <label>
              exit 动作（JSON 数组）
              <textarea v-model="exitText" rows="3" spellcheck="false" />
            </label>
            <p v-if="exitError" class="issue" role="alert">⚠ {{ exitError }}</p>
          </div>

          <div class="actions">
            <button :disabled="!form.machineId || stateNames.length === 0" @click="save">保存到项目</button>
            <span v-if="saveNotice" role="status">{{ saveNotice }}</span>
            <span v-if="saveError" class="issue" role="alert">⚠ {{ saveError }}</span>
          </div>
        </template>
      </div>

      <div class="yaml-col">
        <div class="yaml-bar">
          <h4>YAML</h4>
          <template v-if="yamlMode === 'preview'">
            <span class="muted">只读预览（随表单实时更新）</span>
            <button @click="startEditYaml">编辑 YAML</button>
          </template>
          <template v-else>
            <span class="muted">编辑：粘贴解析成功才应用表单</span>
            <button @click="cancelEditYaml">完成</button>
          </template>
        </div>
        <pre v-if="yamlMode === 'preview'" class="yaml" data-testid="yaml-preview">{{ yamlPreview }}</pre>
        <template v-else>
          <textarea v-model="yamlDraft" class="yaml-edit" rows="22" spellcheck="false" aria-label="YAML 编辑区" @input="onYamlInput" />
          <p v-if="yamlError" class="issue" role="alert" data-testid="yaml-error">
            ⚠ YAML 解析失败（表单未被修改）：{{ yamlError }}
          </p>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.machine-editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
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
.layout {
  display: grid;
  grid-template-columns: 180px 1fr 1fr;
  gap: 16px;
  align-items: start;
}
.list ul,
.states ul {
  list-style: none;
  margin: 0 0 6px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.states li {
  display: flex;
  gap: 4px;
  align-items: center;
}
.item {
  flex: 1;
  text-align: left;
  display: flex;
  gap: 6px;
  align-items: center;
}
.item.active {
  outline: 2px solid #3b82f6;
}
.mini {
  padding: 2px 8px;
}
.tag {
  color: #64748b;
  font-size: 11px;
  border: 1px solid #334155;
  border-radius: 4px;
  padding: 0 4px;
}
.form-col {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: #94a3b8;
}
label.inline {
  flex-direction: row;
  align-items: center;
  gap: 6px;
}
input,
select,
textarea {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 6px;
  color: inherit;
  padding: 6px 8px;
}
textarea {
  font-family: Consolas, monospace;
  font-size: 12px;
}
.transition-row {
  display: grid;
  grid-template-columns: 2fr 1fr 70px 80px auto;
  gap: 6px;
  align-items: center;
}
.json-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.yaml-col {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.yaml-bar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.muted {
  color: #64748b;
  font-size: 12px;
}
.yaml {
  background: #020617;
  border: 1px solid #1e293b;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  overflow: auto;
  margin: 0;
  max-height: 480px;
}
.yaml-edit {
  width: 100%;
  font-family: Consolas, monospace;
  font-size: 12px;
}
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
