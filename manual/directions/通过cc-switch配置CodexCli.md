需要注意Codex Cli 新版本已经采用的不是OpenAiChat而是OpenAiResponses，所以这里需要注意不要使用 "/v1" 的转发地址。

请求地址配置成代理服务的 http://127.0.0.1:5000/openai（可通过管理界面右上角快速复制）

API Key 则是配置代理服务 "API Key" 界面创建的（非提供商大模型的key）

![](ScreenShot_2026-07-10_144521_293.png)

 可选配置项配置如图即可。

模型映射则配置，代理服务为目标模型起的“别名”

需要注意的1！！！！！！上游格式配置为 Responses( 不需要开启cc-switch路由再转一次)

![](ScreenShot_2026-07-10_144549_866.png)

需要注意的2！！！！！！！

因为Codex Cli 原本接入的是 Chat Gpt 模型的，最新模型对话里面新增了 “developer” ，国内有些coding plan 是不认“developer” 角色的。

可以通过代理服务 “模型映射” 界面中为某一个模型单独设置 “角色映射” 如下图

![](ScreenShot_2026-07-10_145437_531.png)