<script setup lang="ts">
/**
 * 视觉资产库（UI-006）。
 *
 * - 模板/掩码资产网格：缩略图（内容端点取图）、命名、sha 前 8 位、
 *   引用该资产的检测器数量；
 * - 删除保护：被检测器引用的资产删除按钮禁用并显示引用明细（不只靠
 *   颜色——加"被引用"文字与图标）；未引用资产点击删除时若控制面未提供
 *   删除端点，显示明确的降级说明（M3 核心版不静默假成功）。
 */
import { computed, onMounted, ref } from 'vue'

import { apiFetch, fetchAssetBytes } from '../api/client'
import { detectorsReferencing, shortSha, type AssetEntry } from '../domain/machineYaml'
import { useProjectStore } from '../stores/project'

const projects = useProjectStore()

const assets = ref<AssetEntry[]>([])
const detectors = ref<Array<Record<string, unknown>>>([])
const loading = ref(false)
const error = ref<string | null>(null)
const thumbnails = ref<Record<string, string>>({})
/** 删除尝试的结果提示（含控制面未提供删除端点的降级说明） */
const deleteNotice = ref<string | null>(null)

const hasProject = computed(() => projects.currentProjectId !== null)

function referencingDetectors(asset: AssetEntry): string[] {
  return detectorsReferencing(asset, detectors.value)
}

async function load(): Promise<void> {
  const pid = projects.currentProjectId
  if (!pid) return
  loading.value = true
  error.value = null
  try {
    const [assetRes, detectorRes] = await Promise.all([
      apiFetch<{ assets: AssetEntry[] }>(`/api/v1/projects/${pid}/assets`),
      apiFetch<{ objects: Array<Record<string, unknown>> }>(`/api/v1/projects/${pid}/detectors`),
    ])
    assets.value = assetRes.assets
    detectors.value = detectorRes.objects
    void loadThumbnails(pid)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    loading.value = false
  }
}

async function loadThumbnails(pid: string): Promise<void> {
  for (const asset of assets.value) {
    if (!/\.(png|jpg)$/i.test(asset.path)) continue
    try {
      const blob = await fetchAssetBytes(pid, asset.asset_id)
      thumbnails.value = { ...thumbnails.value, [asset.asset_id]: URL.createObjectURL(blob) }
    } catch {
      // 缩略图失败不阻断列表（占位块由模板渲染）
    }
  }
}

/** 删除（保护逻辑：被引用 -> 禁用；未引用 -> 尝试调用并如实反馈） */
async function requestDelete(asset: AssetEntry): Promise<void> {
  const pid = projects.currentProjectId
  if (!pid) return
  deleteNotice.value = null
  try {
    await apiFetch(`/api/v1/projects/${pid}/assets/${asset.asset_id}`, { method: 'DELETE' })
    await load()
  } catch (exc) {
    const status = (exc as { status?: number }).status
    deleteNotice.value =
      status === 404 || status === 405
        ? '控制面暂未提供资产删除端点（CTL-004 后续版本提供），此处不做假删除'
        : `删除失败：${exc instanceof Error ? exc.message : String(exc)}`
  }
}

onMounted(async () => {
  await projects.loadProjects()
  await load()
})
</script>

<template>
  <section class="asset-library" aria-label="视觉资产库">
    <header class="bar">
      <h2>视觉资产库</h2>
      <button :disabled="!hasProject || loading" @click="load">刷新</button>
    </header>

    <p v-if="!hasProject" class="empty">请先在左侧选择一个项目</p>
    <p v-else-if="error" class="error" role="alert">
      ⚠ 加载失败：{{ error }}
      <button @click="load">重试</button>
    </p>
    <p v-else-if="assets.length === 0 && !loading" class="empty">
      当前项目没有资产 — 上传模板/掩码后在此管理（资产经控制面 /assets 端点写入）
    </p>

    <p v-if="deleteNotice" class="notice" role="status">{{ deleteNotice }}</p>

    <ul class="grid">
      <li v-for="asset in assets" :key="asset.asset_id" class="card">
        <div class="thumb">
          <img v-if="thumbnails[asset.asset_id]" :src="thumbnails[asset.asset_id]" :alt="`缩略图 ${asset.asset_id}`" />
          <span v-else class="thumb-fallback" aria-hidden="true">{{ asset.kind === 'mask' ? '掩' : '模' }}</span>
        </div>
        <h3>{{ asset.asset_id }}</h3>
        <p class="meta">{{ asset.path }}</p>
        <p class="meta">sha {{ shortSha(asset) }} · {{ asset.kind }} · v{{ asset.version }}</p>
        <p v-if="referencingDetectors(asset).length > 0" class="ref">
          <span aria-hidden="true">🔒</span> 被 {{ referencingDetectors(asset).length }} 个检测器引用（{{
            referencingDetectors(asset).join('、')
          }}）
        </p>
        <p v-else class="ref free">未被检测器引用</p>
        <button
          class="danger"
          :disabled="referencingDetectors(asset).length > 0"
          :title="referencingDetectors(asset).length > 0 ? '被检测器引用的资产不可删除' : '删除该资产'"
          @click="requestDelete(asset)"
        >
          删除
        </button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.asset-library {
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
.grid {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}
.card {
  border: 1px solid #1e293b;
  border-radius: 8px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: #111c31;
}
h3 {
  margin: 0;
  font-size: 14px;
}
.thumb {
  height: 90px;
  border-radius: 6px;
  background: #020617;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
.thumb img {
  max-width: 100%;
  max-height: 100%;
}
.thumb-fallback {
  color: #475569;
  font-size: 28px;
}
.meta {
  margin: 0;
  color: #94a3b8;
  font-size: 12px;
  word-break: break-all;
}
.ref {
  margin: 0;
  color: #fbbf24;
  font-size: 12px;
}
.ref.free {
  color: #64748b;
}
.danger {
  border-color: #ef4444;
  color: #fca5a5;
  align-self: flex-start;
}
.empty {
  color: #64748b;
}
.error {
  color: #f87171;
}
.notice {
  color: #94a3b8;
  border: 1px solid #334155;
  border-radius: 6px;
  padding: 6px 10px;
}
</style>
