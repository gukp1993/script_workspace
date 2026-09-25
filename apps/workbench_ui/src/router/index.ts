import { createRouter, createWebHashHistory } from 'vue-router'

// hash 路由：桌面壳/静态托管下刷新无需服务端回退
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/targets' },
    { path: '/targets', name: 'targets', component: () => import('../views/TargetSelect.vue') },
    { path: '/preview', name: 'preview', component: () => import('../views/LivePreview.vue') },
  ],
})

export default router
