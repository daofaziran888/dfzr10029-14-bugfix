<template>
  <section class="page" data-module="query_builder">
    <header class="page-head">
      <div>
        <h2>OLAP查询管理</h2>
        <p class="page-desc">维护查询模板，围绕模板编号、模板名称、查询语句、参数定义做登记、筛选与状态流转；执行查询后按终态区分空数据、空参数与超时。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记查询模板</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出OLAP查询清单</button>
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
            <button
              class="link"
              type="button"
              :data-testid="`run-query-${row.id}`"
              @click="selectTemplate(row)"
            >
              执行查询
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无OLAP查询数据，可先登记查询模板</td>
        </tr>
      </tbody>
    </table>

    <section class="query-panel" data-testid="query-panel">
      <header class="panel-head">
        <h3>查询执行</h3>
        <span class="panel-desc">先补齐参数再执行；空参数和超时会给出失败终态与原因，不会被当成成功空页。</span>
      </header>

      <form class="analysis-form" @submit.prevent="executeQuery">
        <label>
          <span>查询模板</span>
          <select v-model="selectedTemplateId" data-testid="query-template" @change="syncParamFields">
            <option :value="0" disabled>请选择查询模板</option>
            <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
              {{ row['模板编号'] }} · {{ row['模板名称'] }}
            </option>
          </select>
        </label>
        <label v-for="name in declaredParams" :key="name">
          <span>{{ name }}</span>
          <input v-model="runParams[name]" :data-testid="`query-param-${name}`" :placeholder="`请输入${name}`" />
        </label>
        <label>
          <span>返回行数</span>
          <input v-model="runOptions.rowCount" data-testid="query-row-count" type="number" min="0" max="200" />
        </label>
        <label>
          <span>预计耗时秒数</span>
          <input v-model="runOptions.elapsed" data-testid="query-elapsed" type="number" min="0" step="0.5" />
        </label>
        <label>
          <span>超时秒数（可选覆盖）</span>
          <input v-model="runOptions.timeout" data-testid="query-timeout" type="number" min="1" placeholder="沿用模板设置" />
        </label>
        <label>
          <span>超时策略</span>
          <select v-model="runOptions.policy" data-testid="query-timeout-policy">
            <option value="保留部分结果">保留部分结果</option>
            <option value="回滚整份查询">回滚整份查询</option>
          </select>
        </label>
        <label class="checkbox-item">
          <span>中断恢复</span>
          <input v-model="runOptions.resume" data-testid="query-resume" type="checkbox" />
        </label>
        <button class="btn primary" type="submit" data-testid="execute-query" :disabled="running">
          {{ running ? '查询中…' : '执行查询' }}
        </button>
        <button class="btn" type="button" data-testid="export-result" @click="exportResult">导出查询终态</button>
      </form>

      <div v-if="result" class="query-result" data-testid="query-result" :data-state="result.state">
        <div class="result-head">
          <span class="result-state" data-testid="result-state">{{ result.state }}</span>
          <span class="result-meta" data-testid="result-param-summary">参数摘要：{{ result.paramSummary }}</span>
          <span class="result-meta">返回 {{ result.rowCount }} 行</span>
          <span class="result-meta">耗时 {{ result.elapsedSeconds }}s / 超时 {{ result.timeoutSeconds }}s</span>
          <span v-if="result.diagnosticId" class="result-meta" data-testid="result-diagnostic">
            诊断 {{ result.诊断编号 }}
          </span>
        </div>
        <p class="result-reason" data-testid="failure-reason" :class="{ 'error-text': !result.ok }">{{ result.reason }}</p>
        <p v-if="result.notes?.length" class="result-notes" data-testid="result-notes">
          <span v-for="note in result.notes" :key="note">{{ note }}</span>
        </p>
        <table v-if="result.items.length" class="data-table">
          <thead>
            <tr><th v-for="column in resultColumns" :key="column">{{ column }}</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in result.items" :key="`${result.state}-${row['行号']}`" :data-testid="`result-row-${row['行号']}`">
              <td v-for="column in resultColumns" :key="column">{{ row[column] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-state" data-testid="result-empty">
          {{ result.ok ? '查询已执行，结果集为空（0 行）。' : '本次查询没有返回结果行，原因见上。' }}
        </p>
      </div>
      <p v-else class="empty-state">选择查询模板并补齐参数后执行查询。</p>
    </section>

    <section class="query-panel" data-testid="diagnostics-panel">
      <header class="panel-head">
        <h3>查询失败诊断记录</h3>
        <span class="panel-desc">空参数、超时与中断恢复的异常诊断留在待核对区；列表按诊断编号去重，核对结果只保留一份。</span>
        <button class="btn ghost" type="button" data-testid="reload-diagnostics" @click="loadDiagnostics()">刷新</button>
      </header>
      <table class="data-table diag-table">
        <thead>
          <tr>
            <th>诊断编号</th><th>模板编号</th><th>失败类型</th><th>状态</th>
            <th>重复次数</th><th>参数摘要</th><th>返回行数</th><th>失败原因</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in diagnostics"
            :key="String(item.id)"
            :data-testid="`diagnostic-row-${item.id}`"
            :data-status="String(item.status)"
            class="diag-row"
            @click="openDiagnostic(item.id)"
          >
            <td>{{ item['诊断编号'] }}</td>
            <td>{{ item['模板编号'] }}</td>
            <td>{{ item.kind }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.occurrences }}</td>
            <td>{{ item.paramSummary }}</td>
            <td>{{ item.returnedRowCount }}</td>
            <td>{{ item.reason }}</td>
          </tr>
          <tr v-if="!diagnostics.length">
            <td colspan="8" class="empty-state">暂无查询失败诊断记录</td>
          </tr>
        </tbody>
      </table>

      <div v-if="diagnosticDetail" class="query-result" data-testid="diagnostic-detail">
        <div class="result-head">
          <strong>{{ diagnosticDetail['诊断编号'] }} · {{ diagnosticDetail.kind }}</strong>
          <span class="result-meta" data-testid="diagnostic-status">状态：{{ diagnosticDetail.status }}</span>
          <span class="result-meta">重复次数：{{ diagnosticDetail.occurrences }}</span>
          <span class="result-meta">诊断编号：{{ diagnosticDetail.id }}</span>
        </div>
        <p class="result-reason" data-testid="diagnostic-reason">{{ diagnosticDetail.reason }}</p>
        <dl class="diagnostic-fields">
          <dt>模板编号</dt><dd>{{ diagnosticDetail['模板编号'] }}</dd>
          <dt>模板名称</dt><dd>{{ diagnosticDetail['模板名称'] }}</dd>
          <dt>参数摘要</dt><dd data-testid="diagnostic-param-summary">{{ diagnosticDetail.paramSummary }}</dd>
          <dt>超时设置</dt><dd>{{ diagnosticDetail.timeoutSeconds }} 秒（{{ diagnosticDetail.timeoutPolicy }}）</dd>
          <dt>实际耗时</dt><dd>{{ diagnosticDetail.elapsedSeconds }} 秒</dd>
          <dt>已返回行</dt><dd>{{ diagnosticDetail.returnedRowCount }} 行</dd>
          <dt>保留部分结果</dt><dd>{{ diagnosticDetail.partial ? '是' : '否' }}</dd>
          <dt>回滚整份查询</dt>
          <dd>{{ diagnosticDetail.rolledBack ? `是（丢弃 ${diagnosticDetail.rolledBackRowCount ?? 0} 行）` : '否' }}</dd>
        </dl>
        <div v-if="diagnosticDetail.confirmation" class="diag-confirmation" data-testid="diagnostic-confirmation">
          <strong>确认结果：{{ diagnosticDetail.confirmation['确认结论'] }}</strong>
          <span>{{ diagnosticDetail.confirmation['确认编号'] }} · {{ diagnosticDetail.confirmation['确认说明'] }}</span>
        </div>
        <div v-else class="page-actions">
          <button class="btn primary" type="button" :data-testid="`diagnostic-confirm-${diagnosticDetail.id}`" @click="confirmDiagnostic('确认失败')">确认失败</button>
          <button class="btn" type="button" :data-testid="`diagnostic-pass-${diagnosticDetail.id}`" @click="confirmDiagnostic('确认通过')">确认通过</button>
        </div>
        <p v-if="diagnosticMessage" class="result-meta" data-testid="diagnostic-message">{{ diagnosticMessage }}</p>
        <table v-if="diagnosticDetail.rows?.length" class="data-table">
          <thead><tr><th v-for="column in resultColumns" :key="column">{{ column }}</th></tr></thead>
          <tbody>
            <tr v-for="row in diagnosticDetail.rows" :key="`diag-${row['行号']}`" :data-testid="`diagnostic-result-row-${row['行号']}`">
              <td v-for="column in resultColumns" :key="column">{{ row[column] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记查询模板</h3>
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

    <footer class="page-foot">
      <span>共 {{ total }} 条OLAP查询记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type ResultRow = Record<string, string | number | null>

interface QueryResult {
  ok: boolean
  status: string
  state: string
  reasonCode: string
  reason: string
  paramSummary: string
  rowCount: number
  elapsedSeconds: number
  timeoutSeconds: number
  partial?: boolean
  rolledBack?: boolean
  notes?: string[]
  diagnosticId?: number | null
  诊断编号?: string | null
  items: ResultRow[]
}

interface DiagnosticRow {
  id: number
  诊断编号: string
  模板编号: string
  kind: string
  status: string
  occurrences: number
  模板名称?: string
  paramSummary: string
  returnedRowCount: number
  reason: string
  rows?: ResultRow[]
  confirmation?: Record<string, string> | null
  timeoutSeconds?: number
  timeoutPolicy?: string
  elapsedSeconds?: number
  partial?: boolean
  rolledBack?: boolean
  rolledBackRowCount?: number
}

const ENDPOINT = '/api/query_builder'
const columns = ["模板编号", "模板名称", "查询语句", "参数定义", "数据集范围", "超时秒数", "缓存策略", "模板状态"]
const actions = ["保存模板", "发布模板", "停用模板"]
const statuses = ["草稿", "已保存", "已发布", "已停用"]
const metricNames = ["已发布模板", "草稿模板", "停用模板"]
const resultColumns = ["行号", "分组", "维度", "指标值", "对比基准", "差异"]

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

const selectedTemplateId = ref(0)
const declaredParams = ref<string[]>([])
const runParams = ref<Record<string, string>>({})
const runOptions = ref({ rowCount: '6', elapsed: '0', timeout: '', policy: '保留部分结果', resume: false })
const running = ref(false)
const result = ref<QueryResult | null>(null)
const diagnostics = ref<DiagnosticRow[]>([])
const diagnosticDetail = ref<DiagnosticRow | null>(null)
const diagnosticMessage = ref('')

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
    link.download = 'query_builder.csv'
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
      throw new Error('OLAP查询动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'OLAP查询操作失败'
  }
}

function parseParamNames(definition: unknown): string[] {
  if (definition === null || definition === undefined) return []
  return String(definition)
    .split(/[,，、;；\n]+/)
    .map((token) => token.split(/[=:]/)[0].trim())
    .filter((name) => name && !['无', '无参数', 'none', 'null'].includes(name.toLowerCase()))
}

function currentTemplate(): Row | undefined {
  return rows.value.find((row) => Number(row.id) === selectedTemplateId.value)
}

function selectTemplate(row: Row) {
  selectedTemplateId.value = Number(row.id)
  syncParamFields()
}

function syncParamFields() {
  const template = currentTemplate()
  const names = parseParamNames(template?.['参数定义'])
  if (!names.length) {
    // 参数定义为空时沿用查询语句里的 :占位符，避免漏判空参数。
    const statement = String(template?.['查询语句'] ?? '')
    for (const match of statement.matchAll(/(?<!:):([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*)/g)) {
      if (!names.includes(match[1])) names.push(match[1])
    }
  }
  declaredParams.value = names
  runParams.value = Object.fromEntries(names.map((name) => [name, '']))
  result.value = null
}

async function executeQuery() {
  if (!selectedTemplateId.value) {
    errorMessage.value = '请先选择查询模板'
    return
  }
  running.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${selectedTemplateId.value}/run`, {
      method: 'POST',
      body: JSON.stringify({
        查询参数: runParams.value,
        返回行数: Number(runOptions.value.rowCount || 0),
        预计耗时秒数: Number(runOptions.value.elapsed || 0),
        超时秒数: runOptions.value.timeout === '' ? undefined : Number(runOptions.value.timeout),
        超时策略: runOptions.value.policy,
        中断恢复: runOptions.value.resume,
      }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || payload.message || '查询执行失败')
    result.value = payload as QueryResult
    if (!payload.ok) errorMessage.value = payload.reason
    runOptions.value.resume = false
    await loadDiagnostics(payload.diagnosticId ?? undefined)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '查询执行失败'
  } finally {
    running.value = false
  }
}

async function exportResult() {
  errorMessage.value = ''
  if (!selectedTemplateId.value) {
    errorMessage.value = '请先选择查询模板'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/results/export?templateId=${selectedTemplateId.value}`)
    if (!response.ok) throw new Error('查询终态导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'query_result.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '查询终态导出失败'
  }
}

async function loadDiagnostics(focusId?: number) {
  try {
    const response = await request(`${ENDPOINT}/diagnostics`)
    if (!response.ok) throw new Error('查询失败诊断记录读取失败')
    const payload = await response.json()
    diagnostics.value = payload.items ?? []
    if (focusId !== undefined) {
      await openDiagnostic(focusId)
    } else if (diagnosticDetail.value) {
      const stillThere = diagnostics.value.find((item) => item.id === diagnosticDetail.value?.id)
      if (!stillThere) diagnosticDetail.value = null
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '查询失败诊断记录读取失败'
  }
}

async function openDiagnostic(diagnosticId: number) {
  diagnosticMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/diagnostics/${diagnosticId}`)
    if (!response.ok) throw new Error('查询失败诊断记录读取失败')
    diagnosticDetail.value = (await response.json()) as DiagnosticRow
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '查询失败诊断记录读取失败'
  }
}

async function confirmDiagnostic(action: string) {
  if (!diagnosticDetail.value) return
  const diagnosticId = diagnosticDetail.value.id
  diagnosticMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/diagnostics/${diagnosticId}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '核对失败')
    await loadDiagnostics()
    await openDiagnostic(diagnosticId)
    diagnosticMessage.value = payload.message
  } catch (error) {
    diagnosticMessage.value = error instanceof Error ? error.message : '核对失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('查询模板列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (selectedTemplateId.value && !currentTemplate()) {
      selectedTemplateId.value = 0
      declaredParams.value = []
      runParams.value = {}
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'OLAP查询列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await loadDiagnostics()
})
</script>

<style scoped>
.query-panel {
  margin-top: 16px;
  padding: 12px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.panel-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.panel-head h3 { margin: 0; font-size: 15px; }
.panel-desc { color: var(--muted); font-size: 12px; flex: 1; }
.checkbox-item { flex-direction: row; align-items: center; gap: 6px; }
.query-result { margin-top: 12px; border-top: 1px solid var(--border); padding-top: 10px; }
.result-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 13px; }
.result-state { font-weight: 600; }
.query-result[data-state='数据返回'] .result-state,
.query-result[data-state='空数据'] .result-state { color: #15803d; }
.query-result[data-state='参数缺失'] .result-state,
.query-result[data-state='查询超时'] .result-state,
.query-result[data-state='中断恢复'] .result-state { color: #b42318; }
.result-reason { font-size: 13px; margin: 8px 0; }
.result-notes { display: flex; flex-direction: column; gap: 2px; color: var(--muted); font-size: 12px; }
.diag-row { cursor: pointer; }
.diag-table { margin-top: 8px; }
.diagnostic-fields { display: grid; grid-template-columns: 120px 1fr; gap: 4px 8px; font-size: 13px; }
.diagnostic-fields dt { color: var(--muted); font-size: 12px; }
.diagnostic-fields dd { margin: 0; }
.diag-confirmation { display: flex; flex-direction: column; gap: 4px; padding: 8px; background: #f0fdf4; border-radius: 6px; font-size: 13px; margin-top: 8px; }
</style>
