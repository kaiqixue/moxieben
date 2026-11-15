# DEPLOY-AUDIT-01 · 部署准备审计

> 历史基线文档：下列“无账号/无 Git/无部署文件”等结论描述审计当时状态，已经部分过时。当前代码新增教师账号与对象隔离，DEPLOY-IMPLEMENT-01 补齐生产入口和部署配置；以 [本轮实测报告](DEPLOY-IMPLEMENT-01-REPORT.md) 为准，尚未通过正式发布验收。

**结论：可以进入隔离测试环境的建设，当前版本不能直接面向多位教师上线。** 主要阻塞项是登录与对象权限、生产入口配置，以及后台任务在新权限体系中的执行身份。先保留 SQLite，采用单实例验证。

| 领域 | 已核实的现状 | 部署影响 |
| --- | --- | --- |
| 前端 | `templates/index.html` 加载 `static/` 下的原生 JS/CSS，页面通过同源相对路径访问 Flask JSON API，上传使用 multipart | 无独立前端构建工具或 Node 运行时要求。生产可独立提供静态资源，入口 HTML 仍由 Flask 动态生成 |
| 后端 | `start.bat` / `start.ps1` 使用 Windows 虚拟环境运行 `app.py`。入口为 `create_app(config=None)`，开发服务监听 `127.0.0.1:5000`，关闭 debug 与 reloader | Linux 镜像用生产 WSGI；保留现有 Windows 启动方式用于开发 |
| 配置 | `app.py:88` 的 `DATA_DIR`、请求限制和 CSRF 来自应用工厂配置，默认数据目录在项目内。现有工厂未读取生产部署所需的环境变量。OCR 设置在 SQLite `settings` 表 | 后续增加生产入口、受信任域名与独立密钥配置，明确环境配置优先级 |
| 存储 | `data/dictation.sqlite3`，本机只读查询显示 journal mode 为 `delete`。每请求 SQLite 连接启用外键，连接等待上限 30 秒。图片位于 `data/uploads/`，数据库保存文件名，原始 JPG/PNG 和用于展示/识别的 PNG 分开保存 | 将数据目录整体持久化到服务器本地块盘。不要公开挂载目录或让多个副本共享数据库文件 |
| 文件访问 | Flask 按记录 ID 返回答卷照片、旋转预览、批量照片及导出，参见 `app.py:568`、`teacher_workbench.py:259`、`batch_photos.py:350` | 图片、预览、报告、讲评、导出和历史都必须走对象权限检查 |
| 登录与权限 | `app.py:152` 校验本机 Host，写请求额外校验 Origin（若存在）和页面注入的进程级 CSRF。未发现用户登录或教师所有权校验。班级归档保护属于业务规则 | **P0**。Host 和 CSRF 不提供教师身份。同源内的已登录教师也需要按资源归属鉴权 |
| 凭据与外联 | `settings.py:26` 建立明文键值表，`public_settings` 对密钥仅返回配置状态。设置端点可修改识别服务地址并发起连通性测试。Paddle 结果下载验证 HTTPS，未见固定结果主机范围 | **P0**。部署模式外置凭据锁定设置变更，管控 API 及结果下载域名，备份按含密钥处理 |
| 上传与响应 | 单张照片上限 10 MiB，整次 HTTP 请求上限 10 MiB + 256 KiB。上传界面逐张提交，批量接口的 100 张计数上限与 HTTP 体积限制同时生效。图片验证 JPEG/PNG 内容及像素上限 2500 万，检查解压风险并使用随机文件名。Word/Excel 导入有独立解析和大小检查。应用已有 nosniff、DENY、same-origin Referrer-Policy、no-store | 保留限制，补齐配额、并发和存储告警。原始照片保留随文件上传的元数据，应纳入隐私管理 |
| CORS 与诊断 | 未发现 CORS 中间件或允许跨域响应配置。健康接口返回 OCR/存储/规则摘要。异常处理有通用响应及服务端堆栈。危险/运维功能含认领、修改、删除、重跑及云端测试 | 保持同源。公开探活只给最少状态，详细信息和维护接口仅供授权人员 |
| 路径与可移植性 | 业务文件以项目根或 DATA_DIR 为基准。Windows 启动脚本和一个个人 Word 样本测试依赖本机环境。PDF 查找 Windows/Linux 中文字体，另有回退 | Linux 镜像安装可嵌入的中文字体并验收 PDF。CI 换用合成 Word 测试件，核验现有 OCR JSON 样本来源 |
| 工具链 | 工作目录未初始化 Git。未发现项目 Dockerfile、Compose、.env 示例或 Node 前端构建清单，当前主机未发现 Docker 命令 | 私有仓库可建，首次提交必须使用白名单和人工复核。Linux 容器构建及公网行为待在 Staging 验证 |

本机核对到 Python 3.11.9。运行依赖 `requirements.txt` 为 Flask 3.1.3、Pillow 12.3.0、PaddlePaddle 3.3.1、PaddleOCR 3.7.0、openpyxl 3.1.5、defusedxml 0.7.1、python-docx 1.1.2、requests 2.34.2、ReportLab 5.1.0。开发测试增加 pypdf 6.21.4。版本是当前清单记录，生产发布时还须核验可安装性与安全更新。

## 识别与任务

- 当前设置为 `paddle_cloud`。Paddle 提交、轮询和下载结果，轮询预算 180 秒，结果上限 8 MiB，接续信息存于 `cloud_ocr_jobs`。请求提交和结果读流另有各自超时，180 秒不能视为完整批改或端到端硬上限。续跑复用可用的已提交任务，正式发布前需验证各类中断和重复计费边界。
- OpenAI 兼容接口以图片作为输入，后端调用 Chat Completions，现有网络超时参数 90 秒。当前业务代码外联主要集中在这两条识别路径。本地 Paddle 模型按需加载、CPU 串行，初次需要模型分发或可用缓存。首期生产建议 Paddle 云端，Staging 默认模拟。
- `grading_jobs.py:163` 起用进程内单个守护线程和 SQLite 状态、进度记录。启动初始化会把旧任务标为中断，由用户继续。锁只在进程内有效。后台当前通过 Flask `test_client` 和进程 CSRF 调用内部路由，这与浏览器登录会话不同。增加认证时，要把有权限的任务发起者和对象范围保存到任务，并用应用内部服务入口执行，禁止用公网可传入的请求头绕过认证。
- 首次识别、补识别、分题及成绩的诊断信息需分开记录。生产侧应统一记录脱敏的请求 ID、阶段、耗时、状态、可重试类别，避免通用报错被误当作额度或网络故障。识别调用可能涉及个人信息，应先确认学校允许的服务及处理范围。

## 上线门槛和顺序

1. **P0 权限与配置**。完成独立账号、资源归属、密钥外置和外联控制。验收未登录及跨教师读写，特别是图像、任务队列、下载与设置。
2. **P0 生产运行与恢复**。在 Staging 通过同源代理、单进程任务、数据持久化、备份恢复和版本回滚演练。先把合成数据跑通。
3. **P1 容量与运维**。按资源估算跑上传和批改压测，核对并发、配额、监控和实际供应商耗时。满足安全清单后由产品负责人决定是否进入正式发布。

本轮以静态代码、本机依赖和本地数据库只读元数据为证据。本机运行 `.venv/Scripts/python.exe -m unittest -q test_settings test_paddle_cloud test_grading_jobs test_answer_card`，36 项通过，覆盖配置、模拟 OCR、后台任务和答题卡。服务器 Linux 镜像、反向代理、真实多人登录、域名解析及恢复能力仍是待验收项。

产品负责人审核事项：确认首批试点教师及规模、可用的 OCR 服务与数据边界、服务器预算、恢复目标和发布责任人。审核通过后再开始基础设施与权限改造。

## 配套文档

- [部署架构与环境划分](DEPLOYMENT-ARCHITECTURE.md)
- [Docker Compose 部署计划](DOCKER-DEPLOYMENT-PLAN.md)
- [GitHub 与 CI/CD 计划](GITHUB-CICD-PLAN.md)
- [数据库与图片备份](DATABASE-AND-FILE-BACKUP.md)
- [生产安全验收清单](PRODUCTION-SECURITY-CHECKLIST.md)
- [服务器资源估算](SERVER-RESOURCE-ESTIMATE.md)
