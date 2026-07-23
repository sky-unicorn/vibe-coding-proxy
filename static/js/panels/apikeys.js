// ---- Vue 岛屿：API Key 面板（阶段 1）----
if (window.Vue && window.ElementPlus) {
  const ApiKeysTemplate = `
<div class="card">
  <div class="card-header">
    <h2>API Key 管理</h2>
    <el-button type="primary" @click="openCreate">创建 Key</el-button>
  </div>
  <p style="font-size:13px;color:var(--text2);margin-bottom:12px">管理用于访问 Anthropic 和 OpenAI 代理的 API Key。客户端请求时需在请求头中携带此 Key 进行身份验证。</p>

  <el-table :data="keys" v-loading="loading" style="width:100%" empty-text="暂无 API Key，点击上方按钮创建">
    <el-table-column label="名称" prop="key_name" min-width="200" show-overflow-tooltip><template #default="{row}">[[ esc(row.key_name) ]]</template></el-table-column>
    <el-table-column label="API Key" min-width="160"><template #default="{row}"><code style="font-size:12px">[[ row.api_key_prefix ]]</code></template></el-table-column>
    <el-table-column label="" width="80"><template #default="{row}"><el-button link @click="openView(row.id)">查看</el-button></template></el-table-column>
    <el-table-column label="状态" min-width="150">
      <template #default="{row}">
        <el-switch :model-value="!!row.enabled" @change="v=>toggle(row.id,v)" />
        <el-tag size="small" :type="row.enabled?'success':'danger'" style="margin-left:6px">[[ row.enabled?'启用':'禁用' ]]</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="创建时间" min-width="170"><template #default="{row}"><span style="font-size:12px">[[ formatTime(row.created_at) ]]</span></template></el-table-column>
    <el-table-column label="最后使用" min-width="170"><template #default="{row}"><span style="font-size:12px">[[ row.last_used_at?formatTime(row.last_used_at):'-' ]]</span></template></el-table-column>
    <el-table-column label="操作" width="90"><template #default="{row}"><el-button type="danger" link @click="del(row.id)">删除</el-button></template></el-table-column>
  </el-table>

  <!-- 创建弹窗 -->
  <el-dialog v-model="dialogCreate" title="创建 API Key" width="420px" :close-on-click-modal="false">
    <el-form>
      <el-form-item label="Key 名称">
        <el-input v-model="form.key_name" placeholder="如: Claude Code" />
      </el-form-item>
    </el-form>
    <el-alert v-if="createdKey" type="warning" :closable="false" show-icon style="margin:8px 0">
      <template #title>请复制保存此 Key（仅展示一次，不可复现）</template>
      <div style="display:flex;align-items:center;gap:8px;margin-top:4px">
        <code style="word-break:break-all">[[ createdKey ]]</code>
        <el-button link @click="copyCreated">复制</el-button>
      </div>
    </el-alert>
    <template #footer>
      <el-button @click="closeCreate">关闭</el-button>
      <el-button v-if="!createdKey" type="primary" :loading="submitting" @click="submitCreate">创建</el-button>
    </template>
  </el-dialog>

  <!-- 查看弹窗 -->
  <el-dialog v-model="dialogView" title="查看 API Key" width="420px">
    <div style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:12px">
      <code style="word-break:break-all">[[ viewKey.api_key ]]</code>
    </div>
    <template #footer>
      <el-button @click="dialogView=false">关闭</el-button>
      <el-button type="primary" @click="copyView">复制</el-button>
    </template>
  </el-dialog>
</div>
`;

  const ApiKeysApp = Vue.createApp({
    delimiters: ['[[', ']]'],
    template: ApiKeysTemplate,
    data(){
      return {
        keys: [],
        loading: false,
        dialogCreate: false,
        dialogView: false,
        form: { key_name: '' },
        submitting: false,
        createdKey: '',
        viewKey: { api_key: '', key_name: '' },
      }
    },
    methods: {
      async load(){
        this.loading = true;
        try {
          const r = await fetch(API+'/api/keys');
          this.keys = await r.json();
        } catch(e) {
          ElementPlus.ElMessage.error('加载 API Key 列表失败');
        } finally {
          this.loading = false;
        }
      },
      openCreate(){
        this.form = { key_name: '' };
        this.createdKey = '';
        this.dialogCreate = true;
      },
      async submitCreate(){
        if(!this.form.key_name.trim()){
          ElementPlus.ElMessage.warning('请填写 Key 名称');
          return;
        }
        this.submitting = true;
        try {
          const r = await fetch(API+'/api/keys',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({key_name:this.form.key_name.trim()})
          });
          if(!r.ok){
            const j = await r.json().catch(()=>({}));
            ElementPlus.ElMessage.error(j.error||'创建失败');
            return;
          }
          const j = await r.json();
          this.createdKey = j.api_key;
          this.load();
        } catch(e) {
          ElementPlus.ElMessage.error('创建失败');
        } finally {
          this.submitting = false;
        }
      },
      closeCreate(){
        this.dialogCreate = false;
        this.createdKey = '';
        this.form = { key_name: '' };
      },
      async del(id){
        try {
          await ElementPlus.ElMessageBox.confirm(
            '确定删除此 API Key？删除后使用此 Key 的客户端将无法访问代理。',
            '提示',
            { type:'warning', confirmButtonText:'删除', cancelButtonText:'取消' }
          );
        } catch(e){ return; }
        try {
          const r = await fetch(API+'/api/keys/'+id,{method:'DELETE'});
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
          const r = await fetch(API+'/api/keys/'+id+'/toggle',{
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
      async openView(id){
        try {
          const r = await fetch(API+'/api/keys/'+id);
          if(!r.ok){
            ElementPlus.ElMessage.error('获取 Key 失败');
            return;
          }
          this.viewKey = await r.json();
          this.dialogView = true;
        } catch(e) {
          ElementPlus.ElMessage.error('获取 Key 失败');
        }
      },
      copyCreated(){
        navigator.clipboard.writeText(this.createdKey).then(()=>{
          ElementPlus.ElMessage.success('已复制到剪贴板');
        }).catch(()=>{
          ElementPlus.ElMessage.error('复制失败');
        });
      },
      copyView(){
        navigator.clipboard.writeText(this.viewKey.api_key).then(()=>{
          ElementPlus.ElMessage.success('已复制到剪贴板');
        }).catch(()=>{
          ElementPlus.ElMessage.error('复制失败');
        });
      },
    },
    mounted(){
      this.load();
    }
  });
  ApiKeysApp.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  if (window.ElementPlusIconsVue) {
    for (const [k, v] of Object.entries(ElementPlusIconsVue)) ApiKeysApp.component(k, v);
  }
  ApiKeysApp.config.globalProperties.esc = esc;
  ApiKeysApp.config.globalProperties.formatTime = formatTime;
  ApiKeysApp.mount('#vue-apikeys');
}
