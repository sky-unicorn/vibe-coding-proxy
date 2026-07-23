// ---- Vue 岛屿：登录页（阶段 9）----
if (window.Vue && window.ElementPlus) {
  const LoginTemplate = `
<div class="login-card">
  <h1>Vibe Coding 服务转发</h1>
  <p class="subtitle">管理后台登录</p>
  <el-alert v-if="errorMsg" :title="errorMsg" type="error" show-icon :closable="false" style="margin-bottom:16px" />
  <el-form label-width="0" @submit.prevent="handleLogin">
    <el-form-item>
      <el-input v-model="username" placeholder="请输入用户名" :prefix-icon="UserIcon" clearable @keyup.enter="handleLogin" />
    </el-form-item>
    <el-form-item>
      <el-input v-model="password" type="password" placeholder="请输入密码" :prefix-icon="LockIcon" show-password @keyup.enter="handleLogin" />
    </el-form-item>
    <el-form-item>
      <el-button type="primary" :loading="loading" @click="handleLogin" style="width:100%">登录</el-button>
    </el-form-item>
  </el-form>
</div>
`;

  const LoginApp = Vue.createApp({
    delimiters: ['[[', ']]'],
    template: LoginTemplate,
    data(){
      return {
        username: '',
        password: '',
        loading: false,
        errorMsg: '',
        UserIcon: window.ElementPlusIconsVue ? window.ElementPlusIconsVue.User : null,
        LockIcon: window.ElementPlusIconsVue ? window.ElementPlusIconsVue.Lock : null,
      }
    },
    methods: {
      async checkAuth(){
        try {
          const r = await fetch('/api/auth/status');
          const data = await r.json();
          if(data.logged_in) window.location.href = '/';
        } catch(e) { /* 静默 */ }
      },
      async handleLogin(){
        this.errorMsg = '';
        if(!this.username.trim() || !this.password){
          this.errorMsg = '请输入用户名和密码';
          return;
        }
        this.loading = true;
        try {
          const r = await fetch('/api/auth/login', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ username: this.username, password: this.password }),
          });
          const data = await r.json();
          if(r.ok && data.ok){
            window.location.href = '/';
          } else {
            this.errorMsg = data.error || '登录失败，请检查用户名和密码';
          }
        } catch(err) {
          this.errorMsg = '网络错误，请稍后重试';
        } finally {
          this.loading = false;
        }
      },
    },
    mounted(){
      this.checkAuth();
    }
  });
  LoginApp.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  if (window.ElementPlusIconsVue) {
    for (const [k, v] of Object.entries(ElementPlusIconsVue)) LoginApp.component(k, v);
  }
  LoginApp.mount('#vue-login');
}