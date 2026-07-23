// ---- Vue 岛屿：MCP 广场面板（阶段 7）----
if (window.Vue && window.ElementPlus) {
  const McpTemplate = `
<div class="mcp-root">
  <!-- 标题区（固定在顶部，不随列表滚动） -->
  <div class="mcp-head" style="margin-bottom:18px">
    <h2 style="font-size:18px;font-weight:600">MCP 广场</h2>
    <p style="font-size:13px;color:var(--text2);margin-top:6px;line-height:1.6">所有可用的 MCP 服务。在 AI 客户端（Claude Code / Codex）配置对应端点与 headers 即可调用，点击卡片展开配置详情。</p>
  </div>

  <!-- 方块列表 + 详情的滚动容器：滚动条收在本层，不撑出页面整体滚动 -->
  <div class="mcp-scroll-wrap custom-scroll">
    <!-- 卡片网格：新增 MCP 时在 grid 内追加一张 el-card + 对应 detail 块 -->
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin-bottom:8px">
    <el-card shadow="hover" :body-style="{padding:'16px',display:'flex',flexDirection:'column',gap:'10px',cursor:'pointer'}" @click="toggle">
      <div style="display:flex;gap:12px;align-items:center">
        <div class="mcp-card-icon">N</div>
        <div>
          <div class="mcp-card-title">Nacos MCP</div>
          <el-tag type="primary" size="small">HTTP</el-tag>
        </div>
      </div>
      <div class="mcp-card-desc">通过 MCP 协议远程管理 Nacos 3.x 命名空间与配置--让 AI 直接读取、发布、回滚配置。</div>
      <div class="mcp-card-stats">
        <span class="mcp-stat"><strong>9</strong> 个工具</span>
        <span class="mcp-stat">端点 <strong>/mcp</strong></span>
        <span class="mcp-stat">连接参数 <strong>headers</strong></span>
      </div>
      <el-button link class="mcp-card-toggle" @click.stop="toggle">[[ expanded?'收起 ▴':'展开 ▾' ]]</el-button>
    </el-card>
  </div>

  <!-- Nacos 配置详情（默认收起，点击卡片展开） -->
  <div v-if="expanded">
    <!-- MCP 客户端配置（headers-only） -->
    <div class="card">
      <div class="card-header"><h2>MCP 客户端配置</h2></div>
      <p style="font-size:13px;color:var(--text2);margin-bottom:14px;line-height:1.6">Nacos 连接参数<strong style="color:var(--text)">不再在此项目配置</strong>，而是在你的 AI 客户端（Claude Code / Codex）的 MCP server 配置里通过 HTTP headers 提供，每次请求自动携带给 <code class="inline-code">/mcp</code>。下方「MCP 端点」卡片有完整的可复制配置示例。</p>
      <div class="mcp-tip">
        需要提供的 4 个 header（大小写敏感，<strong style="color:var(--text)">每次请求自动携带</strong>）：
        <el-table :data="nacosHeaders" style="margin-top:10px;width:100%" size="small">
          <el-table-column label="Header" width="230">
            <template #default="{row}"><code style="font-family:monospace;font-size:12px;color:var(--primary-h)">[[ row.name ]]</code></template>
          </el-table-column>
          <el-table-column label="含义" min-width="260">
            <template #default="{row}"><span v-html="row.desc" style="font-size:13px;line-height:1.5;color:var(--text2)"></span></template>
          </el-table-column>
        </el-table>
        <div style="margin-top:10px"><strong style="color:var(--text)">注意：</strong>项目 API Key 单独走 <code class="inline-code">Authorization: Bearer &lt;key&gt;</code>，与上面 4 个 Nacos header 并存。账号密码不会落地本项目数据库。</div>
      </div>
    </div>

    <!-- MCP 端点信息 -->
    <div class="card">
      <div class="card-header"><h2>MCP 端点</h2></div>
      <div class="mcp-url-bar">
        <span class="url-label anthropic">MCP URL</span>
        <code>[[ mcpUrl ]]</code>
        <el-button size="small" @click="copyMcpUrl">复制</el-button>
      </div>
      <div class="mcp-tip tip-warn">
        <strong style="color:var(--warn)">API Key：</strong>请到 <strong>API Key</strong> Tab 复制你已创建的 key，或新建后立即保存。在 Claude Code / Codex 的 MCP 配置里填此 URL，并在请求头携带 <code>Authorization: Bearer &lt;你的项目 API Key&gt;</code>。
      </div>

      <el-collapse v-model="configCollapse" style="margin-top:14px">
        <el-collapse-item title="查看配置示例（Claude Code / Codex）" name="config">
          <div style="display:flex;flex-direction:column;gap:14px;margin-bottom:12px">
            <div>
              <div class="mcp-code-sample-label">Claude Code（<code>.mcp.json</code> 或全局配置）：</div>
              <pre class="mcp-code-block">{
  "mcpServers": {
    "nacos": {
      "url": "[[ mcpUrl ]]",
      "type": "http",
      "headers": {
        "Authorization": "Bearer &lt;你的项目 API Key&gt;",
        "X-Nacos-Console-Url": "http://your-nacos-host:8848",
        "X-Nacos-Auth-Url": "http://your-nacos-host:8848",
        "X-Nacos-Username": "nacos",
        "X-Nacos-Password": "你的Nacos密码"
      }
    }
  }
}</pre>
            </div>
            <div>
              <div class="mcp-code-sample-label">Codex CLI（<code>config.toml</code> 中追加）：</div>
              <pre class="mcp-code-block">[mcp_servers.nacos]
url = "[[ mcpUrl ]]"
bearer_token = "&lt;你的项目 API Key&gt;"

[mcp_servers.nacos.http_headers]
X-Nacos-Console-Url = "http://your-nacos-host:8848"
X-Nacos-Auth-Url = "http://your-nacos-host:8848"
X-Nacos-Username = "nacos"
X-Nacos-Password = "你的Nacos密码"</pre>
              <span class="form-hint" style="margin-top:6px">Codex CLI 已支持 <code>http_headers</code>（<a href="https://github.com/openai/codex/issues/5180" target="_blank" style="color:var(--primary-h)">#5180</a>，已发布）。<code>bearer_token</code> 是 Codex 的 Authorization Bearer 简写；也可用 <code>bearer_token_env_var = "NACOS_MCP_API_KEY"</code> 从环境变量取。</span>
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>
    </div>

    <!-- 工具清单 -->
    <div class="card">
      <div class="card-header"><h2>工具清单（9 个）</h2></div>
      <p style="font-size:13px;color:var(--text2);margin-bottom:12px;line-height:1.6">AI 客户端通过 MCP 可调用的 Nacos 管理工具。工具名统一以 <code class="inline-code">nacos_</code> 前缀。</p>
      <el-table :data="tools" style="width:100%">
        <el-table-column label="工具名" min-width="200">
          <template #default="{row}"><code style="font-family:monospace;font-size:12px;color:var(--primary-h)">[[ row.name ]]</code></template>
        </el-table-column>
        <el-table-column label="类别" width="120" align="center">
          <template #default="{row}">
            <el-tag :type="row.cat==='命名空间'?'primary':'success'" size="small">[[ row.cat ]]</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="260">
          <template #default="{row}"><span style="font-size:13px;line-height:1.5;color:var(--text2)">[[ row.desc ]]</span></template>
        </el-table-column>
      </el-table>
    </div>
  </div><!-- /.mcp-scroll-wrap -->
</div>
`;

  const McpApp = Vue.createApp({
    delimiters: ['[[', ']]'],
    template: McpTemplate,
    data(){
      return {
        expanded: false,
        configCollapse: [],
        nacosHeaders: [
          { name:'X-Nacos-Console-Url', desc:'Console API 基址（命名空间 / 配置 CRUD），merged 部署通常<strong>不带</strong> <code>/nacos</code>，如 <code>http://host:8848</code>' },
          { name:'X-Nacos-Auth-Url', desc:'认证基址，通常<strong>带</strong> <code>/nacos</code>，如 <code>http://host:8848/nacos</code>' },
          { name:'X-Nacos-Username', desc:'Nacos 登录用户名（如 <code>nacos</code>）' },
          { name:'X-Nacos-Password', desc:'Nacos 登录密码' },
        ],
        tools: [
          { name:'nacos_list_namespaces', cat:'命名空间', desc:'列出全部命名空间（含 public）' },
          { name:'nacos_create_namespace', cat:'命名空间', desc:'创建命名空间（id 可选，省略由服务端生成）' },
          { name:'nacos_update_namespace', cat:'命名空间', desc:'修改命名空间名称/描述（id 不可改）' },
          { name:'nacos_delete_namespace', cat:'命名空间', desc:'删除命名空间（public 不可删）' },
          { name:'nacos_list_configs', cat:'配置', desc:'分页查询配置列表，支持 dataId/group 搜索' },
          { name:'nacos_get_config', cat:'配置', desc:'读取单个配置完整内容（含 type、md5）' },
          { name:'nacos_publish_config', cat:'配置', desc:'发布配置（存在即覆盖，新增/修改合一）' },
          { name:'nacos_delete_config', cat:'配置', desc:'删除单个配置' },
          { name:'nacos_get_config_history', cat:'配置', desc:'查询配置历史版本' },
        ],
      }
    },
    computed: {
      // 端点 URL 动态生成（location 运行期不变，computed 一次性求值缓存；无需 switchTab 手动刷新）
      mcpUrl(){
        return location.protocol+'//'+location.host+'/mcp';
      }
    },
    methods: {
      toggle(){ this.expanded = !this.expanded; },
      copyMcpUrl(){
        const text = this.mcpUrl;
        navigator.clipboard.writeText(text).then(()=>{
          ElementPlus.ElMessage.success('已复制到剪贴板：'+text);
        }).catch(()=>{
          // fallback
          const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');}catch(e){}document.body.removeChild(ta);
          ElementPlus.ElMessage.success('已复制到剪贴板');
        });
      },
    },
  });
  McpApp.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  if (window.ElementPlusIconsVue) {
    for (const [k, v] of Object.entries(ElementPlusIconsVue)) McpApp.component(k, v);
  }
  McpApp.mount('#vue-mcp');
}
