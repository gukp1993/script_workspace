<script setup lang="ts">
/**
 * 项目导航与对象树（UI-002 基础版 + M3 E11 页面导航）。
 *
 * 顶部项目选择/创建，下方按 目标/策略/检测器/状态机/标定 五类懒加载
 * 对象列表（点击种类标题拉取）。会话导航由底部控制条与目标选择页承担；
 * 中部为工作台页面入口（RouterLink，键盘可达）。
 */
import { onMounted, ref } from 'vue'

import { NAV_KINDS, objectLabel, useProjectStore } from '../stores/project'

/** 工作台页面入口（UI-017：全部页面可经左侧导航到达） */
const PAGE_LINKS: ReadonlyArray<{ to: string; label: string }> = [
  { to: '/targets', label: '目标选择' },
  { to: '/preview', label: '实时预览' },
  { to: '/assets', label: '资产库' },
  { to: '/detectors', label: '检测器编辑' },
  { to: '/calibration', label: '标定向导' },
  { to: '/machines', label: '状态机编辑' },
  { to: '/graph', label: '状态图' },
  { to: '/graph-edit', label: '节点编辑' },
  { to: '/inspector', label: '会话检视' },
  { to: '/timeline', label: '时间轴' },
  { to: '/tests', label: '测试中心' },
  { to: '/settings', label: '设置' },
]

const store = useProjectStore()
const newProjectId = ref('')
const newProjectName = ref('')
const expanded = ref<Set<string>>(new Set())

onMounted(() => {
  void store.loadProjects()
})

function toggle(kind: string): void {
  if (expanded.value.has(kind)) {
    expanded.value.delete(kind)
  } else {
    expanded.value.add(kind)
    void store.loadObjects(kind)
  }
  expanded.value = new Set(expanded.value)
}

async function createProject(): Promise<void> {
  const id = newProjectId.value.trim()
  const name = newProjectName.value.trim() || id
  if (!id) return
  await store.createProject(id, name)
  newProjectId.value = ''
  newProjectName.value = ''
}
</script>

<template>
  <nav class="nav" aria-label="项目与对象树">
    <section class="projects">
      <h3>项目</h3>
      <p v-if="store.error" class="error" role="alert">加载失败：{{ store.error }}</p>
      <ul>
        <li v-for="p in store.projects" :key="p.project_id">
          <button
            class="project"
            :class="{ active: p.project_id === store.currentProjectId }"
            :title="p.description || p.name"
            @click="store.selectProject(p.project_id)"
          >
            {{ p.name || p.project_id }}
          </button>
        </li>
        <li v-if="store.projects.length === 0 && !store.loading" class="empty">暂无项目</li>
      </ul>
      <form class="create" @submit.prevent="createProject">
        <input v-model="newProjectId" placeholder="project-id" aria-label="新项目 ID" />
        <input v-model="newProjectName" placeholder="名称" aria-label="新项目名称" />
        <button type="submit" :disabled="!newProjectId.trim()">创建</button>
      </form>
    </section>

    <section class="pages">
      <h3>页面</h3>
      <ul>
        <li v-for="link in PAGE_LINKS" :key="link.to">
          <RouterLink class="page-link" :to="link.to" active-class="active">{{ link.label }}</RouterLink>
        </li>
      </ul>
    </section>

    <section class="tree">
      <h3>对象树</h3>
      <p v-if="!store.currentProjectId" class="empty">先选择一个项目</p>
      <ul>
        <li v-for="{ kind, label } in NAV_KINDS" :key="kind">
          <button class="kind" :aria-expanded="expanded.has(kind)" @click="toggle(kind)">
            <span class="caret">{{ expanded.has(kind) ? '▾' : '▸' }}</span>
            {{ label }}
            <span class="count">{{ store.objects[kind]?.length ?? '' }}</span>
          </button>
          <ul v-if="expanded.has(kind)">
            <li v-for="obj in store.objects[kind] ?? []" :key="objectLabel(kind, obj)" class="leaf">
              {{ objectLabel(kind, obj) }}
            </li>
            <li v-if="(store.objects[kind] ?? []).length === 0" class="empty">（空）</li>
          </ul>
        </li>
      </ul>
    </section>
  </nav>
</template>

<style scoped>
.nav {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
h3 {
  margin: 0 0 8px;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #94a3b8;
}
ul {
  list-style: none;
  margin: 0;
  padding: 0;
}
.project {
  width: 100%;
  text-align: left;
  border: none;
  background: transparent;
  padding: 6px 8px;
}
.project.active {
  background: #1d4ed855;
  border-left: 3px solid #3b82f6;
}
.kind {
  width: 100%;
  text-align: left;
  background: transparent;
  border: none;
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 6px 8px;
}
.page-link {
  display: block;
  width: 100%;
  text-align: left;
  text-decoration: none;
  color: #cbd5e1;
  padding: 5px 8px;
  border-radius: 6px;
}
.page-link:hover {
  background: #1e293b;
}
.page-link.active {
  background: #1d4ed855;
  color: #e2e8f0;
  border-left: 3px solid #3b82f6;
}
.caret {
  width: 12px;
  color: #64748b;
}
.count {
  margin-left: auto;
  color: #64748b;
  font-size: 12px;
}
.leaf {
  padding: 4px 8px 4px 30px;
  color: #cbd5e1;
  cursor: default;
}
.empty {
  color: #64748b;
  font-size: 12px;
  padding: 4px 8px;
}
.error {
  color: #f87171;
  font-size: 12px;
}
.create {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}
input {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 6px;
  color: inherit;
  padding: 6px 8px;
}
</style>
