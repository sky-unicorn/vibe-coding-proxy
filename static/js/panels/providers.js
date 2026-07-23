// ---- Vue 岛屿：提供商管理面板（阶段 3）----
if (window.Vue && window.ElementPlus) {
  const ProvidersTemplate = `
<div class="card">
  <div class="card-header">
    <h2>API 提供商</h2>
    <el-button type="primary" @click="openCreate">添加提供商</el-button>
  </div>
  <p style="font-size:13px;color:var(--text2);margin-bottom:12px">管理上游大模型提供商（Anthropic / OpenAI 双协议）。每个提供商可配置并发上限与计费规则，系统按并发最低的提供商进行负载均衡。</p>

  <el-table :data="rows" v-loading="loading" style="width:100%" empty-text="暂无提供商，点击上方按钮添加">
    <el-table-column label="名称" min-width="140" show-overflow-tooltip><template #default="{row}"><strong>[[ esc(row.name) ]]</strong></template></el-table-column>
    <el-table-column label="Anthropic URL" min-width="200">
      <template #default="{row}">
        <el-tooltip v-if="row.anthropic_url" :content="row.anthropic_url" placement="top">
          <span style="display:inline-flex;align-items:center;gap:4px">
            <code style="font-size:12px">[[ truncateUrl(row.anthropic_url) ]]</code>
            <el-tag size="small" :type="row.full_path?'success':'warning'">[[ row.full_path?'完整':'拼接' ]]</el-tag>
          </span>
        </el-tooltip>
        <span v-else style="color:var(--text2)">-</span>
      </template>
    </el-table-column>
    <el-table-column label="OpenAI URL" min-width="200">
      <template #default="{row}">
        <el-tooltip v-if="row.openai_url" :content="row.openai_url" placement="top">
          <span style="display:inline-flex;align-items:center;gap:4px">
            <code style="font-size:12px">[[ truncateUrl(row.openai_url) ]]</code>
            <el-tag size="small" :type="row.full_path?'success':'warning'">[[ row.full_path?'完整':'拼接' ]]</el-tag>
          </span>
        </el-tooltip>
        <span v-else style="color:var(--text2)">-</span>
      </template>
    </el-table-column>
    <el-table-column label="API Key" min-width="140"><template #default="{row}"><code class="key-mask">[[ maskKey(row.api_key) ]]</code></template></el-table-column>
    <el-table-column label="并行" width="70" align="center">
      <template #default="{row}">
        <span v-if="row.max_concurrency>0">[[ row.max_concurrency ]]</span>
        <span v-else style="color:var(--text2)">-</span>
      </template>
    </el-table-column>
    <el-table-column label="并发" min-width="120">
      <template #default="{row}">
        <div class="concurrency-bar">
          <div class="bar"><div class="bar-fill" :class="concCls(row)" :style="{width:concPct(row)+'%'}"></div></div>
          <span class="label">[[ concLabel(row) ]]</span>
        </div>
      </template>
    </el-table-column>
    <el-table-column label="Token" min-width="90">
      <template #default="{row}">
        <code v-if="row._totalTokens>0" style="font-size:12px">[[ formatTokens(row._totalTokens) ]]</code>
        <span v-else style="color:var(--text2)">-</span>
      </template>
    </el-table-column>
    <el-table-column label="调用" width="70" align="center"><template #default="{row}"><code style="font-size:12px">[[ row._callCount||0 ]]</code></template></el-table-column>
    <el-table-column label="计费" min-width="90">
      <template #default="{row}">
        <el-tag v-if="row._billing" :type="billingTagType(row._billing.billing_mode)" size="small">[[ billingModeLabel(row._billing.billing_mode) ]]</el-tag>
        <el-tag v-else type="info" size="small">不限</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="状态" min-width="140">
      <template #default="{row}">
        <el-switch :model-value="!!row.enabled" @change="v=>toggle(row.id,v)" />
        <el-tag size="small" :type="row.enabled?'success':'danger'" style="margin-left:6px">[[ row.enabled?'启用':'禁用' ]]</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="操作" width="190" fixed="right">
      <template #default="{row}">
        <el-button link @click="openEdit(row)">编辑</el-button>
        <el-button link @click="editBilling(row.id)">计费</el-button>
        <el-button link type="danger" @click="del(row.id)">删除</el-button>
      </template>
    </el-table-column>
  </el-table>

  <!-- 创建/编辑弹窗 -->
  <el-dialog v-model="dialogVisible" :title="editingId?'编辑提供商':'添加提供商'" width="560px" :close-on-click-modal="false">
    <el-form label-width="110px">
      <el-form-item label="名称">
        <el-input v-model="form.name" placeholder="如: Anthropic Official" />
      </el-form-item>
      <el-form-item label="Anthropic">
        <el-input v-model="form.anthropic_url" placeholder="Anthropic URL" />
      </el-form-item>
      <el-form-item label="OpenAI">
        <el-input v-model="form.openai_url" placeholder="OpenAI URL" />
      </el-form-item>
      <el-form-item label="API Key">
        <el-input v-model="form.api_key" type="password" show-password placeholder="sk-ant-..." />
      </el-form-item>
      <el-form-item label="最大并行数">
        <el-input-number v-model="form.max_concurrency" :min="0" :max="100" :controls="false" style="width:140px" />
        <div style="font-size:11px;color:var(--text2);margin-top:2px">0 表示不限制并发，超过限制的请求将排队等待（不会断开连接）</div>
      </el-form-item>
      <el-form-item label="完整路径">
        <el-switch v-model="form.full_path" />
        <span style="font-size:13px;margin-left:8px">[[ form.full_path?'原样使用':'自动拼接路径' ]]</span>
        <div style="font-size:11px;color:var(--text2);margin-top:2px">关闭时URL路径自动拼接 /v1/messages(Anthropic) 与 /chat/completions(OpenAI)</div>
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="form.enabled" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible=false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</div>
`;

  const ProvidersApp = Vue.createApp({
    delimiters: ['[[', ']]'],
    template: ProvidersTemplate,
    data(){
      return {
        rows: [],
        concData: {},
        loading: false,
        dialogVisible: false,
        editingId: null,
        form: { name:'', anthropic_url:'', openai_url:'', api_key:'', max_concurrency:0, full_path:false, enabled:true },
        submitting: false,
        _concTimer: null,
      }
    },
    methods: {
      async load(){
        this.loading = true;
        try {
          const [providers, conc, stats, billingOv] = await Promise.all([
            fetch(API+'/api/providers').then(r=>r.json()),
            fetch(API+'/api/concurrency').then(r=>r.json()),
            fetch(API+'/api/providers/stats').then(r=>r.json()).catch(()=>[]),
            fetch(API+'/api/providers/billing/overview').then(r=>r.json()).catch(()=>[]),
          ]);
          this.concData = conc || {};
          const billingMap = {};
          (billingOv||[]).forEach(b=>{ if(b.has_billing) billingMap[b.provider_id]=b.billing_config; });
          const statsMap = {};
          (stats||[]).forEach(s=>{ statsMap[s.provider]=s; });
          this.rows = (providers||[]).map(p=>{
            const st = statsMap[p.name];
            const totalTokens = st ? (st.total_input_tokens+st.total_output_tokens) : 0;
            const callCount = st ? st.request_count : 0;
            return Object.assign({}, p, {
              _billing: billingMap[p.id]||null,
              _totalTokens: totalTokens,
              _callCount: callCount,
            });
          });
        } catch(e) {
          ElementPlus.ElMessage.error('加载提供商列表失败');
        } finally {
          this.loading = false;
        }
      },
      async pollConc(){
        try {
          const conc = await fetch(API+'/api/concurrency').then(r=>r.json());
          this.concData = conc || {};
        } catch(e) { /* 静默：轮询失败不打扰用户 */ }
      },
      // 并发条取值：优先用轮询数据，回退到 provider 自身的 max_concurrency
      concOf(row){
        return this.concData[row.id] || { used:0, max: row.max_concurrency||0 };
      },
      concPct(row){
        const c = this.concOf(row);
        const max = c.max || 0;
        if(!max || max<=0) return 0;
        return Math.min(100, Math.round(c.used/max*100));
      },
      concCls(row){
        const pct = this.concPct(row);
        return pct>=100?'full':pct>=70?'warn':'ok';
      },
      concLabel(row){
        const c = this.concOf(row);
        const max = c.max || 0;
        if(!max || max<=0) return c.used+'/∞';
        return c.used+'/'+max;
      },
      billingTagType(mode){
        return ({request_count:'warning', token_count:'success', balance:'primary', none:'info'})[mode]||'info';
      },
      openCreate(){
        this.editingId = null;
        this.form = { name:'', anthropic_url:'', openai_url:'', api_key:'', max_concurrency:0, full_path:false, enabled:true };
        this.dialogVisible = true;
      },
      openEdit(row){
        this.editingId = row.id;
        this.form = {
          name: row.name||'',
          anthropic_url: row.anthropic_url||'',
          openai_url: row.openai_url||'',
          api_key: row.api_key||'',
          max_concurrency: row.max_concurrency||0,
          full_path: row.full_path!=null?!!row.full_path:false,
          enabled: !!row.enabled,
        };
        this.dialogVisible = true;
      },
      async submit(){
        if(!this.form.name.trim()){
          ElementPlus.ElMessage.warning('请填写提供商名称');
          return;
        }
        this.submitting = true;
        try {
          const data = {
            name: this.form.name.trim(),
            anthropic_url: this.form.anthropic_url,
            openai_url: this.form.openai_url,
            api_key: this.form.api_key,
            max_concurrency: parseInt(this.form.max_concurrency)||0,
            full_path: this.form.full_path?1:0,
            enabled: this.form.enabled,
          };
          const url = this.editingId ? API+'/api/providers/'+this.editingId : API+'/api/providers';
          const method = this.editingId ? 'PUT' : 'POST';
          const r = await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
          if(!r.ok){
            const j = await r.json().catch(()=>({}));
            ElementPlus.ElMessage.error(j.error||(this.editingId?'保存失败':'创建失败'));
            return;
          }
          this.dialogVisible = false;
          this.load();
        } catch(e) {
          ElementPlus.ElMessage.error('保存失败');
        } finally {
          this.submitting = false;
        }
      },
      async del(id){
        try {
          await ElementPlus.ElMessageBox.confirm(
            '确定删除？关联的模型映射也会被删除。',
            '提示',
            { type:'warning', confirmButtonText:'删除', cancelButtonText:'取消' }
          );
        } catch(e){ return; }
        try {
          const r = await fetch(API+'/api/providers/'+id,{method:'DELETE'});
          if(!r.ok){
            const j = await r.json().catch(()=>({}));
            ElementPlus.ElMessage.error(j.error||'删除失败');
            return;
          }
          this.load();
        } catch(e) {
          ElementPlus.ElMessage.error('删除失败');
        }
      },
      async toggle(id, enabled){
        try {
          const r = await fetch(API+'/api/providers/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
          if(!r.ok){
            ElementPlus.ElMessage.error('切换状态失败');
            this.load();
          } else {
            this.load();
            // 跨面板触发：后端会级联更新该 provider 下所有 model_mappings 的 enabled，刷新模型列表保持一致
            window.loadModels?.();
            this.showEnabledToast(enabled);
          }
        } catch(e) {
          ElementPlus.ElMessage.error('切换状态失败');
          this.load();
        }
      },
      // 跨面板触发：打开计费抽屉（计费面板阶段 5 才迁移，当前 editProviderBilling 仍是全局函数）
      editBilling(id){
        window.editProviderBilling?.(id);
      },
      showEnabledToast(enabled){
        if(typeof window.showFeatureToast==='function'){
          window.showFeatureToast(enabled?'已启用提供商':'已停用提供商', enabled
            ? '该提供商下所有映射模型已同步置为<strong>启用</strong>。'
            : '该提供商下所有映射模型已同步置为<strong>停用</strong>。');
        }
      },
    },
    mounted(){
      this.load();
      // 并发轮询定时器迁入组件：仅当 providers 面板激活时拉取，3s 一次
      this._concTimer = setInterval(()=>{
        const panel = document.getElementById('panel-providers');
        if(panel && panel.classList.contains('active')){
          this.pollConc();
        }
      }, 3000);
      // 暴露全局入口，供 switchTab 切到 providers 时刷新（替代旧的全局 loadProviders）
      window.__reloadProviders = ()=>this.load();
    },
    beforeUnmount(){
      if(this._concTimer){ clearInterval(this._concTimer); this._concTimer = null; }
      if(window.__reloadProviders) delete window.__reloadProviders;
    },
  });
  ProvidersApp.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  if (window.ElementPlusIconsVue) {
    for (const [k, v] of Object.entries(ElementPlusIconsVue)) ProvidersApp.component(k, v);
  }
  ProvidersApp.config.globalProperties.esc = esc;
  ProvidersApp.config.globalProperties.maskKey = maskKey;
  ProvidersApp.config.globalProperties.truncateUrl = truncateUrl;
  ProvidersApp.config.globalProperties.formatTokens = formatTokens;
  ProvidersApp.config.globalProperties.billingModeLabel = billingModeLabel;
  ProvidersApp.mount('#vue-providers');
}
