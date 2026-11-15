# Docker Compose 部署计划

> 更新：DEPLOY-IMPLEMENT-01 已新增 Dockerfile、docker-compose.yml、Caddyfile、wsgi.py 和配置/备份工具。下文保留原设计蓝图，不能据此认定全部完成；当前执行步骤见 [操作手册](SERVER-OPERATIONS.md)，实际验证和阻塞项见 [验收报告](DEPLOY-IMPLEMENT-01-REPORT.md)。

以下是后续实施蓝图。必须先完成权限清单中的 P0 项并在隔离环境验收。当前 `create_app()` 直接启动仍会加载本地默认配置，并拒绝公开域名。

## 镜像与服务

| 项目 | 实施约定 |
| --- | --- |
| 应用镜像 | Debian slim 系列 Python 3.11 镜像，锁定基镜像摘要。以非 root UID 运行，镜像包含 Python 源码、Jinja 模板、原生静态资源、生产依赖、CA 证书及可嵌入中文字体。首期云端镜像剔除按需加载的本地 Paddle 重依赖，先证实相关启动、健康页和设置行为正确。若需本地 OCR，单独验证镜像和模型缓存 |
| 入口 | 新建生产 WSGI 模块，通过环境变量或文件显式构建 `create_app(config)`；缺少必须配置应启动失败。参考进程命令 `gunicorn --workers 1 --worker-class gthread --threads 4 --bind 0.0.0.0:8000 wsgi:app`。保留单 worker 和唯一写实例，禁止热重载或滚动重叠 |
| 代理 | Caddy 只公开 80/443，经私有容器网络访问应用 8000，应用无宿主机端口映射。代理体积门槛与 Flask 的 10 MiB + 256 KiB 请求上限一致，登录和上传按账号或来源限速由应用或上游网关实现。耗时识别走后台，Staging 验证仍存在的同步维护路径和超时 |
| 挂载 | 每环境宿主机本地目录 `/srv/moxieben/<env>/data` → `/var/lib/moxieben`，容器内含 SQLite、uploads 及应用删除备份目录。Caddy 独立挂载 `/data` 与 `/config`。宿主机仅服务 UID 和备份主体可读写 |
| 环境配置 | 后续约定非密配置 `APP_ENV`、`APP_HOST`、`APP_ORIGIN`、`DATA_DIR`、`OCR_PROVIDER`、`PADDLE_OCR_URL`；会话密钥、Paddle Token、OpenAI 兼容 Key 通过专属 Compose secrets 文件挂载。生产 OCR 凭据和地址由运营配置管理，应用内设置仅对管理员开放必要只读状态 |
| 探活与停止 | 增加最简本地探活、数据库就绪检查，探活不调用 OCR。配置 `restart: unless-stopped`、日志轮转和资源上限。优先等待当前任务结束再停止，突发重启由已有中断状态支持手动继续。Docker 健康状态用于告警，单靠 healthcheck 不会触发容器自动重启 |

应用同时提供动态页面、同源 API 和静态资源。首版由代理转发所有路径，后续需要独立提供静态文件时，使用同一版本发布包的静态资源并做版本对齐。[Gunicorn 部署](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/) · [Docker 挂载](https://docs.docker.com/engine/storage/bind-mounts/) · [Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)

## 发布与回滚

1. 构建候选应用镜像，运行自动测试并发布到私有镜像库。记录 Git SHA、镜像摘要、配置版本及数据库兼容说明。清单与镜像先在 Staging 用合成数据重启和重建，验证数据卷保留、权限、中文 PDF/Word、OCR 失败与任务接续。
2. 经负责人批准后，预拉取同一摘要。正式发布进入维护窗口，阻断用户入口和新任务，确认后台队列、OCR 和正在写入请求已清空。停止应用时保持存储在线。创建并核验同一时点的数据库、图片和配置备份。
3. 仅启动新版本单个应用实例。应用工厂含启动时表结构初始化，必须记录启动前后兼容性。探活后用授权测试账号执行登录、读取、图片和导出核对，再恢复入口。监控应用日志、队列、磁盘与错误率。
4. 失败时封闭入口，保存失败现场。若无不兼容变更且旧镜像与现有数据经验证兼容，回切旧镜像和配置，保留新写入。若不兼容，用部署前同批数据库、图片及配置恢复到独立目录，验证后整体切换。开放入口后产生的写入需先评估取回方案，并由负责人接受恢复点差异，避免静默丢失后续批改。

实施时形成可复用的 Compose 文件、反代配置和脚本，并在 Linux 环境执行 `docker compose config --quiet`、镜像构建、启动及回滚演练。发布日志只记录摘要、状态和错误类别，配置渲染结果可能含秘密，不输出到 CI 日志。
