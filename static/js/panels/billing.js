// ---- Vue 岛屿：计费管理面板（阶段 5）----
if (window.Vue && window.ElementPlus) {
  const BillingTemplate = `
<div v-if="!rows.length && !loading">
  <el-empty description="暂无提供商" />
</div>
<div v-else>
  <div class="card">
    <div class="card-header">
      <h2>计费概览</h2>
      <el-button type="primary" @click="load">刷新</el-button>
    </div>
    <div class="billing-summary" v-if="rows.length">
      <div class="billing-stat"><div class="val">[[ rows.length ]]</div><div class="lbl">提供商总数</div></div>
      <div class="billing-stat"><div class="val" style="color:var(--primary)">[[ rows.filter(r=>r.has_billing).length ]]</div><div class="lbl">已配置计费</div></div>
      <div class="billing-stat"><div class="val" style="color:var(--warn)">[[ rows.filter(r=>r.near_limit).length ]]</div><div class="lbl">接近限额</div></div>
      <div class="billing-stat"><div class="val" style="color:var(--danger)">[[ rows.filter(r=>!r.allowed).length ]]</div><div class="lbl">已超限</div></div>
    </div>
    <div class="billing-cards" v-if="rows.length">
      <div v-for="row in rows" :key="row.provider_id" class="usage-card">
        <div class="usage-card-header">
          <h4>[[ esc(row.provider_name) ]]</h4>
          <el-tag :type="tagType(mode(row))" size="small">[[ billingModeLabel(mode(row)) ]]</el-tag>
        </div>
        <div v-if="mode(row)==='request_count' || mode(row)==='token_count'">
          <div v-for="w in windows" :key="w.key" v-show="showLimit(row,w.key)" class="usage-row">
            <span class="usage-label">[[ w.label ]]</span>
            <div class="usage-bar">
              <div class="usage-bar-fill bar-fill" :class="barCls(usagePct(row,w.key))" :style="{width:usagePct(row,w.key)+'%'}"></div>
            </div>
            <span class="usage-val">[[ usageText(row,w.key) ]]</span>
          </div>
          <div v-if="!hasAnyLimit(row)" style="font-size:12px;color:var(--text2)">暂无使用数据</div>
        </div>
        <div v-else-if="mode(row)==='balance'" class="usage-row">
          <span class="usage-label">余额</span>
          <div class="usage-bar">
            <div class="usage-bar-fill bar-fill" :class="balanceCls(row)" :style="{width:balancePct(row)+'%'}"></div>
          </div>
          <span class="usage-val">¥[[ num2(row.billing_config.balance) ]]</span>
        </div>
        <div v-else style="font-size:12px;color:var(--text2)">暂无使用数据</div>
        <div style="margin-top:8px;font-size:12px">
          <el-tag :type="statusType(row)" size="small">[[ statusLabel(row) ]]</el-tag>
          <span v-if="row.billing_config && row.billing_config.expiration_date" style="margin-left:8px;color:var(--text2)">到期: [[ row.billing_config.expiration_date ]]</span>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header"><h2>提供商计费详情</h2></div>
    <el-table :data="rows" v-loading="loading" style="width:100%" :max-height="tableMaxHeight" :row-class-name="rowCls" empty-text="暂无计费数据">
      <el-table-column label="提供商" min-width="140"><template #default="{row}"><span>[[ esc(row.provider_name) ]]</span><el-tag v-if="!row.enabled" type="info" size="small" style="margin-left:6px">已禁用</el-tag></template></el-table-column>
      <el-table-column label="计费模式" width="120">
        <template #default="{row}">
          <el-tag :type="tagType(mode(row))" size="small">[[ billingModeLabel(mode(row)) ]]</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="5小时" width="180">
        <template #default="{row}">
          <div v-if="showLimit(row,'5h')" style="display:flex;align-items:center;gap:6px;min-width:140px">
            <div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden;min-width:40px">
              <div class="bar-fill" :class="barCls(usagePct(row,'5h'))" :style="{width:usagePct(row,'5h')+'%'}"></div>
            </div>
            <span style="font-size:11px;color:var(--text2);white-space:nowrap">[[ usageText(row,'5h') ]]</span>
          </div>
          <span v-else style="color:var(--text2)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="一周" width="180">
        <template #default="{row}">
          <div v-if="showLimit(row,'week')" style="display:flex;align-items:center;gap:6px;min-width:140px">
            <div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden;min-width:40px">
              <div class="bar-fill" :class="barCls(usagePct(row,'week'))" :style="{width:usagePct(row,'week')+'%'}"></div>
            </div>
            <span style="font-size:11px;color:var(--text2);white-space:nowrap">[[ usageText(row,'week') ]]</span>
          </div>
          <span v-else style="color:var(--text2)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="一月" width="180">
        <template #default="{row}">
          <div v-if="showLimit(row,'month')" style="display:flex;align-items:center;gap:6px;min-width:140px">
            <div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden;min-width:40px">
              <div class="bar-fill" :class="barCls(usagePct(row,'month'))" :style="{width:usagePct(row,'month')+'%'}"></div>
            </div>
            <span style="font-size:11px;color:var(--text2);white-space:nowrap">[[ usageText(row,'month') ]]</span>
          </div>
          <span v-else style="color:var(--text2)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="余额" width="120">
        <template #default="{row}">
          <span v-if="mode(row)==='balance'" style="font-family:monospace;font-size:13px">¥[[ num2(row.billing_config.balance) ]]</span>
          <span v-else style="color:var(--text2)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="到期时间" width="140">
        <template #default="{row}">
          <span v-if="!row.billing_config || !row.billing_config.expiration_date" style="color:var(--text2)">-</span>
          <el-tag v-else-if="isExpired(row.billing_config.expiration_date)" type="danger" size="small">已过期</el-tag>
          <span v-else style="font-size:12px">[[ row.billing_config.expiration_date ]]</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{row}">
          <el-tag :type="statusType(row)" size="small">[[ statusLabel(row) ]]</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{row}">
          <el-button link @click="openDrawer(row.provider_id)">计费设置</el-button>
          <el-button v-if="row.has_billing" link type="danger" @click="delBilling(row.provider_id)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>

  <el-drawer v-model="drawerVisible" :title="drawerTitle" size="420px" :close-on-click-modal="false" destroy-on-close>
    <el-form v-loading="drawerLoading" label-width="110px">
      <el-form-item label="计费模式">
        <el-select v-model="form.billing_mode" style="width:100%">
          <el-option label="不限（无限制）" value="none" />
          <el-option label="请求次数限制" value="request_count" />
          <el-option label="Token数量限制" value="token_count" />
          <el-option label="余额计费" value="balance" />
        </el-select>
      </el-form-item>
      <template v-if="form.billing_mode==='request_count' || form.billing_mode==='token_count'">
        <el-form-item label="5小时限制">
          <el-input-number v-model="form.limit_5h" :min="0" :controls="false" style="width:140px" placeholder="不限" />
          <span style="font-size:12px;color:var(--text2);margin-left:8px">留空=不限，0=禁止</span>
        </el-form-item>
        <el-form-item label="一周限制">
          <el-input-number v-model="form.limit_week" :min="0" :controls="false" style="width:140px" placeholder="不限" />
        </el-form-item>
        <el-form-item label="一月限制">
          <el-input-number v-model="form.limit_month" :min="0" :controls="false" style="width:140px" placeholder="不限" />
        </el-form-item>
      </template>
      <template v-if="form.billing_mode==='balance'">
        <el-form-item label="余额">
          <el-input-number v-model="form.balance" :min="0" :precision="2" :step="0.01" :controls="false" style="width:140px" />
        </el-form-item>
        <el-form-item label="输入价格">
          <el-input-number v-model="form.input_price_per_million" :min="0" :precision="2" :step="0.01" :controls="false" style="width:140px" />
          <span style="font-size:12px;color:var(--text2);margin-left:8px">/ 百万 Token</span>
        </el-form-item>
        <el-form-item label="输出价格">
          <el-input-number v-model="form.output_price_per_million" :min="0" :precision="2" :step="0.01" :controls="false" style="width:140px" />
          <span style="font-size:12px;color:var(--text2);margin-left:8px">/ 百万 Token</span>
        </el-form-item>
        <el-form-item label="缓存命中价格">
          <el-input-number v-model="form.cache_read_price_per_million" :min="0" :precision="2" :step="0.01" :controls="false" style="width:140px" />
          <span style="font-size:12px;color:var(--text2);margin-left:8px">留空 = 输入价格的 10%</span>
        </el-form-item>
      </template>
      <template v-if="form.billing_mode!=='none'">
        <el-form-item label="到期时间">
          <el-date-picker v-model="form.expiration_date" type="date" value-format="YYYY-MM-DD" placeholder="留空 = 无期限" style="width:100%" />
        </el-form-item>
        <el-form-item label="警告阈值">
          <el-slider v-model="form.warning_threshold" :min="50" :max="100" :format-tooltip="sliderTip" />
          <div style="font-size:12px;color:var(--text2)">[[ form.warning_threshold ]]%</div>
        </el-form-item>
      </template>
    </el-form>
    <template #footer>
      <el-button @click="drawerVisible=false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="saveDrawer">保存</el-button>
    </template>
  </el-drawer>
</div>
`;

  const BillingApp = Vue.createApp({
    delimiters: ['[[', ']]'],
    template: BillingTemplate,
    data(){
      return {
        rows: [],
        providers: [],
        windows: [{key:'5h',label:'5小时'},{key:'week',label:'一周'},{key:'month',label:'一月'}],
        loading: false,
        tableMaxHeight: 500,
        drawerVisible: false,
        drawerLoading: false,
        drawerPid: null,
        drawerTitle: '计费设置',
        hasBilling: false,
        submitting: false,
        form: {
          billing_mode: 'none',
          limit_5h: null,
          limit_week: null,
          limit_month: null,
          balance: null,
          input_price_per_million: null,
          output_price_per_million: null,
          cache_read_price_per_million: null,
          expiration_date: '',
          warning_threshold: 80,
        },
      }
    },
    methods: {
      async load(){
        this.loading = true;
        try {
          const [overview, providers] = await Promise.all([
            fetch(API+'/api/providers/billing/overview').then(r=>r.json()),
            fetch(API+'/api/providers').then(r=>r.json()).catch(()=>[]),
          ]);
          this.rows = overview || [];
          this.providers = providers || [];
        } catch(e) {
          ElementPlus.ElMessage.error('加载计费概览失败');
        } finally {
          this.loading = false;
        }
      },
      rowCls({row}){ return row.enabled ? '' : 'row-disabled'; },
      resizeTable(){
        this.$nextTick(()=>{
          const el = document.querySelector('#vue-billing .el-table');
          if(!el || el.offsetWidth===0) return;
          this.tableMaxHeight = Math.max(240, window.innerHeight - el.getBoundingClientRect().top - 68);
        });
      },
      mode(row){ return row.billing_config ? row.billing_config.billing_mode : 'none'; },
      tagType(mode){
        return {none:'info', request_count:'warning', token_count:'success', balance:'primary'}[mode] || 'info';
      },
      barCls(pct){
        return pct>=95 ? 'full' : pct>=80 ? 'danger' : pct>=60 ? 'warn' : 'ok';
      },
      usageOf(row, windowType){
        return (row.usage || []).find(u => u.window_type === windowType);
      },
      showLimit(row, windowType){
        const c = row.billing_config;
        if(!c || (c.billing_mode!=='request_count' && c.billing_mode!=='token_count')) return false;
        const limit = c['limit_'+windowType];
        return limit && limit > 0;
      },
      hasAnyLimit(row){
        return this.showLimit(row,'5h') || this.showLimit(row,'week') || this.showLimit(row,'month');
      },
      usagePct(row, windowType){
        if(!this.showLimit(row, windowType)) return 0;
        const c = row.billing_config;
        const limit = c['limit_'+windowType];
        const u = this.usageOf(row, windowType);
        const used = c.billing_mode==='request_count' ? (u ? u.request_count : 0) : (u ? (u.input_tokens + u.output_tokens) : 0);
        return Math.min(100, Math.round(used / limit * 100));
      },
      usageText(row, windowType){
        const c = row.billing_config;
        const limit = c['limit_'+windowType];
        const u = this.usageOf(row, windowType);
        const used = c.billing_mode==='request_count' ? (u ? u.request_count : 0) : (u ? (u.input_tokens + u.output_tokens) : 0);
        const pct = this.usagePct(row, windowType);
        if(c.billing_mode==='request_count') return `${used}次/${limit}次 (${pct}%)`;
        return `${this.formatTokens(used)}/${this.formatTokens(limit)} (${pct}%)`;
      },
      balanceCls(row){
        const bal = row.billing_config ? row.billing_config.balance : 0;
        return bal <= 0 ? 'full' : 'ok';
      },
      balancePct(row){
        const bal = row.billing_config ? row.billing_config.balance : 0;
        return bal > 0 ? 100 : 0;
      },
      num2(v){ return (v == null ? 0 : parseFloat(v)).toFixed(2); },
      statusType(row){
        if(!row.has_billing) return 'success';
        if(!row.allowed) return 'danger';
        if(row.near_limit) return 'warning';
        return 'success';
      },
      statusLabel(row){
        if(!row.has_billing) return '不限';
        if(!row.allowed) return '已超限';
        if(row.near_limit) return '接近限额';
        return '正常';
      },
      isExpired(dateStr){
        if(!dateStr) return false;
        try { return new Date(dateStr) < new Date(); } catch(e){ return false; }
      },
      sliderTip(v){ return v+'%'; },
      async openDrawer(pid){
        this.drawerPid = pid;
        this.drawerVisible = true;
        this.drawerLoading = true;
        this.hasBilling = false;
        this.form = {
          billing_mode: 'none',
          limit_5h: null,
          limit_week: null,
          limit_month: null,
          balance: null,
          input_price_per_million: null,
          output_price_per_million: null,
          cache_read_price_per_million: null,
          expiration_date: '',
          warning_threshold: 80,
        };
        const p = this.providers.find(x => x.id === pid);
        this.drawerTitle = (p ? p.name : '提供商') + ' - 计费设置';
        try {
          const r = await fetch(API+'/api/providers/'+pid+'/billing');
          if(r.ok){
            const b = await r.json();
            this.hasBilling = true;
            this.form = {
              billing_mode: b.billing_mode || 'none',
              limit_5h: b.limit_5h != null ? b.limit_5h : null,
              limit_week: b.limit_week != null ? b.limit_week : null,
              limit_month: b.limit_month != null ? b.limit_month : null,
              balance: b.balance != null ? b.balance : null,
              input_price_per_million: b.input_price_per_million != null ? b.input_price_per_million : null,
              output_price_per_million: b.output_price_per_million != null ? b.output_price_per_million : null,
              cache_read_price_per_million: b.cache_read_price_per_million != null ? b.cache_read_price_per_million : null,
              expiration_date: b.expiration_date || '',
              warning_threshold: b.warning_threshold != null ? Math.round(b.warning_threshold * 100) : 80,
            };
          }
        } catch(e) {
          ElementPlus.ElMessage.error('加载计费配置失败');
        } finally {
          this.drawerLoading = false;
        }
      },
      async saveDrawer(){
        if(!this.drawerPid) return;
        this.submitting = true;
        try {
          if(this.form.billing_mode === 'none'){
            if(this.hasBilling){
              const r = await fetch(API+'/api/providers/'+this.drawerPid+'/billing', {method:'DELETE'});
              if(!r.ok){
                const j = await r.json().catch(()=>({}));
                ElementPlus.ElMessage.error(j.error || '删除失败');
                return;
              }
            }
            this.drawerVisible = false;
            this.load();
            window.__reloadProviders?.();
            return;
          }
          const body = {
            billing_mode: this.form.billing_mode,
            warning_threshold: (this.form.warning_threshold || 80) / 100,
            expiration_date: this.form.expiration_date || null,
          };
          const m = this.form.billing_mode;
          if(m==='request_count' || m==='token_count'){
            body.limit_5h = (this.form.limit_5h === '' || this.form.limit_5h == null) ? null : parseInt(this.form.limit_5h);
            body.limit_week = (this.form.limit_week === '' || this.form.limit_week == null) ? null : parseInt(this.form.limit_week);
            body.limit_month = (this.form.limit_month === '' || this.form.limit_month == null) ? null : parseInt(this.form.limit_month);
          }
          if(m==='balance'){
            body.balance = (this.form.balance == null || this.form.balance === '') ? null : (parseFloat(this.form.balance) || 0);
            body.input_price_per_million = (this.form.input_price_per_million == null || this.form.input_price_per_million === '') ? null : (parseFloat(this.form.input_price_per_million) || 0);
            body.output_price_per_million = (this.form.output_price_per_million == null || this.form.output_price_per_million === '') ? null : (parseFloat(this.form.output_price_per_million) || 0);
            if(this.form.cache_read_price_per_million != null && this.form.cache_read_price_per_million !== ''){
              body.cache_read_price_per_million = parseFloat(this.form.cache_read_price_per_million) || 0;
            }
          }
          const method = this.hasBilling ? 'PUT' : 'POST';
          const r = await fetch(API+'/api/providers/'+this.drawerPid+'/billing', {
            method,
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify(body)
          });
          if(!r.ok){
            const j = await r.json().catch(()=>({}));
            ElementPlus.ElMessage.error(j.error || '保存失败');
            return;
          }
          this.drawerVisible = false;
          this.load();
          window.__reloadProviders?.();
        } catch(e) {
          ElementPlus.ElMessage.error('保存失败');
        } finally {
          this.submitting = false;
        }
      },
      async delBilling(pid){
        try {
          await ElementPlus.ElMessageBox.confirm(
            '确定删除该提供商的计费配置？',
            '提示',
            { type:'warning', confirmButtonText:'删除', cancelButtonText:'取消' }
          );
        } catch(e){ return; }
        try {
          const r = await fetch(API+'/api/providers/'+pid+'/billing', {method:'DELETE'});
          if(!r.ok){
            const j = await r.json().catch(()=>({}));
            ElementPlus.ElMessage.error(j.error || '删除失败');
            return;
          }
          this.load();
          window.__reloadProviders?.();
        } catch(e) {
          ElementPlus.ElMessage.error('删除失败');
        }
      },
    },
    mounted(){
      this.load();
      this.resizeTable();
      window.addEventListener('resize', this.resizeTable);
      window.editProviderBilling = (pid) => this.openDrawer(pid);
      window.__reloadBilling = () => { this.resizeTable(); this.load(); };
    },
    beforeUnmount(){
      window.removeEventListener('resize', this.resizeTable);
      if(window.editProviderBilling) delete window.editProviderBilling;
      if(window.__reloadBilling) delete window.__reloadBilling;
    },
  });
  BillingApp.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  if (window.ElementPlusIconsVue) {
    for (const [k, v] of Object.entries(window.ElementPlusIconsVue)) BillingApp.component(k, v);
  }
  BillingApp.config.globalProperties.esc = esc;
  BillingApp.config.globalProperties.formatTime = formatTime;
  BillingApp.config.globalProperties.formatTokens = formatTokens;
  BillingApp.config.globalProperties.billingModeLabel = (m) => ({none:'不限',request_count:'请求次数',token_count:'Token数量',balance:'余额计费'}[m] || '不限');
  BillingApp.mount('#vue-billing');
}
