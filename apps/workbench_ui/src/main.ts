import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'

/**
 * 应用入口（ENG-003）。
 *
 * 桌面壳以 `http://127.0.0.1:<port>/?token=<t>` 加载本前端；
 * 首个 API 请求会经 resolveToken() 把 query 令牌持久化到 localStorage。
 */
const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
