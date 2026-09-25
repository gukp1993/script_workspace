<script setup lang="ts">
import ControlBar from './views/ControlBar.vue'
import ProjectNav from './views/ProjectNav.vue'
</script>

<template>
  <div class="app">
    <header class="app-header">
      <span class="brand">VAW 前台视觉自动化工作台</span>
      <span class="sub">M1 安全闭环 · 仅本机回环</span>
    </header>
    <div class="app-body">
      <aside class="app-side">
        <ProjectNav />
      </aside>
      <main class="app-main">
        <router-view />
      </main>
    </div>
    <footer class="app-footer">
      <ControlBar />
    </footer>
  </div>
</template>

<style>
:root {
  color-scheme: dark;
  font-family: 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif;
  font-size: 14px;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  background: #0f172a;
  color: #e2e8f0;
}
button {
  font: inherit;
  cursor: pointer;
  border-radius: 6px;
  border: 1px solid #334155;
  background: #1e293b;
  color: #e2e8f0;
  padding: 6px 14px;
}
button:hover:not(:disabled) {
  border-color: #64748b;
}
button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.app {
  display: flex;
  flex-direction: column;
  height: 100vh;
}
.app-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid #1e293b;
}
.app-header .brand {
  font-weight: 600;
  font-size: 16px;
}
.app-header .sub {
  color: #64748b;
  font-size: 12px;
}
.app-body {
  display: flex;
  flex: 1;
  min-height: 0;
}
.app-side {
  width: 260px;
  border-right: 1px solid #1e293b;
  overflow-y: auto;
}
.app-main {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
.app-footer {
  border-top: 1px solid #1e293b;
  padding: 8px 16px;
}
</style>
