// ---- Vue 岛屿：请求日志面板（阶段 6）----
if (window.Vue && window.ElementPlus) {
  const LogsTemplate = `
<div>
  <!-- 日志列表卡片 -->
  <div class="card">
    <div class="card-header">
      <h2>请求日志</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="font-size:12px;color:var(--text2)">共 [[ total ]] 条日志</span>
        <el-button @click="load()">刷新</el-button>
        <el-button @click="openCleanup">定时清除</el-button>
        <el-button type="danger" @click="clearAll">清空</el-button>
      </div>
    </div>

    <!-- 筛选区 -->
    <div class="filters">
      <el-input v-model="filterIp" placeholder="按IP筛选" clearable style="width:160px" @input="debounceLoad" />
      <el-select v-model="filterProvider" placeholder="全部提供商" clearable style="width:160px" @change="load(1)">
        <el-option label="全部提供商" value="" />
        <el-option v-for="p in providerOptions" :key="p" :label="p" :value="p" />
      </el-select>
      <el-input v-model="filterModel" placeholder="按模型名过滤" clearable style="width:180px" @input="debounceLoad" />
      <el-select v-model="filterStatus" placeholder="全部状态" clearable style="width:120px" @change="load(1)">
        <el-option label="全部状态" value="" />
        <el-option label="成功" value="success" />
        <el-option label="失败" value="error" />
      </el-select>
    </div>

    <!-- 表格 -->
    <el-table :data="rows" v-loading="loading" style="width:100%" :max-height="tableMaxHeight" empty-text="暂无日志">
      <el-table-column label="时间" min-width="160">
        <template #default="{row}"><span style="white-space:nowrap;font-size:12px">[[ formatTime(row.request_time) ]]</span></template>
      </el-table-column>
      <el-table-column label="调用 IP" min-width="110">
        <template #default="{row}"><code style="font-size:12px;font-family:monospace">[[ esc(row.client_ip||'-') ]]</code></template>
      </el-table-column>
      <el-table-column label="提供商" min-width="100">
        <template #default="{row}"><span>[[ esc(row.provider) ]]</span></template>
      </el-table-column>
      <el-table-column label="调用模型" min-width="160" show-overflow-tooltip>
        <template #default="{row}"><code style="font-size:12px">[[ esc(row.source_model||row.model) ]]</code></template>
      </el-table-column>
      <el-table-column label="目标模型" min-width="160" show-overflow-tooltip>
        <template #default="{row}"><code style="font-size:12px">[[ esc(row.model) ]]</code></template>
      </el-table-column>
      <el-table-column label="入/缓/出(token)" min-width="130">
        <template #default="{row}">
          <span style="font-family:monospace;font-size:12px;white-space:nowrap">[[ formatTokens(row.input_tokens||0) ]]<span style="color:var(--text2)">/</span>[[ formatTokens(row.cache_read_input_tokens||0) ]]<span style="color:var(--text2)">/</span>[[ formatTokens(row.output_tokens||0) ]]</span>
        </template>
      </el-table-column>
      <el-table-column label="耗时" width="80" align="center">
        <template #default="{row}"><span style="font-size:12px">[[ (row.duration_ms/1000).toFixed(2) ]]s</span></template>
      </el-table-column>
      <el-table-column label="原始码" width="80" align="center">
        <template #default="{row}">
          <el-tag v-if="row.original_status_code && row.original_status_code>0" :type="row.original_status_code>=400?'danger':'success'" size="small">[[ row.original_status_code ]]</el-tag>
          <span v-else style="color:var(--text2)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="映射码" width="80" align="center">
        <template #default="{row}">
          <el-tag v-if="row.original_status_code && row.original_status_code>0 && row.mapped_status_code && row.original_status_code!==row.mapped_status_code" type="warning" size="small">[[ row.mapped_status_code ]]</el-tag>
          <el-tag v-else-if="row.original_status_code && row.original_status_code>0" :type="row.original_status_code>=400?'danger':'success'" size="small">[[ row.mapped_status_code||row.original_status_code ]]</el-tag>
          <span v-else style="color:var(--text2)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="80" align="center">
        <template #default="{row}">
          <el-tag :type="row.status==='success'?'success':'danger'" size="small">[[ row.status==='success'?'成功':'失败' ]]</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="70" fixed="right">
        <template #default="{row}">
          <el-button link @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
    <div style="display:flex;justify-content:center;margin-top:12px">
      <el-pagination
        :total="total"
        :current-page="page"
        :page-size="perPage"
        :page-sizes="[20,30,50,100]"
        layout="total, sizes, prev, pager, next, jumper"
        @size-change="onSizeChange"
        @current-change="onPageChange"
      />
    </div>
  </div>

  <!-- 日志详情弹窗 -->
  <el-dialog v-model="detailVisible" title="请求详情" width="90vw" align-center top="0" :close-on-click-modal="false" class="is-log-detail" @close="onDetailClose">
    <div class="log-detail-inner">
      <!-- 错误码映射提示 -->
      <div v-if="detailRow && detailRow.original_status_code && detailRow.mapped_status_code && detailRow.original_status_code!==detailRow.mapped_status_code"
           class="log-detail-hint log-detail-hint--warn">
        <strong>错误码映射:</strong> [[ detailRow.original_status_code ]] &rarr; [[ detailRow.mapped_status_code ]]（Claude Code 收到 [[ detailRow.mapped_status_code ]]，日志记录原始 [[ detailRow.original_status_code ]]）
      </div>
      <div v-if="detailRow && detailRow.error_msg" class="log-detail-hint log-detail-hint--error">
        <strong>错误:</strong> [[ esc(detailRow.error_msg) ]]
      </div>

      <!-- 请求/响应体：左右并排对比 -->
      <div v-if="detailRow && (detailRow.request_body || detailRow.response_body)" class="detail-columns">
        <div class="detail-col">
          <div class="detail-col-label">请求体</div>
          <div v-if="detailRow.request_body" :id="reqEditorId" class="json-editor-wrap"></div>
          <pre v-else class="log-detail custom-scroll">（空）</pre>
        </div>
        <div class="detail-col">
          <div class="detail-col-label">响应体</div>
          <div v-if="detailRow.response_body" :id="respEditorId" class="json-editor-wrap"></div>
          <pre v-else class="log-detail custom-scroll">（空）</pre>
        </div>
      </div>
      <div v-else-if="detailRow" class="log-detail-empty">无请求/响应体</div>
    </div>
    <template #footer>
      <el-button @click="detailVisible=false">关闭</el-button>
    </template>
  </el-dialog>

  <!-- 定时清除弹窗 -->
  <el-dialog v-model="cleanupVisible" title="定时清除设置" width="480px" :close-on-click-modal="false" @open="loadCleanup">
    <el-form label-width="110px">
      <el-form-item label="启用自动清理">
        <el-switch v-model="cleanupEnabled" />
      </el-form-item>
      <el-form-item label="保留天数">
        <el-input-number v-model="cleanupDays" :min="1" :max="365" :controls="false" style="width:100px" />
        <span style="font-size:12px;color:var(--text2);margin-left:8px">天</span>
      </el-form-item>
      <el-form-item label="检查间隔">
        <el-input-number v-model="cleanupInterval" :min="1" :max="168" :controls="false" style="width:100px" />
        <span style="font-size:12px;color:var(--text2);margin-left:8px">小时</span>
      </el-form-item>
    </el-form>
    <div v-if="cleanupInfo" style="font-size:12px;color:var(--text2);margin-top:8px;padding:0 0 0 110px">[[ cleanupInfo ]]</div>
    <template #footer>
      <el-button @click="cleanupVisible=false">取消</el-button>
      <el-button type="primary" :loading="cleanupSaving" @click="saveCleanup">保存</el-button>
    </template>
  </el-dialog>
</div>
`;

  const LogsApp = Vue.createApp({
    delimiters: ['[[', ']]'],
    template: LogsTemplate,
    data(){
      return {
        // 日志列表
        rows: [],
        total: 0,
        page: 1,
        perPage: 30,
        loading: false,
        // 筛选
        filterIp: '',
        filterProvider: '',
        filterModel: '',
        filterStatus: '',
        providerOptions: [],
        // debounce
        _debounceTimer: null,
        // 详情弹窗
        detailVisible: false,
        detailRow: null,
        _jsonEditors: [],
        reqEditorId: 'log-req-editor',
        respEditorId: 'log-resp-editor',
        // 自动清理
        cleanupEnabled: false,
        cleanupDays: 7,
        cleanupInterval: 1,
        cleanupInfo: '',
        cleanupVisible: false,
        cleanupSaving: false,
        // 表格高度
        tableMaxHeight: 500,
      }
    },
    methods: {
      // ---- 筛选 debounce ----
      debounceLoad(){
        if(this._debounceTimer) clearTimeout(this._debounceTimer);
        this._debounceTimer = setTimeout(()=>{ this.load(1); }, 300);
      },

      // ---- 加载日志列表 ----
      async load(p){
        if(p) this.page = p;
        this.loading = true;
        try {
          const params = new URLSearchParams({ page: this.page, per_page: this.perPage });
          if(this.filterIp) params.set('ip', this.filterIp);
          if(this.filterProvider) params.set('provider', this.filterProvider);
          if(this.filterModel) params.set('model', this.filterModel);
          if(this.filterStatus) params.set('status', this.filterStatus);
          const r = await fetch(API+'/api/logs?'+params);
          const data = await r.json();
          this.rows = data.logs || [];
          this.total = data.total || 0;
        } catch(e) {
          ElementPlus.ElMessage.error('加载日志失败');
        } finally {
          this.loading = false;
        }
      },

      // ---- 加载提供商选项 ----
      async loadProviders(){
        try {
          const r = await fetch(API+'/api/logs/providers');
          this.providerOptions = await r.json();
        } catch(e) {
          this.providerOptions = [];
        }
      },

      // ---- 分页 ----
      onSizeChange(size){
        this.perPage = size;
        this.load(1);
      },
      onPageChange(p){
        this.load(p);
      },

      // ---- 清空日志 ----
      async clearAll(){
        try {
          await ElementPlus.ElMessageBox.confirm(
            '确定清空所有日志？此操作不可恢复。',
            '提示',
            { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' }
          );
        } catch(e){ return; }
        try {
          await fetch(API+'/api/logs', { method: 'DELETE' });
          ElementPlus.ElMessage.success('已清空');
          this.load(1);
          this.loadCleanup();
        } catch(e) {
          ElementPlus.ElMessage.error('清空失败');
        }
      },

      // ---- 日志详情 ----
      async openDetail(row){
        // 先销毁旧实例，防止累积
        this.destroyEditors();
        // 列表行只含元数据（不含 request_body/response_body/error_msg），先占位打开弹窗
        this.detailRow = row;
        this.detailVisible = true;
        // 按需拉取完整详情（含正文），再渲染 jsoneditor
        try {
          const r = await fetch(API+'/api/logs/'+row.id);
          if(r.ok){
            this.detailRow = await r.json();
          }
        } catch(e) { /* 失败则保留列表行元数据，弹窗以空正文展示 */ }
        // 等 DOM 渲染后创建 jsoneditor
        this.$nextTick(()=>{
          const d = this.detailRow || {};
          this.createEditor(this.reqEditorId, d.request_body);
          this.createEditor(this.respEditorId, d.response_body);
        });
      },
      onDetailClose(){
        this.destroyEditors();
        this.detailRow = null;
      },
      destroyEditors(){
        this._jsonEditors.forEach(ed=>{ try{ ed.destroy(); }catch(e){} });
        this._jsonEditors = [];
      },
      createEditor(containerId, bodyStr){
        if(!bodyStr) return;
        const wrap = document.getElementById(containerId);
        if(!wrap) return;
        // 清空容器
        wrap.innerHTML = '';

        // 尝试直接解析 JSON
        let jsonObj = null;
        try { jsonObj = JSON.parse(bodyStr); } catch(e) {}

        // 非 JSON，尝试解析 SSE 流式响应
        if(!jsonObj){
          const sseParsed = this.parseSSE(bodyStr);
          if(sseParsed) jsonObj = sseParsed;
        }

        if(jsonObj){
          try {
            const ed = new JSONEditor(wrap, { mode: 'code', mainMenuBar: false, navigationBar: false, statusBar: false }, jsonObj);
            this._jsonEditors.push(ed);
            setTimeout(()=>{ if(ed.aceEditor) ed.aceEditor.resize(); }, 100);
            return;
          } catch(e) { /* 回退到 pre */ }
        }

        // 都解析失败，回退到 pre 显示原文
        const pre = document.createElement('pre');
        pre.className = 'log-detail custom-scroll';
        pre.textContent = bodyStr;
        wrap.appendChild(pre);
      },

      // ---- parseSSE：完整搬入组件 ----
      parseSSE(str){
        // 检测是否为 SSE 格式（包含 event: 或 data: 行）
        if(!/^(event:|data:)\s/m.test(str)) return null;
        const events = [];
        let cur = {};
        for(const line of str.split('\n')){
          const t = line.trim();
          if(!t){ if(cur.event||cur.data){ events.push(cur); cur={}; } continue; }
          if(t.startsWith('event:')){
            // 遇到新 event 行时，先 push 上一个 event 块
            // 后端存 SSE 时可能去掉空行分隔符，不能仅依赖空行来分隔
            if(cur.event || cur.data){ events.push(cur); cur = {}; }
            cur.event = t.slice(6).trim();
          }
          else if(t.startsWith('data:')){
            const raw = t.slice(5).trim();
            // data_raw 记录原始 data 行
            cur.data_raw = (cur.data_raw || '') + (cur.data_raw ? '' : '') + raw;
            if(raw==='[DONE]'){ cur.data='[DONE]'; }
            else{ try{ cur.data=JSON.parse(raw); }catch{ cur.data=raw; } }
          }
        }
        if(cur.event||cur.data) events.push(cur);

        // 合并为可读结构
        const result = {};
        const message_start = []; const content_blocks = []; const content_deltas = []; const message_deltas = [];
        let usage = null; let message = null;
        for(const e of events){
          if(e.event==='message_start' && e.data && typeof e.data==='object'){
            message_start.push(e.data);
            if(e.data.message) message = e.data.message;
          } else if(e.event==='content_block_start' && e.data && typeof e.data==='object'){
            // 记录 content block 元信息（index/type/name/id），后续用它预占 message.content[idx]
            content_blocks.push(e.data);
          } else if(e.event==='content_block_delta' && e.data && typeof e.data==='object'){
            content_deltas.push(e.data);
          } else if(e.event==='content_block_stop' && e.data){
            // skip
          } else if(e.event==='message_delta' && e.data && typeof e.data==='object'){
            message_deltas.push(e.data);
            if(e.data.usage) usage = e.data.usage;
          } else if(e.event==='message_stop' && e.data){
            // skip
          } else if(e.event==='ping'){
            // skip
          } else {
            if(!result._other) result._other = [];
            result._other.push(e);
          }
        }
        // 构建合并后的响应
        if(message){
          const merged = JSON.parse(JSON.stringify(message));
          // 用 content_blocks 预填充 message.content 占位（保留 type/name/id 等元信息）
          for(const blk of content_blocks){
            const idx = blk.index != null ? blk.index : 0;
            while(merged.content.length <= idx) merged.content.push(null);
            if(blk.content_block){
              merged.content[idx] = JSON.parse(JSON.stringify(blk.content_block));
              // input_json_delta 累积前先把 input 置空字符串
              if(merged.content[idx].type === 'tool_use') merged.content[idx].input = '';
            }
          }
          if(content_deltas.length > 0){
            for(const delta of content_deltas){
              const idx = delta.index||0;
              const d = delta.delta;
              if(!merged.content[idx]) merged.content[idx] = { type: 'text' };
              if(d){
                if(d.type==='text_delta' && d.text) merged.content[idx].text = (merged.content[idx].text||'') + d.text;
                else if(d.type==='input_json_delta' && d.partial_json){
                  merged.content[idx].input = (merged.content[idx].input||'') + d.partial_json;
                }
                else Object.assign(merged.content[idx], d);
              }
            }
          }
          // tool_use 的 input 拼完后尝试 JSON.parse 回对象
          for(const c of merged.content){
            if(c && c.type==='tool_use' && typeof c.input==='string' && c.input){
              try{ c.input = JSON.parse(c.input); }catch{ /* 保留为字符串 */ }
            }
          }
          result.message = merged;
        }
        if(usage) result.usage = usage;
        if(message_deltas.length>0) result.message_delta = message_deltas;
        if(message_start.length>0) result.message_start = message_start;
        // 如果只有 content_deltas 没有完整 message，展示合并的 delta 列表
        if(!message && content_deltas.length>0){
          const texts = [];
          for(const d of content_deltas){
            if(d.delta && d.delta.text) texts.push(d.delta.text);
          }
          result.merged_text = texts.join('');
          result.content_deltas = content_deltas;
        }
        return Object.keys(result).length>0 ? result : null;
      },

      // ---- 自动清理 ----
      async loadCleanup(){
        try {
          const [settings, stats] = await Promise.all([
            fetch(API+'/api/settings').then(r=>r.json()),
            fetch(API+'/api/logs/stats').then(r=>r.json()),
          ]);
          this.cleanupEnabled = settings.auto_cleanup_enabled==='1';
          this.cleanupDays = parseInt(settings.cleanup_retention_days)||7;
          this.cleanupInterval = parseInt(settings.cleanup_interval_hours)||1;
          let info = '';
          if(settings.last_cleanup_time) info += '上次清理: ' + settings.last_cleanup_time;
          if(stats.total>0) info += (info?' | ':'') + '共 ' + stats.total + ' 条日志';
          this.cleanupInfo = info;
        } catch(e) { /* 静默 */ }
      },
      openCleanup(){
        this.cleanupVisible = true;
      },
      async saveCleanup(){
        this.cleanupSaving = true;
        try {
          await fetch(API+'/api/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              auto_cleanup_enabled: this.cleanupEnabled?'1':'0',
              cleanup_retention_days: this.cleanupDays,
              cleanup_interval_hours: this.cleanupInterval,
            }),
          });
          ElementPlus.ElMessage.success('已保存');
          this.cleanupVisible = false;
          this.loadCleanup();
        } catch(e) {
          ElementPlus.ElMessage.error('保存失败');
        } finally {
          this.cleanupSaving = false;
        }
      },

      // ---- 表格高度自适应 ----
      resizeTable(){
        this.$nextTick(()=>{
          const el = document.querySelector('#vue-logs .el-table');
          if(!el || el.offsetWidth===0) return;
          // el.top 已含 header/tabs/card-header/筛选框高度，无需再扣
          // 只扣表格下方：分页器(含 margin-top) + 卡片/内容 padding/margin + 安全余量
          const pager = document.querySelector('#vue-logs .el-pagination');
          let below = 16; // 安全余量
          if(pager) below += pager.getBoundingClientRect().height + 12; // 分页器高 + 其 margin-top:12px
          below += 60; // card padding-bottom(20) + card margin-bottom(16) + content padding-bottom(24)
          this.tableMaxHeight = Math.max(240, window.innerHeight - el.getBoundingClientRect().top - below);
        });
      },
      // ---- 详情弹窗内 jsoneditor 跟随视口重排 ----
      resizeEditors(){
        this._jsonEditors.forEach(ed=>{ try{ if(ed.aceEditor) ed.aceEditor.resize(); }catch(e){} });
      },
    },
    mounted(){
      this.loadProviders();
      this.load(1);
      this.loadCleanup();
      // 表格高度按视口自适应
      this.resizeTable();
      window.addEventListener('resize', this.resizeTable);
      window.addEventListener('resize', this.resizeEditors);
      // 跨面板桥接：供 switchTab 切到 logs 时刷新
      window.__reloadLogs = ()=>{ this.resizeTable(); this.load(); this.loadProviders(); this.loadCleanup(); };
    },
    beforeUnmount(){
      if(this._debounceTimer){ clearTimeout(this._debounceTimer); this._debounceTimer = null; }
      this.destroyEditors();
      window.removeEventListener('resize', this.resizeTable);
      window.removeEventListener('resize', this.resizeEditors);
      if(window.__reloadLogs) delete window.__reloadLogs;
    },
  });

  LogsApp.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  if (window.ElementPlusIconsVue) {
    for (const [k, v] of Object.entries(ElementPlusIconsVue)) LogsApp.component(k, v);
  }
  LogsApp.config.globalProperties.esc = esc;
  LogsApp.config.globalProperties.formatTime = formatTime;
  LogsApp.config.globalProperties.formatTokens = formatTokens;
  LogsApp.mount('#vue-logs');
}
