<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>数据审计管理</h2>
        <p class="page-desc">维护审计记录，围绕记录编号、操作类型、操作人、操作对象做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记审计记录</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出数据审计清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据审计数据，可先登记审计记录</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记审计记录</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="query-panel" data-testid="audit-query-diagnostics">
      <header class="panel-head">
        <h3>查询失败诊断记录（跨模块只读）</h3>
        <span class="panel-desc">数据审计复核 OLAP 查询失败诊断；列表按诊断编号去重，详情对照既有处理输入。</span>
        <button class="btn ghost" type="button" data-testid="reload-query-diagnostics" @click="loadQueryDiagnostics()">刷新</button>
      </header>
      <table class="data-table">
        <thead>
          <tr><th>诊断编号</th><th>模板编号</th><th>失败类型</th><th>状态</th><th>重复次数</th><th>参数摘要</th><th>失败原因</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="item in queryDiagnostics"
            :key="String(item.id)"
            :data-testid="`audit-diagnostic-row-${item.id}`"
            :data-status="String(item.status)"
            style="cursor:pointer"
            @click="openQueryDiagnostic(item.id)"
          >
            <td>{{ item['诊断编号'] }}</td>
            <td>{{ item['模板编号'] }}</td>
            <td>{{ item.kind }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.occurrences }}</td>
            <td>{{ item.paramSummary }}</td>
            <td>{{ item.reason }}</td>
          </tr>
          <tr v-if="!queryDiagnostics.length">
            <td colspan="7" class="empty-state">暂无查询失败诊断记录</td>
          </tr>
        </tbody>
      </table>
      <div v-if="queryDiagnosticDetail" class="query-result" data-testid="audit-diagnostic-detail">
        <div class="result-head">
          <strong>{{ queryDiagnosticDetail['诊断编号'] }} · {{ queryDiagnosticDetail.kind }}</strong>
          <span class="result-meta">状态：{{ queryDiagnosticDetail.status }}</span>
          <span class="result-meta">模板：{{ queryDiagnosticDetail['模板编号'] }}</span>
        </div>
        <p class="result-reason">{{ queryDiagnosticDetail.reason }}</p>
        <dl class="diagnostic-fields">
          <dt>参数摘要</dt><dd>{{ queryDiagnosticDetail.paramSummary }}</dd>
          <dt>处理输入</dt><dd>{{ JSON.stringify(queryDiagnosticDetail['处理输入'] ?? {}) }}</dd>
          <dt>已返回行</dt><dd>{{ queryDiagnosticDetail.returnedRowCount }} 行</dd>
          <dt>核对确认</dt>
          <dd>{{ queryDiagnosticDetail.confirmation ? `${queryDiagnosticDetail.confirmation['确认编号']} · ${queryDiagnosticDetail.confirmation['确认结论']}` : '尚未核对' }}</dd>
        </dl>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据审计记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/audit'
const columns = ["记录编号", "操作类型", "操作人", "操作对象", "操作时间", "变更内容", "审计结果", "记录状态"]
const actions = ["提交审计", "标记异常"]
const statuses = ["已记录", "已审计", "异常"]
const metricNames = ["审计记录数", "异常记录数", "已审计记录数"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'audit.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据审计动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据审计操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('审计记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据审计列表读取失败'
  }
}

const queryDiagnostics = ref<any[]>([])
const queryDiagnosticDetail = ref<any>(null)

async function loadQueryDiagnostics() {
  try {
    const response = await request(`${ENDPOINT}/query-diagnostics`)
    if (!response.ok) throw new Error('查询失败诊断记录读取失败')
    const payload = await response.json()
    queryDiagnostics.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '查询失败诊断记录读取失败'
  }
}

async function openQueryDiagnostic(diagnosticId: number) {
  try {
    const response = await request(`${ENDPOINT}/query-diagnostics/${diagnosticId}`)
    if (!response.ok) throw new Error('查询失败诊断记录读取失败')
    queryDiagnosticDetail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '查询失败诊断记录读取失败'
  }
}

onMounted(async () => {
  await reload()
  await loadQueryDiagnostics()
})
</script>

<style scoped>
.query-panel { margin-top: 16px; padding: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; }
.panel-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.panel-head h3 { margin: 0; font-size: 15px; }
.panel-desc { color: var(--muted); font-size: 12px; flex: 1; }
.query-result { margin-top: 12px; border-top: 1px solid var(--border); padding-top: 10px; }
.result-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 13px; }
.result-reason { font-size: 13px; margin: 8px 0; }
.diagnostic-fields { display: grid; grid-template-columns: 120px 1fr; gap: 4px 8px; font-size: 13px; }
.diagnostic-fields dt { color: var(--muted); font-size: 12px; }
.diagnostic-fields dd { margin: 0; }
</style>
