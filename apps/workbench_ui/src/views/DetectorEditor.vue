<script setup lang="ts">
/**
 * 检测器编辑器（UI-007）。
 *
 * - 左侧检测器清单（来自项目 detectors 领域对象），右侧表单：
 *   type / threshold / roi / stable_frames / field_name / template；
 * - 参数校验错误就地显示（纯函数 validateDetectorForm）；
 * - 无实时管线时的阈值效果以"离线校验"文案呈现（thresholdHint）；
 *   /preview/shot 抓帧入口保留（M4 接入在线阈值可视化）。
 * - 保存：新建 POST，修改 PUT（乐观锁 meta.version）。
 */
import { computed, onMounted, ref } from 'vue'

import { apiFetch } from '../api/client'
import {
  DETECTOR_TYPES,
  normalizeDetector,
  thresholdHint,
  validateDetectorForm,
  type DetectorForm,
} from '../domain/machineYaml'
import { useProjectStore } from '../stores/project'
import { usePreviewStore } from '../stores/preview'

const projects = useProjectStore()
const preview = usePreviewStore()

const selectedId = ref('')
const form = ref<DetectorForm | null>(null)
const currentVersion = ref<number | null>(null)
const saveNotice = ref<string | null>(null)
const saveError = ref<string | null>(null)
const loading = ref(false)

const detectors = computed(() => projects.objects['detectors'] ?? [])

const issues = computed(() => (form.value ? validateDetectorForm(form.value) : []))

const canSave = computed(() => form.value !== null && issues.value.length === 0)

function blankForm(): DetectorForm {
  return {
    detectorId: '',
    type: 'color_bar_ratio',
    roi: [0, 0, 0.2, 0.1],
    threshold: 0.5,
    stableFrames: 1,
    fieldName: '',
    template: '',
  }
}

function selectDetector(id: string): void {
  const found = detectors.value.find((d) => (d as { detector_id?: string }).detector_id === id)
  selectedId.value = id
  saveNotice.value = null
  saveError.value = null
  if (found) {
    form.value = normalizeDetector(found as Record<string, unknown>)
    currentVersion.value = Number((found as { meta?: { version?: number } }).meta?.version ?? 1)
  } else {
    form.value = blankForm()
    currentVersion.value = null
  }
}

async function refresh(): Promise<void> {
  const pid = projects.currentProjectId
  if (!pid) return
  loading.value = true
  try {
    await projects.loadObjects('detectors')
  } finally {
    loading.value = false
  }
  // 选中项被删除时回退新建
  if (selectedId.value && !detectors.value.some((d) => (d as { detector_id?: string }).detector_id === selectedId.value)) {
    selectDetector('')
  }
}

async function save(): Promise<void> {
  const pid = projects.currentProjectId
  if (!pid || !form.value || !canSave.value) return
  saveError.value = null
  saveNotice.value = null
  const body: Record<string, unknown> = {
    schema_version: 1,
    detector_id: form.value.detectorId,
    type: form.value.type,
    roi: form.value.roi,
    threshold: form.value.threshold,
    stable_frames: form.value.stableFrames,
    field_name: form.value.fieldName,
  }
  if (form.value.template.trim()) body.template = form.value.template.trim()
  try {
    if (currentVersion.value !== null) {
      body.meta = { version: currentVersion.value }
      await apiFetch(`/api/v1/projects/${pid}/detectors/${form.value.detectorId}`, {
        method: 'PUT',
        body,
      })
    } else {
      await apiFetch(`/api/v1/projects/${pid}/detectors`, { method: 'POST', body })
      selectedId.value = form.value.detectorId
    }
    saveNotice.value = `已保存：${form.value.detectorId}`
    await refresh()
    selectDetector(selectedId.value)
  } catch (exc) {
    saveError.value = exc instanceof Error ? exc.message : String(exc)
  }
}

onMounted(async () => {
  await projects.loadProjects()
  if (projects.currentProjectId) {
    await refresh()
  }
  if (!form.value) selectDetector('')
})
</script>

<template>
  <section class="detector-editor" aria-label="检测器编辑器">
    <header class="bar">
      <h2>检测器编辑器</h2>
      <button @click="selectDetector('')">新建检测器</button>
    </header>

    <p v-if="!projects.currentProjectId" class="empty">请先在左侧选择一个项目</p>

    <div v-else class="columns">
      <aside class="list">
        <h3>检测器清单</h3>
        <p v-if="detectors.length === 0 && !loading" class="empty">暂无检测器 — 点「新建检测器」开始</p>
        <ul>
          <li v-for="d in detectors" :key="String(d.detector_id)">
            <button
              class="item"
              :class="{ active: selectedId === d.detector_id }"
              @click="selectDetector(String(d.detector_id))"
            >
              {{ d.detector_id }}
              <span class="tag">{{ d.type }}</span>
            </button>
          </li>
        </ul>
      </aside>

      <form v-if="form" class="editor" @submit.prevent="save">
        <h3>{{ currentVersion !== null ? `编辑：${selectedId}` : '新建检测器' }}</h3>

        <label>
          检测器 ID
          <input v-model="form.detectorId" :disabled="currentVersion !== null" placeholder="health-bar" />
        </label>

        <label>
          类型
          <select v-model="form.type">
            <option v-for="t in DETECTOR_TYPES" :key="t" :value="t">{{ t }}</option>
          </select>
        </label>

        <label>
          阈值（0~1）
          <input v-model.number="form.threshold" type="number" step="0.01" min="0" max="1" />
        </label>

        <fieldset class="roi">
          <legend>ROI（归一化 x, y, w, h）</legend>
          <div class="roi-grid">
            <input v-for="(_, i) in form.roi" :key="i" v-model.number="form.roi[i]" type="number" step="0.005" min="0" max="1" :aria-label="`ROI 分量 ${i + 1}`" />
          </div>
        </fieldset>

        <label>
          稳定帧数（≥1 整数）
          <input v-model.number="form.stableFrames" type="number" step="1" min="1" />
        </label>

        <label>
          感知字段名
          <input v-model="form.fieldName" placeholder="health_ratio" />
        </label>

        <label v-if="form.type === 'template_match'">
          模板资产路径
          <input v-model="form.template" placeholder="assets/templates/btn.png" />
        </label>

        <div class="offline-check" data-testid="offline-check">
          <h4>离线校验</h4>
          <p v-if="issues.length > 0" class="issue" role="alert">
            <span aria-hidden="true">⚠</span> {{ issues[0].message }}
          </p>
          <p v-else class="ok">
            <span aria-hidden="true">✔</span> {{ thresholdHint(form) }}
          </p>
          <p class="muted">
            实时阈值效果需运行管线（M4 接入 /preview/shot 在线预览）；
            当前可<button type="button" class="link" @click="preview.captureOnce()">抓取一帧主屏预览</button>
            <span v-if="preview.frameCount > 0">（已抓 {{ preview.frameCount }} 帧）</span>
          </p>
          <ul v-if="issues.length > 1" class="issues">
            <li v-for="issue in issues.slice(1)" :key="issue.field + issue.message">{{ issue.message }}</li>
          </ul>
        </div>

        <div class="actions">
          <button type="submit" :disabled="!canSave">保存</button>
          <span v-if="saveNotice" role="status">{{ saveNotice }}</span>
          <span v-if="saveError" class="issue" role="alert">⚠ {{ saveError }}</span>
        </div>
      </form>
    </div>
  </section>
</template>

<style scoped>
.detector-editor {
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
.columns {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 20px;
}
.list ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.item {
  width: 100%;
  text-align: left;
  display: flex;
  justify-content: space-between;
  gap: 6px;
}
.item.active {
  outline: 2px solid #3b82f6;
}
.tag {
  color: #64748b;
  font-size: 11px;
}
.editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 520px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: #94a3b8;
}
input,
select {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 6px;
  color: inherit;
  padding: 6px 8px;
}
.roi {
  border: 1px solid #1e293b;
  border-radius: 6px;
}
.roi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
}
.offline-check {
  border: 1px dashed #334155;
  border-radius: 6px;
  padding: 8px 10px;
}
.offline-check h4 {
  margin: 0 0 4px;
  font-size: 12px;
  color: #94a3b8;
}
.issue {
  margin: 0;
  color: #f87171;
  font-size: 13px;
}
.ok {
  margin: 0;
  color: #6ee7b7;
  font-size: 13px;
}
.muted {
  color: #64748b;
  font-size: 12px;
}
.link {
  border: none;
  background: none;
  color: #60a5fa;
  padding: 0;
  text-decoration: underline;
}
.issues {
  margin: 4px 0 0;
  padding-left: 18px;
  color: #f87171;
  font-size: 12px;
}
.actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.empty {
  color: #64748b;
}
</style>
