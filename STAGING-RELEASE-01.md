# STAGING-RELEASE-01：教师自主注册上线前审计

审计日期：2026-11-21（Asia/Shanghai）  
项目目录：`E:\工作项目\ai批改`  
范围：本地代码、Git、部署定义与既有测试证据。**本轮不连接服务器、不访问业务数据库、不部署、不开放注册、不创建账号。**

## 1. 发布结论

**当前修复可作为受限 Staging 的候选代码，但尚不能批准实际发布或开启注册。**

存在一个已复现的发布验收阻断项：**干净 Git 导出的源码缺少被测试直接导入的脚本，默认测试发现产生 1 个导入错误。** 另外，Linux/Gunicorn 生命周期、候选镜像构建、服务器版本对应关系、数据库兼容副本和部署前备份仍须补齐。

| 用户问题 | 审计回答 |
| --- | --- |
| 当前代码能否作为候选版本？ | 可以进入候选审查；不能凭本机 294 通过直接放行部署，先解决干净源码测试不可复现问题。 |
| 哪些文件需纳入发布？ | 修复提交中的 `app.py`、`test_archive_security.py`；完整构建使用已审查版本的运行文件和依赖定义。测试源码包还需补齐第 3 节指出的必要脚本及经审查的测试资源。 |
| 是否有未解决安全问题？ | 本次归档状态泄露修复逻辑正确，未发现这一修复的剩余阻断缺陷；公开注册仍有邮箱未验证、进程内限流等边界，不能据此批准无访问限制的公网服务。 |
| 是否有数据库兼容风险？ | 本次提交不改变结构；既有启动代码仍执行条件式迁移和状态更新，服务器旧代码/结构与候选是否兼容待现场及隔离副本核验。 |
| 更新容器能否保留数据？ | 能，前提是使用原项目及准确的原宿主数据挂载，备份通过、版本兼容，不复制空库、不改数据路径。不能承诺启动后所有字段逐字不变。 |
| 下一步如何部署？ | 第 8 节列出待批准命令，先部署修复并保持注册为 0；验收通过后另行确认开启注册。 |
| 哪些步骤需人工确认？ | 第 10 节列出发布、数据、备份、停机、配置和注册开放确认点。 |

## 2. Git 现场与完整变更范围

本轮检查到的事实与较早聊天记录不同，应以当前 Git 为准：

- 分支：`feature/mp-02a-teacher-accounts`。
- HEAD：`55edb3840a1c56698c6656e8b6778854aeec02d7`。
- 提交标题：`fix(auth): check tenant ownership before archive write guard`。
- 父提交：`72427e6a19ff7a391866972a964aa9352e1457d0`。
- 审计开始时 `git status --short` 为空，暂存区为空；修复和安全测试均已被当前提交跟踪。
- 本轮没有执行 commit、push 或远程同步；不能从本地提交记录推断服务器或远端分支已经更新。
- 本报告生成后预计仅新增未跟踪的 `STAGING-RELEASE-01.md`，不属于运行代码变更。

| 相对父提交的文件 | 差异 |
| --- | --- |
| `app.py` | 增加 30 行、删除 13 行，仅修改 `archived_assignment_read_only()`。 |
| `test_archive_security.py` | 新增 115 行、4 个回归测试。 |
| 其他文件 | 此提交无其他变更。 |

已检查 `git diff 72427e6 HEAD -- app.py` 的完整内容；并以 Python AST 比较验证：将该保护函数的函数体排除后，新旧 `app.py` 的语法树完全相同。`git diff --check` 通过。

### 2.1 归档保护修复

原逻辑：解析目标关联作业及归档状态 → 可能返回 409 → 业务函数随后才检查资源归属。

现逻辑：

1. 为既有六类来源增加表名，复用统一的 `row()` 授权。
2. 在获取写锁及读取归档状态前，检查 URL 主资源归属；任务删除、班级更新也先授权。
3. 对答卷关联、照片确认及改选接口，先检查请求体中的目标名单归属。
4. 对解析出的关联作业、班级继续授权，然后执行原有锁和归档保护。
5. 不存在或其他教师的资源返回相同 404；本人已归档资源保持原 409，本人未归档资源可继续操作。

答卷锁、按班级编号排序的锁顺序及 teardown 释放逻辑保留。修改没有触及 Origin、CSRF、会话、角色或其他业务函数。

代码定位：`app.py:363`、`:396`、`:428`、`:455`；测试定位：`test_archive_security.py:57`、`:72`、`:87`、`:103`。

## 3. 发布文件与已复现的测试打包缺口

### 3.1 已确认阻断项：干净源码缺少必要测试依赖

- `test_migrate_single_teacher.py:15` 无条件导入 `scripts.migrate_single_teacher`。
- `scripts/migrate_single_teacher.py` 在本机存在，但未被 Git 跟踪，被 `.gitignore:32` 的 `/scripts/*` 忽略。
- 将 **HEAD 通过 `git archive` 导出到独立临时目录**，仅执行 unittest discovery，并禁止数据库连接，得到：

```text
discovered = 296
import_errors = 1
ModuleNotFoundError: No module named 'scripts.migrate_single_teacher'
```

这里的 296 包含失败加载占位项，不能解释为 296 项均可正常运行。未在这个干净导出上运行完整测试，也未执行任何迁移函数。

**发布前最小处理建议（本轮未实施）：** 审查并精确纳入这个必要脚本，或调整测试依赖组织；保留现有测试，不用删除或跳过测试来消除错误。若需要修改 `.gitignore`，只添加必要的例外，不放开整个 scripts 目录。新增文件会形成新的候选提交，之后必须重新验证干净检出。

### 3.2 测试样本不能随本机默认存在

以下文件本机存在，但均被 `.gitignore:28` 忽略：

- `tests/fixtures/grammar_inline_ocr.json`。
- `tests/fixtures/grammar_region_upright.json`。

它们缺失会让相关 OCR 回归增加跳过项；此前本地的“294 通过、2 跳过”不能原样代表干净 Git 源码。应先人工审查样本是否包含真实个人信息、是否可发布，再采用精确 Git 白名单、受控测试资源包或经批准的脱敏样本。不要直接强制加入整个 fixtures 目录。

`tests/fixtures/grammar_sample.docx` 在本机也缺失，是既有 Word 测试跳过的原因，不是本次修复删除了文件。

### 3.3 源码测试包与运行镜像分开

| 内容 | 是否应纳入 |
| --- | --- |
| `app.py` 与 `test_archive_security.py` | 都保留在发布源码版本中；安全测试已经跟踪。 |
| 运行模块、templates、static、`requirements-server.txt`、Dockerfile | 由已审查完整版本构建，不能只把两个差异文件当成完整应用。 |
| Compose、Caddyfile、运维/备份定义 | 保留为版本化部署依据，不覆盖服务器实际 `.env` 和 secrets。 |
| 必要测试脚本和经授权的测试样本 | 供干净源码/CI 验证；无需进入运行镜像。 |
| 本审计报告 | 可作为审批材料，不需要进入运行镜像；本轮不提交。 |
| `.env`、私钥、Token、数据库、uploads、备份、outputs、虚拟环境、缓存 | 禁止进入源码发布包或镜像构建上下文。 |

已扫描 Git 跟踪清单，未见数据库、真实 `.env`、私钥、日志、业务输出等列入跟踪；`.env.example` 是模板，`static/images/teacher-desk.png` 是已跟踪的页面资产，不应误删。

## 4. 注册、认证及教师隔离安全审查

| 检查项 | 代码结论与位置 |
| --- | --- |
| 页面与 POST | `/teacher/register` 同时提供 GET 页面和 POST 表单处理，不是独立 JSON API；模板要求姓名、邮箱、密码及确认密码。`teacher_auth.py:254`、`templates/teacher_login.html:40`。 |
| 注册开关 | 关闭时返回 403，登录模板不显示注册链接；无邀请码或人工审批流程。`teacher_auth.py:258`、模板 `:69`。 |
| 教师角色 | 注册 SQL 固定插入 `'teacher'`，不接受客户端提升为管理员。`teacher_auth.py:278`。 |
| 密码 | 8–128 字符、两次一致；通过 Werkzeug `generate_password_hash()` 保存，登录用 `check_password_hash()`；没有明文保存路径。`teacher_auth.py:193`、`:274`。 |
| 邮箱唯一性 | 去首尾空格、转小写、格式校验；数据库 `UNIQUE` 配合 `ON CONFLICT(email) DO NOTHING`，重复返回 409。`:51`、`:77`、`:278`。 |
| 注册后登录 | 创建服务器会话，清理/轮换浏览器会话，303 到 `/`；不是纯账号预览模式下的空壳流程。`:107`、`:115`、`:282`；生产入口启用 tenant mode。 |
| 会话 | 会话摘要存库，检查到期和禁用状态；Cookie 为 HttpOnly、SameSite=Lax，生产配置 Secure。`:71`、`:97`、`production.py:65`。 |
| 来源与 CSRF | 写请求先严格匹配 HTTPS `PUBLIC_ORIGIN`，再比对会话 CSRF Token；不以 Referer 兜底，也不信任任意 Origin。`app.py:145`、`teacher_auth.py:175`。 |
| 角色边界 | 身份从有效会话读取；普通教师上下文来自服务端。平台管理员不自动取得所有教学数据。`:145`、`app.py:333`。 |
| 资源归属 | 列表按 owner_id 过滤，单项经 `row()` 检查归属，页面依答卷归属授权；主要关联有数据库约束。`app.py:256`、`:455`、`teacher_workbench.py:195`。 |
| 后台任务 | 保存并恢复原教师身份，执行时检查账号和关联资源；内部标记不能由普通 HTTP 头构造。`teacher_auth.py:16`、`grading_jobs.py:250`。 |
| 归档防泄露 | 本次修复先授权后检查状态，覆盖主资源与目标名单；404 响应内容一致。安全测试覆盖七类主资源、三类关联接口、班级更新、本人归档及恢复。 |

### 4.1 尚存安全边界

1. 注册没有邮箱所有权验证；不应把填写邮箱解释为已验证身份，可能发生邮箱被抢注。密码找回也未实现。
2. 注册限流为同 IP 每 300 秒最多 8 次，登录按 IP/邮箱限流，状态在单进程内存中；重启清零，不支持多副本共享。当前部署必须维持单 worker/单实例。
3. 本次检查的注册链路未见验证码和按教师总费用/存储配额控制。开启注册会允许新教师使用有成本的 OCR 等能力，公开公网前需另行评估。
4. “没有发现本次修复的阻断缺陷”不等于完整渗透测试或所有接口的安全保证。待发布镜像仍须做双教师隔离验收。

这些边界允许作为受限 SSH 隧道/VPN 下的 Staging 验收项目；**不构成无访问限制公网开放的批准**。不得为解决登录或端口问题关闭 Host、Origin、CSRF 或租户权限校验。

## 5. Docker 与配置传递

### 5.1 镜像构建

- Dockerfile 使用固定摘要的 Python 3.11 Debian 基础镜像，安装 `requirements-server.txt`，复制根目录 Python 模块、templates 和 static；以 UID/GID 10001 运行 Gunicorn。
- `.dockerignore` 默认拒绝，仅放行根目录 Python、服务依赖定义、模板、静态资产和 Dockerfile；`test_*.py` 排除。运行镜像不带测试是预期行为，不能在该镜像中直接声称跑过完整 unittest。
- 构建步骤不导入 `wsgi`，不挂载业务数据，不把本机数据库烘焙进镜像。
- 基础镜像虽固定，apt 包和传递依赖仍可能随构建时间变化。需要验收实际镜像并记录镜像 ID/摘要，不重复使用 `latest` 表示不可变发布版本。
- 本机未发现 Docker 命令；本轮没有实际构建或启动候选镜像。

### 5.2 数据路径

| 层级 | 路径/含义 |
| --- | --- |
| 服务器 `DATA_PATH` | 用户提供的现场结果为 `/srv/moxieben/staging/data`，由 Compose 插值用于 bind mount。 |
| 容器 `DATA_DIR` | Compose 固定传入 `/var/lib/moxieben`，应用从这里打开业务库。 |
| 数据库 | 容器 `/var/lib/moxieben/dictation.sqlite3` 对应宿主 `/srv/moxieben/staging/data/dictation.sqlite3`。 |
| 照片 | 同一数据目录下 `uploads/`，必须与数据库同一恢复点保全。 |

Compose 使用 `create_host_path: false`，能阻止不存在的挂载目录被自动创建；但如果误选一个已经存在的空目录，应用仍可能初始化新库。因此不能只看 Healthy，必须核对路径和原数据库文件。

### 5.3 注册开关的完整链路

```text
Compose 插值来源（命令进程环境 / --env-file / 默认值）
  → services.app.environment.REGISTRATION_OPEN
  → 容器实际环境变量
  → production.load_config()（只接受字符串 0 或 1，默认 0）
  → TEACHER_REGISTRATION_OPEN 布尔配置
  → 登录页注册链接 + GET/POST 注册是否允许
```

 shell 环境变量可能覆盖 env 文件，不能仅检查文件文字。实际部署要审查解析后的白名单字段，并核对运行容器值。仅编辑 env 文件或执行 `restart` 不会更新已有容器环境，需要重建 app。

### 5.4 只更新 app

`up --no-deps --no-build --pull never --force-recreate app` 可用预先构建好的候选镜像，仅重建 app；保持同一 Compose 项目、数据挂载和原 secrets。Caddy 仅为维护窗口 stop/start，不替换其镜像或配置，不删除其证书卷。

参考：[Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)、[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)。

## 6. 数据库启动行为与兼容风险

**无新增迁移，不代表无启动写入。**

- `app.py:192` 注册认证模块时会执行 `CREATE TABLE IF NOT EXISTS`。
- `app.py:198` 起创建缺失表；`:241` 将运行中的页面识别标记为中断错误；`:249` 起调用各模块的条件式迁移。
- `automatic_grading.py:19` 起可能补字段、整理人工编辑标记、将运行中答卷标记为中断失败、调整补识别状态。
- `batch_photos.py:53` 整理中断的身份识别状态；`grading_jobs.py:146` 将 queued/running 任务标记为 interrupted。
- tenant 初始化还会创建归属约束、索引和触发器；旧结构及未归属数据有拒绝启动的检查。

如果服务器确实是父提交相同结构，预期无需为本补丁变更结构；**服务器 `latest` 对应哪个提交尚未确认，不能直接认定这一前提成立**。部署前应对授权的独立副本进行 schema 前后比较、关键业务记录及图片引用核对。副本测试也会触发既有启动写入，必须隔离，不能拿正在运行的业务库做试启动。

教师账号数量为 0 不能证明没有班级、答卷、成绩或其他重要数据，仍需完整备份。不要自动运行旧单教师迁移脚本，也不要为通过启动检查清空或重新分配业务数据。

## 7. 测试证据与 Linux 补齐方式

### 7.1 已核对的证据

- 先前 DEPLOY-02A 在独立临时源码副本完整执行 `python -m unittest discover -q`，最终 296 计数、294 成功、0 失败、0 错误、2 跳过，退出码 0。
- 本轮重新对照先前测试源码 SHA-256 清单，**89 个被测文件无变化**；这份结论包含本机被忽略的必要脚本和样本。
- 本轮仅重新发现测试，没有执行数据库测试：当前本机发现 296 项、4 个新增安全测试、导入错误 0；数据库连接已被禁止。
- 干净 HEAD 导出发现 296 计数但有 1 个导入错误，见第 3 节。不能将本机成功结果等同于干净发布包成功。

| 原先跳过的测试 | 原因及补齐方式 |
| --- | --- |
| `test_app.WebsiteTests.test_word_preview_extracts_grammar_blanks_without_writing` | 本机缺少 `tests/fixtures/grammar_sample.docx`。由负责人提供经授权、脱敏且符合测试断言的样本后补跑；不能伪造一个不对应断言的空文件。 |
| `test_server_runtime.ServerRuntimeTests.test_single_worker_start_stop_restart_preserves_private_data` | 当前 Windows；测试要求 Linux 并安装 Gunicorn。到独立 Linux 测试机或隔离 CI，使用候选源码和临时目录补跑。 |

### 7.2 Linux/Gunicorn 待执行命令

以下只针对**独立测试机/CI**，不是当前 Staging 应用容器；先解决干净源码缺失文件问题。依赖安装使用获准的软件源或预先准备的 wheel 包，不使用真实 OCR 凭据。

```bash
# 在独立、完整、干净的候选源码目录执行
python3 -m venv /tmp/moxieben-release-test-<唯一编号>
. /tmp/moxieben-release-test-<唯一编号>/bin/activate
python -m pip install -r requirements-server.txt pypdf==6.21.4

# 避免继承业务路径/凭据，并防止回环请求经代理转发
unset DATA_DIR SECRET_KEY_FILE PADDLE_OCR_TOKEN_FILE VLM_REVIEW_API_KEY_FILE
unset MOXIEBEN_TENANT_DIR MOXIEBEN_ACCOUNT_PREVIEW_DIR
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY=127.0.0.1,localhost
export REGISTRATION_OPEN=0
export PYTHONDONTWRITEBYTECODE=1

python -m unittest -v test_server_runtime
python -m unittest discover -q
```

完整测试使用各测试自身的临时数据库及模拟供应商；独立执行环境还应禁止外网出口，只允许测试所需的回环访问。Gunicorn 测试会启动临时子进程和本机随机端口，不能采用之前 Windows 测试那种连回环 bind 也全部禁止的策略。测试会为临时库生成合成凭据，不继承服务器 secrets、不挂载 Staging 的 data 或 secrets。

注意：测试中的 `REGISTRATION_OPEN=1` 仅作用于临时测试配置，不等于开启服务器注册。运行镜像未带测试，故不能用 `docker exec` 对业务容器运行这套测试。

## 8. 待批准的 Staging 部署命令

**以下是方案，不是本轮执行记录。第 3 节阻断项及 Linux/镜像/数据兼容验收未解决前，不执行以下变更命令。所有尖括号均须填入核验值，不可原样复制。**

### 8.1 先只读确定现场配置位置和旧镜像

在已解锁的 SSH 管理终端执行，只打印白名单字段：

```bash
sudo docker inspect --format 'PROJECT={{index .Config.Labels "com.docker.compose.project"}} WORKDIR={{index .Config.Labels "com.docker.compose.project.working_dir"}} CONFIG_FILES={{index .Config.Labels "com.docker.compose.project.config_files"}} ENV_FILES={{index .Config.Labels "com.docker.compose.project.environment_file"}} IMAGE_ID={{.Image}}' moxieben-staging-app-1
sudo docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/moxieben"}}DATA_PATH={{.Source}} CONTAINER_PATH={{.Destination}}{{end}}{{end}}' moxieben-staging-app-1
sudo docker exec moxieben-staging-app-1 python -B -c 'import os,json; print(json.dumps({k:os.environ.get(k) for k in ("APP_ENV","REGISTRATION_OPEN","DATA_DIR")}))'
```

确认项目为 moxieben-staging，数据路径与第 5 节一致。若存在多个 Compose 文件或 env 文件，必须保留真实的顺序及作用范围，不能套用下方单文件示例。不要输出完整 inspect JSON、完整环境变量或 `.env` 内容。

用户上一轮现场提供的旧镜像 ID 为：

```text
sha256:38b0a1b5e7782c060ff97825cb32f621b2c01997e4fca03218be55b92a6d3337
```

该值和 `latest` 引用仅为既有现场证据，部署当天必须重新核实；本轮未读取远程容器源码，也未证明旧镜像就是本地父提交。

### 8.2 准备候选、旧镜像回滚标签和变量

```bash
# RELEASE_DIR 必须是解决验收阻断后的、已审查完整提交的独立源码目录
RELEASE_DIR='<已准备并核对的候选源码绝对路径>'
COMPOSE_DIR='<上一步核实的部署工作目录>'
COMPOSE_FILE='<上一步核实的单个Compose文件绝对路径>'
ENV_FILE='<现场确认实际使用的env文件绝对路径>'
NEW_IMAGE='moxieben:staging-<候选提交短SHA>'
OLD_TAG='moxieben:rollback-<唯一编号>'
BACKUP_DIR='/srv/moxieben/staging/backups/<唯一编号>'

OLD_IMAGE_ID=$(sudo docker inspect --format '{{.Image}}' moxieben-staging-app-1)
sudo docker image tag "$OLD_IMAGE_ID" "$OLD_TAG"
sudo docker build -t "$NEW_IMAGE" "$RELEASE_DIR"
sudo docker image inspect --format '{{.Id}}' "$NEW_IMAGE"

dc() {
  sudo docker compose -p moxieben-staging --project-directory "$COMPOSE_DIR" \
    -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}
```

构建前先在独立源码目录核对 `git rev-parse HEAD`、干净状态和第 7 节测试结果。镜像构建上下文不能是业务数据目录。不覆盖服务器 `.env`，不把本机 data 上传到服务器。

### 8.3 维护窗口、停写及一致备份

```bash
dc stop caddy
```

入口停止后，以只读运维检查确认 queued/running 后台任务、running 页面/答卷、identifying 照片和已受理写请求均已结束。没有现成的“一条命令证明全部空闲”的接口；需要值守确认，不能因为普通教师数量为 0 就跳过。不得强停收费 OCR 后自动重发。

确认空闲后：

```bash
dc stop app
sudo test -f /srv/moxieben/staging/data/dictation.sqlite3
sudo test ! -e "$BACKUP_DIR"
sudo python3 "$RELEASE_DIR/backup_data.py" backup \
  /srv/moxieben/staging/data "$BACKUP_DIR" --app-stopped
sudo python3 "$RELEASE_DIR/backup_data.py" verify "$BACKUP_DIR"
```

`backup_data.py` 只读源库，用 SQLite Backup API 生成目标快照、复制 uploads、检查完整性/外键/图片引用，生成 SHA-256 清单；备份目标为新目录。`--app-stopped` 只是操作者声明，不会自动检测其他写入进程。校验失败即停止发布，保持数据不变，不删除失败备份。

同时以受限权限保全旧 Compose/Caddy 配置、实际 env 文件、UID/GID/权限和旧镜像标识；secrets 和证书按既有安全方式单独加密保全，不输出其内容。备份工具不递归打包历次业务删除备份，若需保留该类档案应另行清点。

### 8.4 仅更新 app，注册继续关闭

人工批准后，在实际 env 文件中**仅更新镜像选择，保留注册为 0**。保留原权限，其余值尤其 DATA_PATH、SECRETS_PATH、APP_HOST、项目名不变；不复制 `.env.example` 覆盖原配置：

```bash
sudoedit "$ENV_FILE"
# 只调整 APP_IMAGE 为上一步 NEW_IMAGE 的实际标签
# 确保 REGISTRATION_OPEN=0；不要在终端输出整份文件

dc config --quiet
dc config --format json | python3 -c '
import json,sys
s=json.load(sys.stdin)["services"]["app"]; e=s["environment"]
assert e["APP_ENV"]=="staging", "Unexpected environment"
assert str(e["REGISTRATION_OPEN"])=="0", "Registration must stay closed"
assert e["DATA_DIR"]=="/var/lib/moxieben", "Unexpected DATA_DIR"
v=next(v for v in s["volumes"] if v["target"]=="/var/lib/moxieben")
assert v["type"]=="bind" and v["source"]=="/srv/moxieben/staging/data", "Unexpected data mount"
print(json.dumps({"image":s["image"],"APP_ENV":e["APP_ENV"],"REGISTRATION_OPEN":e["REGISTRATION_OPEN"],"DATA_DIR":e["DATA_DIR"],"DATA_PATH":v["source"]}))
'
```

人工核对解析出的 image **正好等于** NEW_IMAGE；若受 shell 环境覆盖、文件叠加或路径不符，停止，不启动。

```bash
dc up -d --no-deps --no-build --pull never --force-recreate app
sudo docker inspect --format 'STATUS={{.State.Status}} HEALTH={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} IMAGE_ID={{.Image}}' moxieben-staging-app-1
sudo docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/moxieben"}}{{.Source}} -> {{.Destination}}{{end}}{{end}}' moxieben-staging-app-1
sudo docker exec moxieben-staging-app-1 python -B -c 'import os,json; print(json.dumps({k:os.environ.get(k) for k in ("APP_ENV","REGISTRATION_OPEN","DATA_DIR")}))'
```

等待 app 为 healthy，核对其实际镜像 ID 与预先记录的候选 ID 一致、数据挂载一致、注册为 0，再执行：

```bash
dc start caddy
```

保留标准 HTTPS 443 入口和受信任证书；不要重新引入 8443 Origin 不一致，也不要跳过认证防护。通过浏览器检查 healthz、登录页、注册仍关闭，并用已有获准账号/独立合成验收环境验证本人数据、照片、成绩、导出与跨教师 404。账号为 0 的环境无法完成双教师验收，需在后续批准的注册测试阶段补齐，不能本轮擅自造账号。

### 8.5 单独批准后才开启注册

修复镜像与数据保留验收通过后，另行确认开放范围仍为受限 Staging，确认维护窗口及备份仍有效。再编辑同一 ENV_FILE，将 `REGISTRATION_OPEN=1`，保留已固定的 APP_IMAGE 和其他配置。

```bash
# 仅在另行批准开启注册后执行
sudoedit "$ENV_FILE"
dc config --quiet
dc up -d --no-deps --no-build --pull never --force-recreate app
sudo docker exec moxieben-staging-app-1 python -B -c 'import os; print("APP_ENV="+os.environ["APP_ENV"]); print("REGISTRATION_OPEN="+os.environ["REGISTRATION_OPEN"])'
```

开启前也须核对解析配置只改变预期字段，不依赖文件文字猜测运行值。验证登录页出现注册链接、注册 GET 返回 200；随后由获准人员使用虚构账号验证注册成功、重复邮箱 409、普通教师角色、重新登录和双教师隔离。注册和成功登录都会写库，此步骤不属于本轮审计。

## 9. 回滚方案

1. 若候选启动失败或数据/权限验收异常，停止入口，保全日志和当前数据，不开放注册。
2. 等待写请求/任务结束后停止 app，将 ENV_FILE 的 APP_IMAGE 改回保留的 OLD_TAG，REGISTRATION_OPEN 保持 0，再按同一项目和挂载重建 app。
3. 首选仅回滚镜像，保留当前数据目录，避免丢失部署后新增数据。回滚前确认旧镜像可读当前结构；本补丁没有新增迁移不等于可忽略其他版本差异。
4. 若确需恢复备份，另行确认后使用 `backup_data.py restore` 恢复到全新目录，验证并设置原 UID/GID 权限，再切换挂载。绝不直接覆盖现库或只恢复数据库遗漏 uploads。
5. 回到旧镜像会重新引入此次权限缺陷；保持受限入口，不能把回滚当作最终修复。

```bash
# 经确认的代码回滚示例；实际 env 中改 APP_IMAGE 为 OLD_TAG，注册置 0
dc stop caddy
# 确认写入及任务结束
dc stop app
sudoedit "$ENV_FILE"
dc config --quiet
dc up -d --no-deps --no-build --pull never --force-recreate app
# 健康、镜像、挂载及数据核验通过后，决定是否恢复入口
```

所有阶段禁止 `docker compose down -v`、删除/重建 DATA_PATH、数据库初始化、自动生产迁移、重建密钥、未经确认覆盖数据或重启服务器。

## 10. 人工确认点与待现场核验

| 阶段 | 确认内容 |
| --- | --- |
| 发布源码准备 | 审查并补齐缺失脚本/测试资源的最小方案，检查样本隐私；形成可复现的新候选提交。当前 55edb38 不自动等于最终发布 SHA。 |
| Linux 与镜像验收 | 补齐 Gunicorn 测试、记录额外跳过项；构建候选镜像并确认实际依赖、启动与权限。 |
| 服务器版本与配置 | 真实 Compose/env 文件及叠加顺序、当前镜像 ID/源码对应版本、原数据和 secrets 挂载、单实例运行、注册仍为 0。 |
| 数据兼容副本 | 另行授权取得隔离副本，不在业务库试启动；关闭 OCR 外联，检查 schema、关键记录、图片引用与预期状态变化。 |
| 备份与维护窗口 | 可用空间、所有写入者停止、备份/校验成功、旧镜像及原配置已保全；失败立即停止发布。 |
| 部署执行 | 明确批准仅 Staging 的 app 镜像及相应配置更新；不顺带更新 Caddy、开放公网或迁移旧库。 |
| 注册开放 | 修复镜像上线确认后单独批准从 0 改为 1；确定测试账号、费用和访问范围。 |
| 数据恢复 | 只有确需恢复时另行确认目标新目录、恢复点和部署后新增数据的保全方式。 |

用户提供的上一轮现场证据：Ubuntu 24.04.2 LTS、app running/healthy、Staging 环境、注册为 0、数据挂载正确、users 表存在、普通教师数量 0。**本轮未重新连接服务器，未验证这些状态是否发生变化。**

当前 SSH 私钥需要交互解锁；既有非交互认证失败不能证明服务器不可用。实际服务器命令应在已解锁的授权终端或获准管理通道执行，不要求向聊天提供私钥口令。

## 11. 本轮交付和执行边界

本轮只新增此报告，未修改运行代码、测试、忽略规则、数据库、env、密钥或服务器；未执行 commit、push、镜像构建/部署、账号创建或注册开关变更。临时 Git 导出仅用于无数据库的测试发现核验。

**下一步：先确认如何修复干净源码的测试依赖缺口，再完成 Linux 和镜像验收；之后才批准 Staging 部署，最后批准注册开放。**
