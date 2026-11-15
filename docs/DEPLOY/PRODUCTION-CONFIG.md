# 生产配置说明

入口：`wsgi:app` → `production.create_production_app()` → 现有 Flask 工厂。Windows `start.bat/start.ps1` 保留，仅用于本机。

| 项目 | 用途和限制 |
|---|---|
| APP_ENV | 必须 staging 或 production，不产生任何开放公网行为 |
| APP_HOST | 单个准确域名，无协议、端口或通配符；浏览器写请求 Origin 必须为相同 HTTPS 来源 |
| DATA_DIR | 容器内 `/var/lib/moxieben`；独立持久目录，含 dictation.sqlite3 和 uploads |
| DATA_PATH | Compose 宿主机绝对路径，必须预先存在且 UID/GID 10001 可写 |
| SECRET_KEY_FILE | 固定会话签名密钥文件，至少 64 字符；重启保持，轮换会让浏览器会话失效 |
| PADDLE_OCR_TOKEN_FILE | 云端 Token 文件；不写入 SQLite，不向教师显示 |
| PADDLE_RESULT_HOSTS | 结果下载域名完整白名单；HTTPS/443、无重定向，拒绝解析到非公网 IP |
| REGISTRATION_OPEN | 0/1，部署默认 0，受限环境验证后再决定；开放仍有按来源/邮箱的进程内 5 分钟限流 |
| VLM_REVIEW_BASE_URL/MODEL | 可选；只接受管理员服务器配置的 HTTPS 服务，不经教师设置接口 |
| VLM_REVIEW_API_KEY_FILE | 可选视觉模型 Key 文件，配置视觉服务时必填 |
| VLM_REVIEW_AUTO | 0/1，默认 0；启用必须先配置视觉服务，原评分保护不变 |
| HTTP_BIND/HTTPS_BIND | 默认 127.0.0.1，未经审查不改公网 |
| CADDY_TLS | internal 只适用于受限测试；公网证书改为证书联系邮箱并满足域名与网络条件 |

生产强制禁用 debug/testing，Gunicorn 1 worker、4 threads、不 preload、不 reload。4GB 应用内存上限、256MB Caddy 上限，SQLite 单写实例。Compose 日志每文件 10MB、保留 3 份。应用异常只记 endpoint 与异常类型，不输出请求体、原异常消息或堆栈；访问日志不记查询参数/Cookie。

代理信任边界：只信 Caddy 的一层 X-Forwarded-For/Proto，不信 Forwarded Host/Prefix。app 没有宿主机端口映射；不要把 Gunicorn 直接暴露。公开 `/healthz` 仅返回 ok/unavailable，检查库可读；`/api/health` 仍受教师端权限限制。

出站校验在请求前检查精确域名与 DNS 公网地址，但 DNS 解析检查与 HTTP 客户端再次解析之间仍存在时间差；最终还应在主机/网络出口阻止环回、私网、链路本地和云元数据地址，结合指定供应商域名策略。公网发布前验证，不能把代码校验当作网络隔离的替代。

配置优先级：生产入口强制使用 secret 和环境中的云端配置，忽略旧 SQLite settings 的服务值，不自动删除旧值。导入旧数据前需要单独核对、清理和轮换历史凭据，本轮没有迁移。`.env.example` 只含占位符；不要运行会输出所有 secrets 的调试命令。
