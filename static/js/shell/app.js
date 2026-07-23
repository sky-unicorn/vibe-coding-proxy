// ---- Vue 岛屿：公共外壳 Header + Tabs（阶段 8）----
// 7 个面板已全部 Vue 化，统一迁移外壳：Header（版本徽章/URL 复制条/登出）+ Tabs 导航。
// 原生 switchTab 退役，改用 el-tabs 的 v-model 绑定 + hash 路由双向同步。
if (window.Vue && window.ElementPlus) {

  // ============ Header 应用 ============
  const HeaderTemplate = `
<div class="header">
  <div style="display:flex;align-items:center">
    <h1>Vibe Coding 服务转发</h1>
    <el-tag v-if="ver.state==='loading'" type="info" effect="plain" size="small" style="margin-left:10px">v? 检查中…</el-tag>
    <el-tag v-else-if="ver.state==='latest'" type="success" size="small" style="margin-left:10px">✓ 已是最新版 (v[[ ver.current ]])</el-tag>
    <el-tag v-else-if="ver.state==='new'" type="warning" effect="dark" size="small" style="margin-left:10px;cursor:pointer" @click="openLatest">🆕 有新版本 v[[ ver.latest ]]</el-tag>
    <el-tag v-else type="info" size="small" style="margin-left:10px" :title="'当前版本 v'+ver.current+'（未能获取最新版本信息）'">v[[ ver.current ]]</el-tag>
  </div>
  <div style="display:flex;align-items:center;gap:12px">
    <div class="url-bar">
      <div class="url-item" v-for="u in urls" :key="u.key">
        <span class="url-label" :class="u.cls">[[ u.label ]]</span>
        <code>[[ u.url ]]</code>
        <el-button text size="small" @click="copyUrl(u)">复制</el-button>
      </div>
    </div>
    <el-button :icon="RefreshIcon" circle size="small" :loading="verRefreshing" @click="refreshVersion(true)" title="重新检查最新版本" />
    <el-button type="danger" size="small" @click="logout">登出</el-button>
  </div>
</div>
`;

  const HeaderApp = Vue.createApp({
    delimiters: ['[[', ']]'],
    template: HeaderTemplate,
    data(){
      return {
        // 版本徽章三态：loading / latest / new / unknown
        ver: { state:'loading', current:'', latest:'', latest_url:'', releases_url:'' },
        verRefreshing: false,
        RefreshIcon: window.ElementPlusIconsVue ? window.ElementPlusIconsVue.Refresh : null,
      }
    },
    computed: {
      urls(){
        const base = location.origin;
        return [
          { key:'anthropic', label:'Anthropic', cls:'anthropic', url: base+'/anthropic' },
          { key:'openai-chat', label:'OpenAiChat', cls:'openai', url: base+'/v1' },
          { key:'openai-responses', label:'OpenAiResponses', cls:'openai', url: base+'/openai' },
        ];
      }
    },
    methods: {
      async refreshVersion(force){
        this.verRefreshing = true;
        try {
          const url = '/api/version' + (force ? ('?force=1&t='+Date.now()) : '');
          const r = await fetch(url);
          if(r.ok){
            const info = await r.json();
            if(info.has_update===true){
              this.ver = { state:'new', current:info.current||'', latest:info.latest||'', latest_url:info.latest_url||'', releases_url:info.releases_url||'' };
            } else if(info.has_update===false){
              this.ver = { state:'latest', current:info.current||'' };
            } else {
              this.ver = { state:'unknown', current:info.current||'' };
            }
          }
        } catch(e) { /* 静默：徽章保留当前版本字样 */ }
        finally { this.verRefreshing = false; }
      },
      openLatest(){
        const url = this.ver.latest_url || this.ver.releases_url;
        if(url) window.open(url, '_blank');
      },
      copyUrl(u){
        navigator.clipboard.writeText(u.url).then(()=>{
          ElementPlus.ElMessage.success('已复制：'+u.url);
        }).catch(()=>{
          ElementPlus.ElMessage.error('复制失败');
        });
      },
      async logout(){
        await fetch('/api/auth/logout', { method:'POST' });
        window.location.href = '/login';
      },
    },
    mounted(){
      this.refreshVersion(false);
    }
  });
  HeaderApp.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  if (window.ElementPlusIconsVue) {
    for (const [k, v] of Object.entries(ElementPlusIconsVue)) HeaderApp.component(k, v);
  }
  HeaderApp.mount('#vue-shell-header');


  // ============ Tabs 应用 ============
  const TabsTemplate = `
<el-tabs v-model="activeTab">
  <el-tab-pane v-for="t in tabs" :key="t.name" :label="t.label" :name="t.name"></el-tab-pane>
</el-tabs>
`;

  const TabsApp = Vue.createApp({
    delimiters: ['[[', ']]'],
    template: TabsTemplate,
    data(){
      return {
        activeTab: 'providers',
        tabs: [
          { name:'providers', label:'提供商管理' },
          { name:'models', label:'模型映射' },
          { name:'errors', label:'错误码映射' },
          { name:'logs', label:'请求日志' },
          { name:'apikeys', label:'API Key' },
          { name:'billing', label:'计费管理' },
          { name:'mcp', label:'MCP 广场' },
        ],
        _onHashChange: null,
        // 程序设值（mounted/onHashChange 初始化）时为 true，watch 据此跳过 reload 与重设 hash
        _suppressReload: false,
      }
    },
    watch: {
      activeTab(name){
        // 程序设值（mounted/onHashChange 初始化）时不触发 reload 与重设 hash，只同步面板显示
        if(this._suppressReload){
          this._suppressReload = false;
          this.applyTab(name, false);
          return;
        }
        // 用户点击 el-tabs 触发：完整切换（同步 hash + 触发 reload）
        this.applyTab(name, true);
      }
    },
    methods: {
      // 切换面板可见性 + 同步 hash + 触发对应面板刷新
      applyTab(name, setHash){
        document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
        const panel = document.getElementById('panel-'+name);
        if(panel) panel.classList.add('active');
        if(setHash && location.hash !== '#'+name) history.replaceState(null, '', '#'+name);
        // 切回面板时刷新（errors/apikeys 无 reload 桥接，因其自加载即可）
        const reloaders = { providers:'__reloadProviders', models:'__reloadModels', logs:'__reloadLogs', billing:'__reloadBilling' };
        const fn = reloaders[name];
        if(fn) window[fn] && window[fn]();
      },
      onHashChange(){
        const h = location.hash.replace('#','');
        const valid = this.tabs.map(t=>t.name);
        if(valid.includes(h) && h !== this.activeTab){
          // 浏览器前进/后退：仅切显示 + 同步 activeTab，不 reload（避免覆盖用户在面板内的交互状态）
          // hash 已是 h，无需 setHash；用 suppress 标志让 watch 走 suppress 分支
          this._suppressReload = true;
          this.activeTab = h;
        }
      },
    },
    mounted(){
      // 读 location.hash 初始化 activeTab。
      // 注意：面板 div 的 mounted.load 早已在 app.js 之前执行完毕（自加载一次），
      // 若此处赋 activeTab 触发 watch→applyTab(h, true)→__reloadXXX，会双倍请求。
      // 故用 _suppressReload 标志：赋 activeTab 让 el-tabs 视觉同步，
      // watch 检测到标志后仅执行 applyTab(h, false)（切 .active class、不 reload、不重设 hash）。
      const h = location.hash.replace('#','');
      const valid = this.tabs.map(t=>t.name);
      if(valid.includes(h) && h !== this.activeTab){
        this._suppressReload = true;
        this.activeTab = h;
      }
      // 监听浏览器前进/后退
      this._onHashChange = this.onHashChange.bind(this);
      window.addEventListener('hashchange', this._onHashChange);
    },
    beforeUnmount(){
      if(this._onHashChange) window.removeEventListener('hashchange', this._onHashChange);
    }
  });
  TabsApp.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  if (window.ElementPlusIconsVue) {
    for (const [k, v] of Object.entries(ElementPlusIconsVue)) TabsApp.component(k, v);
  }
  TabsApp.mount('#vue-shell-tabs');
}
