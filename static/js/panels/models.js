// ---- Vue 岛屿：模型映射面板（阶段 4）----
if (window.Vue && window.ElementPlus) {
  // 别名分组配色盘：同名别名同色（与旧实现一致），R,G,B 字符串供 rgba()/rgb() 注入
  const ALIAS_PALETTE=[
    '99,102,241','16,185,129','245,158,11','239,68,68','59,130,246','217,70,239',
    '20,184,166','251,146,60','96,165,250','192,132,252','52,211,153','253,186,116',
  ];
  // settings 表里降级三键是字符串 '0'/'1'，统一宽松判定为布尔
  const truthy=v=>v==='1'||v===1||v===true||String(v).toLowerCase()==='true';

  const ModelsTemplate = `
<div class="card">
  <div class="card-header">
    <h2>模型映射</h2>
    <div style="display:flex;gap:8px">
      <el-button @click="openDegradation">降级设置</el-button>
      <el-button type="primary" @click="openCreate">添加映射</el-button>
    </div>
  </div>
  <p style="font-size:13px;color:var(--text2);margin-bottom:12px">将请求中的模型名（别名）映射到实际的模型和提供商。同一别名可配置多条映射，系统按优先级加权轮询，并自动选并发最低的提供商。</p>

  <el-table :data="rows" v-loading="loading" style="width:100%" :row-style="rowStyle" :max-height="tableMaxHeight" empty-text="暂无映射，点击上方按钮添加">
    <el-table-column label="别名" min-width="170">
      <template #default="{row}">
        <span class="alias-badge" :style="{'--alias-rgb':row._aliasColor}"><span class="alias-color-dot"></span>[[ row.alias ]]</span>
      </template>
    </el-table-column>
    <el-table-column label="优先级" width="80" align="center">
      <template #default="{row}">
        <span class="badge" :style="{background:'rgba('+row._aliasColor+',.35)',color:'rgb('+row._aliasColor+')'}">[[ row.priority!=null?row.priority:1 ]]</span>
      </template>
    </el-table-column>
    <el-table-column label="目标模型" min-width="190" show-overflow-tooltip>
      <template #default="{row}"><code style="font-size:12px">[[ row.target_model ]]</code></template>
    </el-table-column>
    <el-table-column label="模型类型" width="90" align="center">
      <template #default="{row}">
        <el-tag v-if="row.model_type==='multimodal'" type="primary" size="small">多模态</el-tag>
        <el-tag v-else type="info" size="small">文本</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="最大输出 Token" width="130" align="center">
      <template #default="{row}">
        <code v-if="row.max_tokens>0" style="font-size:12px">[[ row.max_tokens ]]</code>
        <span v-else style="color:var(--text2)">默认</span>
      </template>
    </el-table-column>
    <el-table-column label="提供商" min-width="150">
      <template #default="{row}">
        <span style="white-space:nowrap">
          <span>[[ row.provider_name||'未知' ]]</span>
          <el-tag v-if="row._provDisabled" type="danger" size="small" style="margin-left:6px">提供商已禁用</el-tag>
        </span>
      </template>
    </el-table-column>
    <el-table-column label="降级状态" width="150">
      <template #default="{row}">
        <el-tag v-if="!isDegraded(row)" type="success" size="small">正常</el-tag>
        <el-tag v-else type="danger" size="small">降级（剩余 [[ degRemaining(row) ]]s）</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="状态" min-width="150">
      <template #default="{row}">
        <el-switch :model-value="!!row.enabled" :disabled="row._provDisabled" @change="v=>toggle(row.id,v)" />
        <el-tag size="small" :type="row.enabled?'success':'danger'" style="margin-left:6px">[[ row.enabled?'启用':'禁用' ]]</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="操作" width="340" fixed="right">
      <template #default="{row}">
        <div style="white-space:nowrap">
          <el-button link @click="openEdit(row)">编辑</el-button>
          <el-button link style="color:#10b981" @click="openCodex(row)">Codex配置</el-button>
          <el-button link style="color:#6366f1" @click="openRole(row)">角色映射</el-button>
          <el-button link type="danger" @click="del(row.id)">删除</el-button>
        </div>
      </template>
    </el-table-column>
  </el-table>

  <!-- 创建/编辑映射弹窗 -->
  <el-dialog v-model="dialogModel" :title="editingId?'编辑映射':'添加映射'" width="580px" :close-on-click-modal="false">
    <el-form label-width="120px">
      <el-form-item label="别名">
        <el-input v-model="form.alias" placeholder="如: claude-sonnet-4-20250514" />
        <div class="form-hint">对外模型名，多条同名则按优先级加权轮询</div>
      </el-form-item>
      <el-form-item label="优先级">
        <el-input-number v-model="form.priority" :min="1" :controls="false" style="width:140px" />
        <div class="form-hint">数值越小优先级越高。同一别名下，优先级高的模型获得更多流量，但所有模型都会被使用</div>
      </el-form-item>
      <el-form-item label="目标模型">
        <el-input v-model="form.targetModel" placeholder="如: claude-sonnet-4-20250514 或 gpt-4o" />
      </el-form-item>
      <el-form-item label="模型类型">
        <el-select v-model="form.modelType" style="width:100%">
          <el-option label="文本模型" value="text" />
          <el-option label="多模态模型" value="multimodal" />
        </el-select>
        <div class="form-hint">多模态模型会保留图片内容，文本模型会自动移除图片</div>
      </el-form-item>
      <el-form-item label="最大输出 Token">
        <el-input-number v-model="form.maxTokens" :min="0" :controls="false" style="width:140px" />
        <div class="form-hint">替换调用方传入的最大输出 token 数，留 0 表示不替换。仅当调用方传入超限导致上游报错时才需配置（如 kimi-2.7 上限 32768，调用方传 64000 报错则填 32768）</div>
      </el-form-item>
      <el-form-item label="提供商">
        <el-select v-model="form.providerId" placeholder="选择提供商" style="width:100%">
          <el-option v-for="p in providers" :key="p.id" :label="p.name + protoLabel(p)" :value="p.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="form.enabled" :disabled="formEnabledLocked" />
        <span style="font-size:13px;margin-left:8px" :style="{color:formEnabledLocked?'#f59e0b':''}">[[ formEnabledLocked?'启用（提供商已禁用，请先启用提供商）':'启用' ]]</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogModel=false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
    </template>
  </el-dialog>

  <!-- Codex 配置弹窗（三方互斥：think / reasoningField / nativeResponses） -->
  <el-dialog v-model="dialogCodex" title="Codex 配置" width="580px" :close-on-click-modal="false">
    <p style="font-size:12px;color:var(--text2);margin-bottom:16px">仅作用于 codex CLI 走的 Responses→Chat 转换路径（Claude Code 走 Anthropic 直转不受影响）。</p>

    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
      <el-switch v-model="codexForm.reasoning" />
      <span style="font-size:13px">透传思考强度参数（<code class="inline-code">reasoning_effort</code>）</span>
    </div>
    <div class="form-hint" style="margin:0 0 14px 52px">开启后调用方传来的思考强度（如 <code>reasoning_effort=high</code>）原值透传给上游；关闭则保守跳过。默认开启，若实测 400 再关闭。值 low/medium/high 不做翻译。</div>

    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
      <el-switch v-model="codexForm.think" @change="v=>mutexCodex('think',v)" />
      <span style="font-size:13px">思考过程注入：<code class="inline-code">&lt;think&gt;</code> 标签</span>
    </div>
    <div class="form-hint" style="margin:0 0 14px 52px">将 codex 回传的历史 reasoning 以 <code>&lt;think&gt;...&lt;/think&gt;</code> 标签注入 assistant 消息 content。适用：MiniMax-M3（Interleaved Thinking，多轮工具调用必需）。默认关闭。</div>

    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
      <el-switch v-model="codexForm.reasoningField" @change="v=>mutexCodex('reasoningField',v)" />
      <span style="font-size:13px">思考过程注入：<code class="inline-code">reasoning_content</code> 字段</span>
    </div>
    <div class="form-hint" style="margin:0 0 14px 52px">将 codex 回传的历史 reasoning 以独立 <code>reasoning_content</code> 字段注入 assistant 消息。适用：DeepSeek/GLM/Kimi 思考模式原生字段（多轮工具调用必需，缺失会 400）。默认开启。</div>

    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
      <el-switch v-model="codexForm.nativeResponses" @change="v=>mutexCodex('nativeResponses',v)" />
      <span style="font-size:13px">模型原生支持 Responses 协议（透传，跳过 Responses↔Chat 转换）</span>
    </div>
    <div class="form-hint" style="margin:0 0 14px 52px">开启后 <code>/openai</code> 端点对该 mapping 不做 Responses↔Chat 双向转换，直接按 <code>openai_url</code> 派生 <code>/responses</code> 原样转发。上方两个"思考过程注入"开关自动关闭、三方互斥。默认关闭。</div>

    <template #footer>
      <el-button @click="dialogCodex=false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="saveCodex">保存</el-button>
    </template>
  </el-dialog>

  <!-- 角色映射弹窗（动态多行） -->
  <el-dialog v-model="dialogRole" title="角色映射" width="540px" :close-on-click-modal="false">
    <div class="rm-tip">将请求中指定角色的消息替换为目标角色，仅对当前提供商的当前模型生效。例如：部分上游不支持 <code>developer</code> 角色，可映射为 <code>system</code>。</div>
    <div v-for="(r,i) in roleForm.rules" :key="i" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
      <el-input v-model="r.from" placeholder="原角色（如 developer）" />
      <span style="color:var(--primary-h);flex-shrink:0">→</span>
      <el-input v-model="r.to" placeholder="目标角色（如 system）" />
      <el-button type="danger" size="small" @click="removeRule(i)">✕</el-button>
    </div>
    <div v-if="!roleForm.rules.length" class="rm-empty">暂无映射规则，点击下方按钮添加一条</div>
    <el-button style="width:100%;border-style:dashed;margin-top:4px" @click="addRule">+ 添加规则</el-button>
    <template #footer>
      <el-button @click="dialogRole=false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="saveRole">保存</el-button>
    </template>
  </el-dialog>

  <!-- 降级设置弹窗 -->
  <el-dialog v-model="dialogDeg" title="降级设置" width="560px" :close-on-click-modal="false">
    <el-form label-width="120px">
      <el-form-item label="启用服务降级">
        <el-switch :model-value="degSettings.enabled" @change="onDegEnabledChange" />
        <span style="font-size:12px;color:var(--text2);margin-left:10px">上游持续报错时自动临时跳过该映射</span>
      </el-form-item>
      <el-form-item label="严格优先级">
        <el-switch v-model="degSettings.strict" :disabled="!degSettings.enabled" @change="onDegStrictChange" />
        <span style="font-size:12px;color:var(--text2);margin-left:10px">仅在最高优先级层内选择，逐级下放</span>
      </el-form-item>
      <el-form-item label="降级持续秒数">
        <el-input-number v-model="degSettings.duration" :min="1" :controls="false" style="width:140px" />
        <span style="font-size:12px;color:var(--text2);margin-left:10px">单次降级时长</span>
      </el-form-item>
      <el-form-item label="转发重试次数">
        <el-input-number v-model="degSettings.retryCount" :min="0" :step="1" :controls="false" style="width:140px" />
        <span style="font-size:12px;color:var(--text2);margin-left:10px">首次失败后最多再重试的次数（0=不重试）</span>
      </el-form-item>
      <el-form-item label="重试间隔秒数">
        <el-input-number v-model="degSettings.retryDelay" :min="0" :step="0.5" :precision="1" :controls="false" style="width:140px" />
        <span style="font-size:12px;color:var(--text2);margin-left:10px">两次重试之间的等待时间</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogDeg=false">取消</el-button>
      <el-button type="primary" @click="saveDegradation">保存</el-button>
    </template>
  </el-dialog>
</div>
`;

  const ModelsApp = Vue.createApp({
    delimiters: ['[[', ']]'],
    template: ModelsTemplate,
    data(){
      return {
        rows: [],
        providers: [],
        degData: {},
        degSettings: { enabled:false, strict:false, duration:30, retryCount:3, retryDelay:1 },
        loading: false,
        dialogModel: false,
        dialogCodex: false,
        dialogRole: false,
        dialogDeg: false,
        editingId: null,
        submitting: false,
        form: { alias:'', priority:1, targetModel:'', modelType:'text', maxTokens:0, providerId:null, enabled:true },
        codexForm: { mappingId:null, reasoning:true, think:false, reasoningField:true, nativeResponses:false },
        roleForm: { mappingId:null, rules:[] },
        _degTimer: null,
        tableMaxHeight: 500,
      }
    },
    computed: {
      // 所选 provider 禁用时，锁定「启用」开关（与列表 toggle 锁定、后端拦截语义一致）
      formEnabledLocked(){
        const p = this.providers.find(x=>x.id===this.form.providerId);
        return p ? !p.enabled : false;
      },
    },
    watch: {
      'form.providerId'(){ this.syncEnabledLock(); },
    },
    methods: {
      async load(){
        this.loading = true;
        try {
          const [models, providers, deg] = await Promise.all([
            fetch(API+'/api/models').then(r=>r.json()),
            fetch(API+'/api/providers').then(r=>r.json()),
            fetch(API+'/api/models/degradation').then(r=>r.json()).catch(()=>({})),
          ]);
          this.providers = providers || [];
          this.degData = this.parseDeg(deg);
          // 按别名分桶同色：先整体排序（alias 升序 → priority 升序 → id 升序），再按别名首次出现顺序分配配色
          const rows = (models||[]).map(m=>Object.assign({}, m, {
            _provDisabled: !m.provider_enabled || Number(m.provider_enabled)===0,
            _aliasColor: '',
          }));
          rows.sort((a,b)=>{
            const aa=a.alias||'', ba=b.alias||'';
            if(aa<ba) return -1; if(aa>ba) return 1;
            return (a.priority||1)-(b.priority||1) || a.id-b.id;
          });
          let cIdx=-1, lastAlias=null;
          for(const r of rows){
            if(r.alias!==lastAlias){ cIdx++; lastAlias=r.alias; }
            r._aliasColor = ALIAS_PALETTE[cIdx%ALIAS_PALETTE.length];
          }
          this.rows = rows;
        } catch(e) {
          ElementPlus.ElMessage.error('加载模型列表失败');
        } finally {
          this.loading = false;
        }
      },
      async loadDegradation(){
        try {
          const s = await fetch(API+'/api/settings').then(r=>r.json());
          this.degSettings.enabled = truthy(s.degradation_enabled);
          this.degSettings.strict = truthy(s.degradation_strict_priority);
          this.degSettings.duration = parseInt(s.degradation_duration)||30;
          this.degSettings.retryCount = (parseInt(s.degradation_retry_count)>=0 && !isNaN(parseInt(s.degradation_retry_count)))
            ? parseInt(s.degradation_retry_count) : 3;
          this.degSettings.retryDelay = (parseFloat(s.degradation_retry_delay)>=0 && !isNaN(parseFloat(s.degradation_retry_delay)))
            ? parseFloat(s.degradation_retry_delay) : 1;
          if(!this.degSettings.enabled) this.degSettings.strict = false;
        } catch(e) { /* 静默 */ }
      },
      async pollDegradation(){
        try {
          const deg = await fetch(API+'/api/models/degradation').then(r=>r.json());
          this.degData = this.parseDeg(deg);
        } catch(e) { /* 静默：轮询失败不打扰用户 */ }
      },
      // 降级状态归一：后端 {mapping_id:{degraded,remaining}}，兼容历史数组形态，统一以字符串 id 为键
      parseDeg(resp){
        const out={};
        if(!resp) return out;
        if(Array.isArray(resp)){
          for(const d of resp){
            const k = d.id!=null?d.id:(d.alias||d.model);
            if(k!=null) out[String(k)]=d;
          }
          return out;
        }
        if(typeof resp==='object'){
          for(const k of Object.keys(resp)) out[String(k)]=resp[k];
        }
        return out;
      },
      isDegraded(row){ const d=this.degData[row.id]; return !!(d && d.degraded); },
      degRemaining(row){ const d=this.degData[row.id]; return (d&&d.remaining!=null)?Math.max(0,Math.round(d.remaining)):0; },
      // 行底色：单元格透明，由 row-style 在 tr 注入 --alias-rgb + .07 底色；hover 由全局 CSS 提升至 .14
      rowStyle({row}){
        if(!row._aliasColor) return {};
        return { '--alias-rgb':row._aliasColor, background:'rgba('+row._aliasColor+',.07)' };
      },
      protoLabel(p){
        const ps=[]; if(p.anthropic_url) ps.push('Anthropic'); if(p.openai_url) ps.push('OpenAI');
        return ps.length ? (' ('+ps.join('/')+')') : '';
      },
      // 降级开关说明：用 Element Plus 内置 ElNotification 呈现（标题 + HTML 正文，4s 自动关闭）
      degToast(title, bodyHtml){
        ElementPlus.ElNotification({
          title,
          message: bodyHtml,
          dangerouslyUseHTMLString: true,
          type: 'info',
          duration: 4000,
          position: 'top-right',
        });
      },
      onDegEnabledChange(v){
        this.degSettings.enabled = v;
        if(!v){ this.degSettings.strict = false; }
        else {
          this.degToast('启用服务降级',
            '开启后，当某个模型映射对应的提供商持续返回错误（如 429 限流、5xx）时，系统会自动将该映射临时标记为<strong>降级</strong>状态，并在设定的<strong>降级持续秒数</strong>内跳过它、优先调用同一别名下的其他提供商，避免反复打到坏掉的上游。<br><br><strong>适用场景</strong>：同一别名配置了多个提供商做负载均衡，希望某个上游出故障时自动切到其他可用上游，而不是逐个失败重试。若每个别名只有一个提供商，开启降级意义不大。');
        }
      },
      onDegStrictChange(v){
        if(v){
          this.degToast('严格优先级（逐级下放）',
            '关闭（默认）时，系统在同一别名的<strong>所有优先级层</strong>之间<strong>加权轮询</strong>——优先级高的层获得更多流量，但低优先级层也会被使用。<br><br>开启<strong>严格优先级</strong>后，系统<strong>仅在当前可用的最高优先级层</strong>内选择；只有当整个高优先级层都已降级（全部提供商故障）时，才会<strong>逐级下放</strong>到下一优先级层。<br><br><strong>适用场景</strong>：希望主力模型优先用满、省钱模型只在主力全部不可用时才兜底；或不同优先级对应不同价位/质量，希望严格按层级使用，不愿低优先级分走流量。');
        }
      },
      async saveDegradation(){
        try {
          await fetch(API+'/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({
            degradation_enabled: this.degSettings.enabled?'1':'0',
            degradation_strict_priority: this.degSettings.strict?'1':'0',
            degradation_duration: parseInt(this.degSettings.duration)||30,
            degradation_retry_count: (parseInt(this.degSettings.retryCount)>=0 && !isNaN(parseInt(this.degSettings.retryCount)))?String(parseInt(this.degSettings.retryCount)):'3',
            degradation_retry_delay: (parseFloat(this.degSettings.retryDelay)>=0 && !isNaN(parseFloat(this.degSettings.retryDelay)))?String(parseFloat(this.degSettings.retryDelay)):'1',
          })});
          ElementPlus.ElMessage.success('已保存');
          this.dialogDeg = false;
          this.pollDegradation();
        } catch(e) {
          ElementPlus.ElMessage.error('保存失败');
        }
      },
      openDegradation(){ this.loadDegradation(); this.dialogDeg = true; },
      // 表格最大高度：实测 el-table 距视口顶部的位置动态计算（header/tabs/卡片头/描述文字全部自动计入），
      // 仅表格内部滚动（表头固定），避免整页滚动条。面板未激活(display:none → offsetWidth===0)时跳过，
      // 切回 models tab 时由 __reloadModels 触发重测。
      resizeTable(){
        this.$nextTick(()=>{
          const el = document.querySelector('#vue-models .el-table');
          if(!el || el.offsetWidth === 0) return;
          // 减去表格下方预留：卡片 padding-bottom(20) + 卡片 margin-bottom(16) + content padding-bottom(24) ≈ 60，+8 安全余量
          this.tableMaxHeight = Math.max(240, window.innerHeight - el.getBoundingClientRect().top - 68);
        });
      },
      syncEnabledLock(){ if(this.formEnabledLocked) this.form.enabled=false; },
      openCreate(){
        this.editingId = null;
        this.form = { alias:'', priority:1, targetModel:'', modelType:'text', maxTokens:0, providerId:(this.providers[0]&&this.providers[0].id)||null, enabled:true };
        this.syncEnabledLock();
        this.dialogModel = true;
      },
      openEdit(row){
        this.editingId = row.id;
        this.form = { alias:row.alias||'', priority:row.priority||1, targetModel:row.target_model||'', modelType:row.model_type||'text', maxTokens:row.max_tokens||0, providerId:row.provider_id, enabled:!!row.enabled };
        this.syncEnabledLock();
        this.dialogModel = true;
      },
      async submit(){
        if(!this.form.alias || !this.form.alias.trim()){ ElementPlus.ElMessage.warning('请填写别名'); return; }
        if(!this.form.providerId){ ElementPlus.ElMessage.warning('请选择提供商'); return; }
        this.submitting = true;
        try {
          const data = {
            alias: this.form.alias.trim(),
            priority: parseInt(this.form.priority)||1,
            target_model: this.form.targetModel,
            model_type: this.form.modelType,
            max_tokens: parseInt(this.form.maxTokens)||0,
            provider_id: parseInt(this.form.providerId),
            enabled: !!this.form.enabled,
          };
          const url = this.editingId ? API+'/api/models/'+this.editingId : API+'/api/models';
          const method = this.editingId ? 'PUT' : 'POST';
          const r = await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
          if(!r.ok){
            const j = await r.json().catch(()=>({}));
            ElementPlus.ElMessage.error(j.error||(this.editingId?'保存失败':'创建失败'));
            return;
          }
          this.dialogModel = false;
          this.load();
        } catch(e) {
          ElementPlus.ElMessage.error('保存失败');
        } finally {
          this.submitting = false;
        }
      },
      async toggle(id, enabled){
        try {
          const r = await fetch(API+'/api/models/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
          if(!r.ok){
            const j = await r.json().catch(()=>({}));
            ElementPlus.ElMessage.error(j.error||'切换状态失败');
            this.load();
          } else {
            this.load();
          }
        } catch(e) {
          ElementPlus.ElMessage.error('切换状态失败');
          this.load();
        }
      },
      async del(id){
        try {
          await ElementPlus.ElMessageBox.confirm('确定删除此模型映射？','提示',{type:'warning',confirmButtonText:'删除',cancelButtonText:'取消'});
        } catch(e){ return; }
        try {
          const r = await fetch(API+'/api/models/'+id,{method:'DELETE'});
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
      openCodex(row){
        this.codexForm = {
          mappingId: row.id,
          reasoning: !!row.reasoning_effort_supported,
          think: !!row.think_injection,
          reasoningField: !!row.reasoning_content_field,
          nativeResponses: !!row.native_responses,
        };
        this.dialogCodex = true;
      },
      // 三方互斥（think / reasoningField / nativeResponses）：开任一自动关另两个；reasoning（思考强度透传）独立
      mutexCodex(which, val){
        if(!val) return;
        if(which==='think'){ this.codexForm.reasoningField=false; this.codexForm.nativeResponses=false; }
        else if(which==='reasoningField'){ this.codexForm.think=false; this.codexForm.nativeResponses=false; }
        else if(which==='nativeResponses'){ this.codexForm.think=false; this.codexForm.reasoningField=false; }
      },
      async saveCodex(){
        if(!this.codexForm.mappingId) return;
        this.submitting = true;
        try {
          const r = await fetch(API+'/api/models/'+this.codexForm.mappingId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({
            reasoning_effort_supported: this.codexForm.reasoning,
            think_injection: this.codexForm.think,
            reasoning_content_field: this.codexForm.reasoningField,
            native_responses: this.codexForm.nativeResponses,
          })});
          if(!r.ok){
            const j = await r.json().catch(()=>({}));
            ElementPlus.ElMessage.error(j.error||'保存失败');
            return;
          }
          this.dialogCodex = false;
          this.load();
        } catch(e) {
          ElementPlus.ElMessage.error('保存失败');
        } finally {
          this.submitting = false;
        }
      },
      openRole(row){
        let rules = [];
        if(row.role_mappings){ try{ rules = JSON.parse(row.role_mappings); }catch(e){ rules=[]; } }
        if(!Array.isArray(rules)) rules = [];
        if(!rules.length) rules = [{from:'',to:''}];
        this.roleForm = { mappingId: row.id, rules: rules.map(r=>({from:r.from||'', to:r.to||''})) };
        this.dialogRole = true;
      },
      addRule(){ this.roleForm.rules.push({from:'',to:''}); },
      removeRule(i){ this.roleForm.rules.splice(i,1); },
      async saveRole(){
        if(!this.roleForm.mappingId) return;
        const rules = this.roleForm.rules
          .filter(r=>r.from.trim()&&r.to.trim())
          .map(r=>({from:r.from.trim(),to:r.to.trim()}));
        this.submitting = true;
        try {
          const r = await fetch(API+'/api/models/'+this.roleForm.mappingId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({role_mappings:rules})});
          if(!r.ok){
            const j = await r.json().catch(()=>({}));
            ElementPlus.ElMessage.error(j.error||'保存失败');
            return;
          }
          this.dialogRole = false;
          this.load();
        } catch(e) {
          ElementPlus.ElMessage.error('保存失败');
        } finally {
          this.submitting = false;
        }
      },
    },
    mounted(){
      this.load();
      this.loadDegradation();
      // 表格高度按视口自适应，仅表格内部滚动
      this.resizeTable();
      window.addEventListener('resize', this.resizeTable);
      // 降级轮询定时器迁入组件：仅当 models 面板激活时拉取，3s 一次
      this._degTimer = setInterval(()=>{
        const panel = document.getElementById('panel-models');
        if(panel && panel.classList.contains('active')) this.pollDegradation();
      }, 3000);
      // 跨面板桥接：暴露 loadModels（兼容阶段3提供商启停后刷新模型列表）+ __reloadModels（switchTab 切回时刷新）
      window.loadModels = ()=>this.load();
      window.__reloadModels = ()=>{ this.resizeTable(); this.load(); };
    },
    beforeUnmount(){
      if(this._degTimer){ clearInterval(this._degTimer); this._degTimer = null; }
      window.removeEventListener('resize', this.resizeTable);
      if(window.loadModels) delete window.loadModels;
      if(window.__reloadModels) delete window.__reloadModels;
    },
  });
  ModelsApp.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  if (window.ElementPlusIconsVue) {
    for (const [k, v] of Object.entries(ElementPlusIconsVue)) ModelsApp.component(k, v);
  }
  ModelsApp.mount('#vue-models');
}
