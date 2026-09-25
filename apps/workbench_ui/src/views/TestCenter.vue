<script setup lang="ts">
/**
 * 测试中心（UI-015）。
 *
 * - 回放/差异报告列表：读项目 tests/replay 目录产物（控制面
 *   /replay-reports 端点），含名称/大小/修改时间与内联预览；
 * - 点击报告拉取全文并渲染（markdown/文本按纯文本渲染，等宽可读）；
 * - 目录不存在/为空时显示明确的空态说明（不空白、不报错）。
 */
import { computed, onMounted, ref } from 'vue'

import { apiFetch, type ReplayReportSummary } from '../api/client'
import { useProjectStore } from '../stores/project'

const projects = useProjectStore()

const reports = ref<ReplayReportSummary[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const selectedName = ref('')
const content = ref<string | null>(null)
const contentError = ref<string | null>(null)
const loadingContent = ref(false)

const hasProject = computed(() => projects.currentProjectId !== null)

async function load(): Promise<void> {
  const pid = projects.currentProjectId
  if (!pid) return
  loading.value = true
  error.value = null
  try {
    const data = await apiFetch<{ reports: ReplayReportSummary[] }>(`/api/v1/projects/${pid}/replay-reports`)
    reports.value = data.reports
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    loading.value = false
  }
}

async function open(report: ReplayReportSummary): Promise<void> {
  const pid = projects.currentProjectId
  if (!pid) return
  selectedName.value = report.name
  contentError.value = null
  loadingContent.value = true
  try {
    const data = await apiFetch<{ content: string }>(`/api/v1/projects/${pid}/replay-reports/${report.name}/content`)
    content.value = data.content
  } catch (exc) {
    content.value = null
    contentError.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    loadingContent.value = false
  }
}

onMounted(async () => {
  await projects.loadProjects()
  await load()
})
</script>

<template>
  <section class="test-center" aria-label="测试中心">
    <header class="bar">
      <h2>测试中心</h2>
      <button :disabled="!hasProject || loading" @click="load">刷新</button>
    </header>

    <p v-if="!hasProject" class="empty">请先在左侧选择一个项目</p>
    <p v-else-if="error" class="error" role="alert">⚠ {{ error }} <button @click="load">重试</button></p>
    <p v-else-if="reports.length === 0 && !loading" class="empty">
      tests/replay 目录暂无回放/差异报告 — 通过回放工具（trace_format replay/diff）生成产物后在此查看
    </p>

    <div v-else class="layout">
      <ul class="list">
        <li v-for="report in reports" :key="report.name">
          <button class="item" :class="{ active: selectedName === report.name }" @click="open(report)">
            <strong>{{ report.name }}</strong>
            <span class="meta">{{ report.rel_path }} · {{ report.size_bytes }} B · {{ report.modified_at }}</span>
            <span v-if="report.preview" class="meta preview-line">{{ report.preview.slice(0, 60) }}</span>
          </button>
        </li>
      </ul>

      <div class="viewer">
        <p v-if="!selectedName" class="empty">选择左侧报告查看全文</p>
        <p v-else-if="loadingContent" class="empty">加载中…</p>
        <p v-else-if="contentError" class="error" role="alert">⚠ {{ contentError }}</p>
        <template v-else-if="content !== null">
          <h3>{{ selectedName }}</h3>
          <pre class="report" data-testid="report-content">{{ content }}</pre>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.test-center {
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
h3 {
  margin: 0 0 8px;
  font-size: 14px;
}
.layout {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 16px;
  align-items: start;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.item {
  width: 100%;
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.item.active {
  outline: 2px solid #3b82f6;
}
.meta {
  color: #64748b;
  font-size: 11px;
}
.preview-line {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.viewer {
  min-height: 200px;
}
.report {
  margin: 0;
  background: #020617;
  border: 1px solid #1e293b;
  border-radius: 6px;
  padding: 12px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  overflow: auto;
  max-height: 480px;
}
.error {
  color: #f87171;
}
.empty {
  color: #64748b;
}
</style>
