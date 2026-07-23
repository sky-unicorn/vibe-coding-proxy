// ---- Vue 岛屿：错误码映射面板（阶段 2）----
if (window.Vue && window.ElementPlus) {
  const ErrorsTemplate = `
<div class="card">
  <div class="card-header">
    <h2>错误码映射</h2>
    <el-button type="primary" @click="openCreate">添加规则</el-button>
  </div>
  <p style="font-size:13px;color:var(--text2);margin-bottom:12px">将上游大模型返回的 HTTP 错误码映射为不同的错误码返回给 Claude Code。日志中记录原始错误码，Claude Code 收到映射后的错误码。可为每个提供商单独配置映射规则，留空表示全局规则。</p>

  <el-table :data="rows" v-loading="loading" style="width:100%" empty-text="暂无映射规则，点击上方按钮添加">
    <el-table-column label="提供商" min-width="120"><template #default="{row}"><el-tag>[[ row.provider || '全局' ]]</el-tag></template></el-table-column>
    <el-table-column label="原始错误码" min-width="120"><template #default="{row}"><el-tag type="danger" style="font-weight:700">[[ row.original_code ]]</el-tag></template></el-table-column>
    <el-table-column label="" width="50" align="center"><template #default>→</template></el-table-column>
    <el-table-column label="映射错误码" min-width="120"><template #default="{row}"><el-tag type="warning" style="font-weight:700">[[ row.mapped_code ]]</el-tag></template></el-table-column>
    <el-table-column label="状态" min-width="150">
      <template #default="{row}">
        <el-switch :model-value="!!row.enabled" @change="v=>toggle(row.id,v)" />
        <el-tag size="small" :type="row.enabled?'success':'danger'" style="margin-left:6px">[[ row.enabled?'启用':'禁用' ]]</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="操作" width="120">
      <template #default="{row}">
        <el-button type="primary" link @click="openEdit(row)">编辑</el-button>
        <el-button type="danger" link @click="del(row.id)">删除</el-button>
      </template>
    </el-table-column>
  </el-table>

  <!-- 创建/编辑弹窗 -->
  <el-dialog v-model="dialogVisible" :title="editingId?'编辑错误码映射':'添加错误码映射'" width="520px" :close-on-click-modal="false">
    <el-form label-width="84px">
      <el-form-item label="提供商">
        <el-select v-model="form.provider" placeholder="选择提供商" style="width:100%">
          <el-option label="全局" value="" />
          <el-option v-for="p in providers" :key="p.name" :label="p.name" :value="p.name" />
        </el-select>
        <div style="font-size:11px;color:var(--text2);margin-top:2px">选择具体提供商单独配置，或选"全局"作为通用规则</div>
      </el-form-item>
      <el-form-item label="错误码">
        <div style="display:flex;align-items:center;gap:10px;width:100%">
          <el-input-number v-model="form.original_code" :min="100" :max="599" :controls="false" placeholder="原始" style="width:100%" />
          <span style="color:var(--text2)">→</span>
          <el-input-number v-model="form.mapped_code" :min="100" :max="599" :controls="false" placeholder="映射" style="width:100%" />
        </div>
        <div style="font-size:11px;color:var(--text2);margin-top:2px">左侧为上游返回的原始错误码，右侧为返回给调用方的映射码（范围 100-599）</div>
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

  const ErrorsApp = Vue.createApp({
    delimiters: ['[[', ']]'],
    template: ErrorsTemplate,
    data(){
      return {
        rows: [],
        providers: [],
        loading: false,
        dialogVisible: false,
        editingId: null,
        form: { provider: '', original_code: null, mapped_code: null, enabled: true },
        submitting: false,
      }
    },
    methods: {
      async load(){
        this.loading = true;
        try {
          const r = await fetch(API+'/api/error-mappings');
          this.rows = await r.json();
        } catch(e) {
          ElementPlus.ElMessage.error('加载错误码映射列表失败');
        } finally {
          this.loading = false;
        }
      },
      async loadProviders(){
        try {
          const r = await fetch(API+'/api/providers');
          const all = await r.json();
          this.providers = all.filter(p=>p.enabled);
        } catch(e) {
          this.providers = [];
        }
      },
      async openCreate(){
        await this.loadProviders();
        this.editingId = null;
        this.form = { provider: '', original_code: null, mapped_code: null, enabled: true };
        this.dialogVisible = true;
      },
      async openEdit(row){
        await this.loadProviders();
        this.editingId = row.id;
        this.form = { provider: row.provider||'', original_code: row.original_code, mapped_code: row.mapped_code, enabled: !!row.enabled };
        this.dialogVisible = true;
      },
      async submit(){
        if(!this.form.original_code || !this.form.mapped_code){
          ElementPlus.ElMessage.warning('请填写原始错误码和映射错误码');
          return;
        }
        this.submitting = true;
        try {
          const url = this.editingId
            ? API+'/api/error-mappings/'+this.editingId
            : API+'/api/error-mappings';
          const method = this.editingId ? 'PUT' : 'POST';
          const r = await fetch(url,{
            method,
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({
              provider: this.form.provider,
              original_code: this.form.original_code,
              mapped_code: this.form.mapped_code,
              enabled: this.form.enabled,
            })
          });
          if(!r.ok){
            const j = await r.json().catch(()=>({}));
            ElementPlus.ElMessage.error(j.error||'保存失败');
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
            '确定删除此错误码映射规则？',
            '提示',
            { type:'warning', confirmButtonText:'删除', cancelButtonText:'取消' }
          );
        } catch(e){ return; }
        try {
          const r = await fetch(API+'/api/error-mappings/'+id,{method:'DELETE'});
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
          const r = await fetch(API+'/api/error-mappings/'+id,{
            method:'PUT',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({enabled})
          });
          if(!r.ok){
            ElementPlus.ElMessage.error('切换状态失败');
            this.load();
          } else {
            this.load();
          }
        } catch(e) {
          ElementPlus.ElMessage.error('切换状态失败');
          this.load();
        }
      },
    },
    mounted(){
      this.load();
    }
  });
  ErrorsApp.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  if (window.ElementPlusIconsVue) {
    for (const [k, v] of Object.entries(ElementPlusIconsVue)) ErrorsApp.component(k, v);
  }
  ErrorsApp.config.globalProperties.esc = esc;
  ErrorsApp.mount('#vue-errors');
}
