# DEPLOY-IMPLEMENT-01 服务器操作手册

状态：部署候选版本；正式发布被验收门槛阻止。以下命令在 Ubuntu 22.04 服务器执行，不是 Windows PowerShell。当前轮未连接阿里云、未执行公网发布、未搬运真实数据。

## 1. 架构与前提

浏览器 → Caddy HTTPS → Gunicorn 1 worker / 4 threads → Flask → SQLite 与私有 uploads；识别调用 Paddle 云端。页面和 API 同源，没有前端构建步骤。容器不安装本地 Paddle 模型，不支持在普通教师界面切换服务。应用不能扩容为多个副本，也不能使用 Gunicorn preload/reload/滚动重叠发布。

准备独立 Staging、独立域名、专用 OCR 凭据和空数据目录。安装 Docker Engine 与 Compose 插件，参照 Docker 官方 Ubuntu 安装文档：https://docs.docker.com/engine/install/ubuntu/ 。不要直接运行来源不明的安装脚本。检查 `docker version`、`docker compose version`。私有仓库访问使用仅具读取权限的凭据，不把 Token 拼进 URL 或命令历史。

## 2. 取得经过审查的版本

```bash
sudo install -d -m 0755 /srv/moxieben
sudo chown "$(id -u):$(id -g)" /srv/moxieben
git clone https://github.com/kaiqixue/moxieben.git /srv/moxieben/app
cd /srv/moxieben/app
git checkout <经过审查的提交或标签>
git status --short
```

不要把占位符原样执行。当前修改尚未自动提交或推送；确认代码、Docker 文件、`static/images/teacher-desk.png` 均在目标提交后再部署。不要上传 `.venv`、本地 `.env`、学生数据或旧生产库。

## 3. 初始化隔离目录与密钥

```bash
sudo install -d -o 10001 -g 10001 -m 0700 /srv/moxieben/staging/data
sudo install -d -o root -g 10001 -m 0750 /srv/moxieben/staging/secrets
sudo install -d -o root -g root -m 0700 /srv/moxieben/staging/backups
sudo sh -c 'umask 027; openssl rand -hex 32 > /srv/moxieben/staging/secrets/session_key'
sudo install -o root -g 10001 -m 0640 /dev/null /srv/moxieben/staging/secrets/paddle_token
sudo install -o root -g 10001 -m 0640 /dev/null /srv/moxieben/staging/secrets/visual_key
sudo chown root:10001 /srv/moxieben/staging/secrets/session_key
sudo chmod 0640 /srv/moxieben/staging/secrets/session_key
sudoedit /srv/moxieben/staging/secrets/paddle_token
cp .env.example .env
chmod 0600 .env
editor .env
```

`paddle_token` 文件只填写平台 Token，不带引号或说明。可选视觉服务不启用时 `visual_key` 保持空文件。Compose 的文件型 secret 是只读绑定，不提供自动加密，也不替你改变宿主文件权限；UID/GID 必须让应用 10001:10001 可读。`.env` 不放真实 Key。

修改 `.env` 中域名、数据路径、镜像版本。`PADDLE_RESULT_HOSTS` 必须是经供应商资料或合成图片接口验证确认的**完整结果下载主机名**，逗号分隔，不接受通配符。不要记录签名 URL 的查询参数，不要为图省事允许任意主机。当前未用真实 Token 调用，实际下载主机仍待平台负责人确认；域不匹配会安全失败而非放宽。

保留 `HTTP_BIND=127.0.0.1`、`HTTPS_BIND=127.0.0.1`、`CADDY_TLS=internal`、`REGISTRATION_OPEN=0`。这样仅供受限验收，**不会监听公网端口**。

## 4. 构建与启动（首次仅合成数据）

```bash
docker compose --env-file .env config --quiet
docker compose build --pull app
docker compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose up -d
docker compose ps
docker compose logs --tail=100 app
docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status)"
```

预期 app healthy，Caddy 正常。日志不应含 Key、姓名、OCR 原文。`healthz` 只检查数据库可读，不调用 OCR；healthy 不代表所有业务或供应商可用。只改 `.env` 后，`docker compose restart` 不会应用新环境，需 `docker compose up -d --force-recreate app`；先等待现有任务结束。

## 5. 受限浏览验收

在你的 Windows 终端使用 `ssh -N -L 443:127.0.0.1:443 <服务器用户>@<服务器地址>`（本机 443 必须空闲，必要时使用管理员终端）。本机 hosts 将测试域名指向 127.0.0.1，只在测试期间启用，访问 `https://测试域名/`。本轮 APP_HOST 不带端口，保持标准 HTTPS 域名与端口；不要关闭应用的 Host/Origin 校验来适配任意端口。受控 VPN 也是可选入口。

`internal` 使用 Caddy 私有 CA：将此隔离实例的 CA 公钥通过可信渠道导入测试浏览器信任；不要导出 CA 私钥，也不要对真实教师要求忽略证书错误。更方便的替代方案是已准备好的受限 VPN + 受信任证书。

受限验收时将 `REGISTRATION_OPEN=1` 并重建 app，使用两个虚构账号完整走注册、班级、导入、上传合成答卷、模拟/专用 OCR、评分、改判、报告、CSV/XLSX、PDF/Word。平台管理员通过 `docker compose exec app flask --app wsgi platform-admin` 初始化；普通教师不能配置平台密钥。管理员也不默认读取教学数据。

禁用账号：`docker compose exec app flask --app wsgi account-status <邮箱> --disable`。恢复用 `--enable`。密码找回和邮箱验证尚未实现，不能声称已有；试用期需明确人工身份核验支持流程。

## 6. 备份与恢复

先关闭业务入口（停止 Caddy），等待已受理任务和写请求完成。核对未完成任务后停止 app；不得强行中断正在收费 OCR 后立即重复提交。

```bash
docker compose stop caddy
# 等待后台任务完成；如需中断应明确记录待续任务。
docker compose stop app
sudo python3 /srv/moxieben/app/backup_data.py backup \
  /srv/moxieben/staging/data /srv/moxieben/staging/backups/<唯一备份编号> --app-stopped
sudo python3 /srv/moxieben/app/backup_data.py verify /srv/moxieben/staging/backups/<唯一备份编号>
docker compose start app caddy
```

工具仅使用 Python 标准库，支持 Ubuntu 22.04 自带 Python 3.10；也可使用已构建的 app 镜像执行并挂载数据/备份目录：

```bash
sudo docker run --rm --network none --user 0:0 --entrypoint python \
  --mount type=bind,src=/srv/moxieben/staging/data,dst=/source,readonly \
  --mount type=bind,src=/srv/moxieben/staging/backups,dst=/backups \
  <当前应用镜像标签或摘要> /app/backup_data.py backup /source /backups/<唯一备份编号> --app-stopped
```

工具备份 SQLite（Online Backup API）和 uploads，生成 SHA-256 清单，检查数据库完整性、外键和照片引用。应用必须停止，才能保证数据库与图片属于同一时点。应用删除备份不递归包入，避免无限增长；旧本机删除档案按单独政策保留。

恢复：`python backup_data.py restore <备份目录> <全新恢复目录> --app-stopped`，永不覆盖已有目录。恢复验证后设置属主 10001:10001，修改 DATA_PATH 指向恢复目录，并用匹配版本启动。不要仅恢复数据库而丢掉图片。配置、secrets 和 Caddy 证书卷另行加密备份，密钥与备份分开管理。建议 7 个日备份、4 个周备份、3 个月备份；本轮不自动删除任何备份。每日定时任务按相同停写流程执行，失败必须告警，不要无条件继续发布。

## 7. 更新和回滚

1. 先在独立 Staging 验收候选提交，构建带唯一 Git SHA 标签的镜像，记录镜像摘要；禁止把生产目录用于测试。
2. 维护窗口停止入口、等待任务、停止 app；生成并验证同批数据库/图片/配置备份。
3. 更新 APP_IMAGE，单实例启动新版本，检查健康、登录、跨教师权限、图片与导出；通过后恢复入口。
4. 回滚前判断数据库结构是否兼容。兼容才切回旧镜像；不兼容则恢复匹配快照到新目录。开放后新增写入须先保全，不能直接覆盖。
5. 禁止 `docker compose down -v`，它会删除 Caddy 证书卷；禁止删除或重建 DATA_PATH。

## 8. 正式发布闸门

仅在 DEPLOY-IMPLEMENT-01-REPORT 中的 P0 全部关闭且负责人批准后：使用正式独立数据/密钥/域名；完成备案与隐私处理要求；配置受信任证书，改 CADDY_TLS 为证书联系邮箱；核验安全组和防火墙后才改公网监听。届时只开放 80/443，SSH 限管理 IP，8000 不映射。先评估公开注册的邮箱验证/人机防滥用、平台费用/存储配额和错误告警；当前单进程内限流不能被视为完整公网防滥用系统。
