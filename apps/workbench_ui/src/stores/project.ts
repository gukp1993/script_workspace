/**
 * 项目与领域对象树 store（UI-002）。
 *
 * 负责项目列表、当前项目与五类对象（目标/策略/检测器/状态机/标定）
 * 的加载；会话列表走 sessionStore。所有 action 支持注入 fetch 实现
 * （默认 apiFetch），便于单测。
 */

import { defineStore } from 'pinia'

import { apiFetch } from '../api/client'

export interface ProjectSummary {
  project_id: string
  name: string
  description: string
  counts?: Record<string, number>
}

/** 领域对象（不同 kind 的 id 字段不同，见 storage.KIND_PARSERS） */
export interface DomainObject {
  [key: string]: unknown
}

/** 对象种类 -> id 字段（与 control_plane.storage.KIND_PARSERS 对齐） */
export const KIND_ID_FIELDS: Readonly<Record<string, string>> = {
  targets: 'target_id',
  policies: 'policy_id',
  detectors: 'detector_id',
  machines: 'machine_id',
  calibrations: 'calibration_id',
}

/** 对象树的展示顺序（UI-002） */
export const NAV_KINDS: ReadonlyArray<{ kind: string; label: string }> = [
  { kind: 'targets', label: '目标' },
  { kind: 'policies', label: '策略' },
  { kind: 'detectors', label: '检测器' },
  { kind: 'machines', label: '状态机' },
  { kind: 'calibrations', label: '标定' },
]

/** 从任意领域对象里取展示用 ID */
export function objectLabel(kind: string, obj: DomainObject): string {
  const field = KIND_ID_FIELDS[kind]
  const id = field ? obj[field] : undefined
  if (typeof id === 'string' && id) return id
  if (typeof obj.id === 'string') return obj.id
  return JSON.stringify(obj).slice(0, 24)
}

type Fetcher = typeof apiFetch

interface ListObjectsResponse {
  project_id: string
  kind: string
  objects: DomainObject[]
  count: number
}

export const useProjectStore = defineStore('project', {
  state: () => ({
    projects: [] as ProjectSummary[],
    currentProjectId: null as string | null,
    objects: {} as Record<string, DomainObject[]>,
    loading: false,
    error: null as string | null,
  }),
  getters: {
    currentProject(state): ProjectSummary | null {
      return state.projects.find((p) => p.project_id === state.currentProjectId) ?? null
    },
  },
  actions: {
    async loadProjects(fetch: Fetcher = apiFetch): Promise<void> {
      this.loading = true
      this.error = null
      try {
        const data = await fetch<{ projects: ProjectSummary[]; count: number }>('/api/v1/projects')
        this.projects = data.projects
        if (this.currentProjectId === null && this.projects.length > 0) {
          this.currentProjectId = this.projects[0].project_id
        }
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
      } finally {
        this.loading = false
      }
    },
    async createProject(projectId: string, name: string, fetch: Fetcher = apiFetch): Promise<void> {
      this.error = null
      try {
        await fetch('/api/v1/projects', {
          method: 'POST',
          body: { project_id: projectId, name, description: '' },
        })
        await this.loadProjects(fetch)
        this.currentProjectId = projectId
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
      }
    },
    selectProject(projectId: string): void {
      if (this.currentProjectId !== projectId) {
        this.currentProjectId = projectId
        // 重新加载已打开过的对象种类：切换项目后，停留在原页面的视图
        // 依赖本 store 的对象缓存，清空后必须立即重取，否则列表会一直是空的。
        const loadedKinds = Object.keys(this.objects).filter(
          (kind) => (this.objects[kind] ?? []).length > 0,
        )
        this.objects = {}
        for (const kind of loadedKinds) void this.loadObjects(kind)
      }
    },
    async loadObjects(kind: string, fetch: Fetcher = apiFetch): Promise<void> {
      const pid = this.currentProjectId
      if (!pid) {
        this.objects = { ...this.objects, [kind]: [] }
        return
      }
      this.error = null
      try {
        const data = await fetch<ListObjectsResponse>(`/api/v1/projects/${pid}/${kind}`)
        this.objects = { ...this.objects, [kind]: data.objects }
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
      }
    },
  },
})
