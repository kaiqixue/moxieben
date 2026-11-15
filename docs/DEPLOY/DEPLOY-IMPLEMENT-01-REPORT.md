# DEPLOY-IMPLEMENT-01 部署改造与验收报告

## 结论

**当前版本是可进入受限服务器验收的部署候选，尚不能认定可以安全向多位教师公网开放。正式发布停止。**

已实际修改代码并验证 Python/Linux/Gunicorn、教师隔离与配置解析；本机无 Docker 引擎，未完成镜像构建、容器挂载/权限/重启与目标 Ubuntu 22.04 ECS 验收。没有连接服务器，没有推送代码，没有迁移/修改正式库，没有上传真实学生图片，没有真实 OCR/VLM 调用。

## 1. 审计发现与修复

| 发现 | 本轮改造 |
|---|---|
| 本机 Host 限制阻止域名、没有生产 WSGI | production.py 显式环境配置，wsgi.py 调用；仅指定 HTTPS 来源可写，缺配置启动失败，可信 Host+单层代理 |
| 会话密钥随机，重启丢登录 | 文件固定密钥，Secure/HttpOnly/SameSite Cookie；已验证重启后会话可用 |
| 运行线程不适合多进程 | Gunicorn 单 worker、gthread 4 threads、禁止 preload/reload；后台仍保存 owner 并逐步验权，重启标 interrupted 由教师继续 |
| OCR 服务配置存 SQLite | 服务器强制 Paddle 云端，配置/Token 来自环境和只读文件；教师设置接口保持禁止；可选 VLM 独立文件 |
| 结果下载地址只有 HTTPS 检查 | 增加精确域名+公网 DNS 检查、禁止非 443/凭据 URL/重定向；网络出口策略仍待验证 |
| 无部署文件 | 非 root、只读根文件系统、临时目录、资源上限、日志轮转、持久绑定目录；默认仅回环端口 |
| 旧异常堆栈可能泄露第三方凭据 | 生产异常只记录 endpoint 与异常类型，不输出异常内容/堆栈；Gunicorn 不记录查询参数、Cookie |
| 无一致备份恢复工具 | backup_data.py 停写备份库与图片，SHA-256、完整性/外键/照片引用校验，只恢复新目录 |
| 公网教师删除操作会复制整库 | 生产临时禁止永久删除并明确说明，归档保留；本机原删除行为不变 |
| 版本/页面资源漏提交风险 | .gitignore 只为明确品牌图片增加例外，.dockerignore 使用输入白名单；不会包含 data/.env/secrets/Git |
| Pillow 12.3.0 安全扫描有漏洞 | 服务器依赖升级 Pillow 13.0.0；本机环境与 requirements.txt 未升级 |

代码依据：`app.py:create_app/local_only/tenant_route_guard/row/healthz`、`teacher_auth.py:teacher_gate/check_csrf/allow_preview_attempt/register_account_cli`、`grading_jobs.py:run_next/post`、`settings.py:read_settings/visual_review_config/_chat`、`paddle_cloud.py:recognize`、`task_deletion.py:delete_task`。原有对象行权限与数据库归属约束复用，不重写评分/提取/匹配。

## 2. 文件清单

- 新增运行文件：`production.py`、`wsgi.py`、`gunicorn.conf.py`、`requirements-server.txt`。
- 新增容器文件：`Dockerfile`、`.dockerignore`、`docker-compose.yml`、`Caddyfile`、`.env.example`。
- 新增备份与检查：`backup_data.py`、`test_production.py`、`test_server_runtime.py`。
- 修改：`app.py`、`teacher_auth.py`、`settings.py`、`paddle_cloud.py`、`task_deletion.py`、`templates/teacher_login.html`、`.gitignore`、`README.md`。
- 新增说明：本报告、`PRODUCTION-CONFIG.md`、`SERVER-OPERATIONS.md`。旧 DEPLOY 审计/架构/安全/Git 文档只追加版本现状提示，不覆盖历史依据。
- 页面必需资源 `static/images/teacher-desk.png` 被精确解除忽略；属于已有本地资源，不重新生成，发布前检查 Git 差异和来源。旧插画仍保持忽略，不作为新增发布资源。

## 3. 验证证据

所有数据库、账号和图片为临时合成数据，OCR 通过模拟替身。

| 检查 | 实际结果 |
|---|---|
| Windows 初轮完整回归 | 289 项，0 失败，1 跳过；后续新增生产项另跑 |
| 生产配置/权限/备份专项 | 9 项通过，包含配置拒绝、域名/Origin、Cookie、注册开关/限流、两教师、模拟云 OCR、导出、图片/会话重启、后台归属续跑、下载白名单、备份篡改检查、删除阻止 |
| Linux Python 3.11.17 安装服务器依赖 | 成功；不装 paddleocr/paddlepaddle，确认按需本地导入不阻止云端启动 |
| Linux 回归（升级前服务器依赖） | 291 项，0 失败，1 跳过；该结果不代替升级后最终回归 |
| Linux 最终完整回归 | Python 3.11.17 / Pillow 13.0.0 / Gunicorn 27.0.1，292 项，0 失败，1 跳过；含真实 Gunicorn 生命周期用例 |
| Python 依赖漏洞复扫 | pip-audit 针对 Linux 服务器环境（含测试 pypdf）扫描，无已知漏洞；只代表扫描时数据库覆盖，不含镜像 OS 层，也不是无漏洞保证 |
| Gunicorn 27.0.1 实际运行 | 回环地址启动、登录页、健康、SIGTERM 停止、重启和持久文件检查通过；可复用 test_server_runtime |
| Compose 配置 | 官方 standalone Compose v5.7.1 `--env-file .env.example config --quiet` 通过；工具 SHA-256 与官方清单匹配 |
| Caddy 配置 | 官方 v2.11.9 `validate --adapter caddyfile` 通过；工具 SHA-512 与官方清单匹配 |
| Docker 镜像构建与容器启动 | **未验证：当前无 Docker Engine**。未把 YAML/Caddy 检查当容器验收 |
| 正式 HTTPS/证书续期/安全组/域名 | **未验证**：没有连接或修改 ECS |
| 中文 PDF/Word | 现有文档导出回归通过；容器内字体嵌入和视觉效果必须再检查 |
| 真实 OCR/VLM、性能/费用 | **未验证**：本轮不调用真实服务、不使用真实学生照片 |

基础镜像从 Docker Hub registry 查询并固定 manifest digest，非凭空填入。Python 3.11 slim-bookworm 和 Caddy 2.11.9-alpine 的具体摘要见 Dockerfile/Compose。系统 apt 依赖和传递 Python 依赖并未完整锁定；最终构建镜像必须扫描并记录摘要。

## 4. 教师权限检查

复用 `test_tenant_isolation` 的 16 项测试和账号用例：跨教师班级、学生、名单、作业、答卷、图片、成绩 CSV、分析、记忆、教师改判/历史、后台任务/恢复均拒绝；同学号在不同教师下独立；无归属/旧全局唯一结构启动拒绝。生产专项在 HTTPS+Origin+Secure Cookie 下重跑关键闭环。

默认拒绝未列入 enabled 集合的业务路由，普通教师不能读取/修改设置或以 HTTP 头构造内部后台身份。平台管理员只用于 CLI 管理，不默认拥有教学数据权限。更全面的路由矩阵、浏览器和真实代理验收仍为发布门槛，不能用少量 ID 检查替代。

## 5. 尚未关闭的 P0 与发布阻塞

| 编号 | 阻塞 | 关闭证据 |
|---|---|---|
| P0-01 | Docker 完整构建/非 root secret 权限/卷持久化/重建/日志上限未实测 | 目标 Ubuntu 22.04 ECS 或等价受限 Docker 环境，执行构建与完整合成业务、停启和恢复演练 |
| P0-02 | 真正代理 HTTPS、证书、来源头、限流来源识别、安全组未验收 | 仅 Caddy 能到 app；伪造转发头无法绕过；真实浏览器两教师流程，证书续期与端口检查 |
| P0-03 | 供应商下载主机未确认、出口防 DNS 变化/云元数据访问未验证 | 专用合成图验证供应商完整域名，网络出口拒绝私网/环回/链路本地/元数据，并确认模型服务边界 |
| P0-04 | 公开注册只有进程内限流，重启清空；没有平台级费用/存储限额或人机/邮箱验证 | 首批访问规模/准入边界确定，入口防滥用与预算/存储限制、告警及账号管理验收；不满足保持注册关闭与受限访问 |
| P0-05 | 实例级磁盘/备份加密异地恢复与镜像 OS 漏洞未验收 | 部署镜像扫描，备份加密与异地恢复、容量告警、故障恢复演练通过 |
| P0-06 | 真实学生图片交付第三方服务与公网资质边界未确认 | 负责人确认学校/平台数据处理、保留政策及大陆正式域名/备案要求；先用合成数据 |

这些不是发现所有路径必然已存在漏洞，而是正式发布必须有证据关闭的门槛。**不能宣布“安全部署完成”。** 不主动更换 SQLite，不引入 Redis/Celery/Kubernetes；若后续实测写锁、队列延迟或多副本需要，再有依据升级。

## 6. 下一步

1. 产品负责人审核本轮代码、功能边界（生产暂禁永久删除、注册开关）及报告。
2. 选定提交并上传私有仓库；本轮未自动 commit/push。
3. 获得受限 Staging 操作授权后，按 SERVER-OPERATIONS 完成 Docker 构建与合成流程验收，不迁移本机正式数据。
4. 全部 P0 证据关闭后再申请正式发布。
