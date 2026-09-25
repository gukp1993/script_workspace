import { createRouter, createWebHashHistory } from 'vue-router'

// hash 路由：桌面壳/静态托管下刷新无需服务端回退
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/targets' },
    { path: '/targets', name: 'targets', component: () => import('../views/TargetSelect.vue') },
    { path: '/preview', name: 'preview', component: () => import('../views/LivePreview.vue') },
    // M3 E11：工作台主体页面
    { path: '/assets', name: 'assets', component: () => import('../views/AssetLibrary.vue') },
    { path: '/detectors', name: 'detectors', component: () => import('../views/DetectorEditor.vue') },
    { path: '/calibration', name: 'calibration', component: () => import('../views/CalibrationWizard.vue') },
    { path: '/machines', name: 'machines', component: () => import('../views/MachineEditor.vue') },
    { path: '/graph', name: 'graph', component: () => import('../views/StateGraph.vue') },
    // M4 EDT-001/002：可编辑节点图（与表单/YAML 同一数据源）
    { path: '/graph-edit', name: 'graph-edit', component: () => import('../components/NodeGraphEditor.vue') },
    { path: '/inspector', name: 'inspector', component: () => import('../views/Inspector.vue') },
    { path: '/timeline', name: 'timeline', component: () => import('../views/TimelineView.vue') },
    { path: '/tests', name: 'tests', component: () => import('../views/TestCenter.vue') },
    { path: '/settings', name: 'settings', component: () => import('../views/SettingsView.vue') },
  ],
})

export default router
