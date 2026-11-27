# RELEASE-02：干净源码测试依赖缺口修复报告

日期：2026-11-21（Asia/Shanghai）  
项目：`E:\工作项目\ai批改`  
分支：`feature/mp-02a-teacher-accounts`  
基线及本轮结束 HEAD：`55edb3840a1c56698c6656e8b6778854aeec02d7`

## 1. 结论

**干净源码的测试依赖缺口已解决，可以进入独立 Linux/Gunicorn 测试阶段。**

从本轮暂存树独立导出的源码完整运行 `python -m unittest discover -q`：

| 项目 | 结果 |
| --- | ---: |
| 实际发现 | 296 |
| unittest 运行计数（包括跳过） | 296 |
| 成功 | 294 |
| 失败 | 0 |
| 错误 | 0 |
| 跳过 | 2 |
| 测试发现导入错误 | 0 |
| 归档安全测试成功 | 4 / 4 |
| 退出码 | 0 |
| 完整测试耗时 | 171.517 秒 |

**没有执行 commit、push、服务器连接、业务数据库迁移、Docker 构建或部署，也没有开启注册。** 进入 Linux 测试阶段不等于允许部署 Staging 或对外开放注册。

## 2. 根因与修复范围

修改前，独立 `git archive HEAD` 导出的默认测试发现结果为：

```text
count = 296
ModuleNotFoundError: No module named 'scripts.migrate_single_teacher'
```

`test_migrate_single_teacher.py:15` 无条件导入该模块；本机文件存在，但被 `/scripts/*` 忽略，未进入 Git。因此本机测试可以通过，干净源码却无法导入。

两个 JSON 也被原 `/tests/fixtures/` 目录忽略规则排除，依赖本机文件才能执行对应 OCR 回归。

| 文件 | 本轮处理及原因 |
| --- | --- |
| `.gitignore` | 修改为精确白名单，避免将无关脚本或测试资源一起纳入。 |
| `scripts/migrate_single_teacher.py` | 审查完整 96 行后纳入暂存跟踪；文件逻辑未修改，供现有测试导入。 |
| `tests/fixtures/grammar_inline_ocr.json` | 审查内容后精确纳入暂存跟踪，恢复干净源码的整页 OCR 回归。内容未修改。 |
| `tests/fixtures/grammar_region_upright.json` | 审查内容后精确纳入暂存跟踪，恢复旋转区域 OCR 回归。内容未修改。 |
| `RELEASE-02.md` | 新增本报告，作为交付材料，未暂存。 |

原有 `STAGING-RELEASE-01.md` 保持原样、仍为未跟踪文件。未修改应用代码、现有测试、Dockerfile、`.dockerignore`、Compose、`.env` 或密钥。

精确规则为：

```gitignore
/tests/fixtures/*
!/tests/fixtures/grammar_inline_ocr.json
!/tests/fixtures/grammar_region_upright.json

/scripts/*
!/scripts/migrate_single_teacher.py
```

scripts 目录原有的三个例外继续保留，没有放开整个目录。将 fixtures 的目录级忽略改为忽略其子项，是为了让 Git 能进入目录并只选择两个例外文件。已验证以下路径仍被忽略：

- `tests/fixtures/grammar_sample.docx`
- `tests/fixtures/student-private.png`
- `tests/fixtures/unapproved.json`
- `scripts/unapproved.py`

后面三个是用于检查规则的路径字符串，没有创建这些文件。

## 3. 迁移脚本完整审查

### 3.1 是否可以纳入 Git

**可以作为源码工具和测试依赖纳入。** 文件没有内置服务器地址、真实账号、密码、密码哈希、Token、私钥或业务数据。

- 顶层仅导入标准库、声明固定表名和 `migrate()`；命令行执行由 `if __name__ == '__main__'` 保护，导入不会自动迁移。
- 数据目录、账号库、目标目录和教师邮箱均由调用者提供，没有默认指向 Staging 或生产库。
- 邮箱查询使用参数绑定；表名来自内部固定常量。
- 源库和账号库通过 `mode=ro` 读取；目标必须是新目录，且不得位于源数据目录之内。
- 结构修改、教师归属填充、DROP/ALTER 都发生在新建的暂存副本中；不会对源库执行这些操作。
- 校验副本完整性、外键和照片引用后才将暂存目录重命名为目标目录；失败清理的是本次建立的暂存副本。

本结论是“源码可以纳入 Git、可以由原有隔离测试导入”，**不是对任意真实数据目录执行迁移的许可或完整生产迁移安全认证**。实际业务迁移仍需要独立授权、停写、备份和输入目录检查。

### 3.2 测试如何使用

`test_migrate_single_teacher.py` 在 `TemporaryDirectory()` 内建立合成单教师库、账号库、白色测试图片和目标副本，再调用 `migrate()`，验证：

- 源库保持旧结构，目标获得正确的教师归属。
- 照片和业务 ID 保留、外键有效、临时账号能登录。
- 环境变量能选择临时租户目录。
- 已存在的目标目录拒绝覆盖。

本轮没有执行迁移脚本的命令行入口，也没有迁移真实数据库。完整 unittest 按原测试定义，仅在合成临时副本上调用迁移函数；账号、会话和图片也都是测试临时数据。

## 4. OCR 样本审查结果

完整读取并人工审查两个 JSON，同时递归检查字符串和结构字段。

| 项目 | `grammar_inline_ocr.json` | `grammar_region_upright.json` |
| --- | --- | --- |
| 内容 | 10 道语法题的题干、提示、匿名 OCR 文本、块编号、坐标及识别属性 | 一段匿名 OCR 文本、旋转角度、输入尺寸和坐标 |
| 学生姓名、学号、学校、班级、成绩归属 | 未发现 | 未发现 |
| 邮箱、手机号或身份证等长号码 | 未发现 | 未发现 |
| 账号、密码、Session、Cookie、Token、密钥 | 未发现 | 未发现 |
| 原始照片、图片编码、下载链接、签名 URL | 未发现 | 未发现 |
| 文件路径或可关联到具体个人的标识 | 未发现 | 未发现 |
| 处理决定 | 按匿名文本测试资源精确纳入 | 按匿名文本测试资源精确纳入 |

正文中的 Jin Nong、Qi Baishi 为题目介绍的历史人物，不是学生身份信息。OCR 文本包含无身份关联的作答词及识别差异，不能据此声称文件全部为人工合成；本轮没有将它们改写成“合成样本”，也没有纳入对应原照片或真实业务记录。

隐私检查结果支持将这两个文件纳入源码候选。**题目文本的来源及对外分发授权无法仅从文件确认**；若后续向公开仓库发布，负责人仍需确认内容授权。当前仅本地暂存，未提交、推送或对外发布。

本轮未创建缺失的 Word 样本，未修改任何测试中的 skip 条件，未用跳过或删除测试掩盖缺口。

## 5. 干净源码验证方法与证据

### 5.1 不依赖本机隐藏文件的导出

由于本轮禁止 commit，不能把尚未提交的修复谎称为 `git archive HEAD` 已包含。实际步骤为：

1. 仅将 `.gitignore` 和上述三个必要文件加入暂存区。
2. 用 `git write-tree` 生成候选暂存树，不创建提交，不移动 HEAD。
3. 用 `git archive --format=zip <候选树>` 导出到独立临时目录。
4. 比较导出文件清单与 `git ls-files`，确认精确一致，共 **93 个文件**，没有 `.git`，没有从工作区额外复制脚本、fixtures、数据或报告。

候选树：

```text
125ea1f253f98965a8add28172433d38db0b3707
```

干净源码目录：

```text
C:\Users\cqhep\AppData\Local\Temp\moxieben-release02-vjmq5qc8\candidate
```

未来经批准提交后，应再从最终提交 SHA 导出验证，避免后续暂存或源码变化使本次证据失效。

### 5.2 完整测试执行

执行环境：Windows、Python 3.11.9；使用现有项目虚拟环境的依赖，源码及测试数据独立。没有为此连接网络安装软件。

实际命令与结果：

```text
python -m unittest discover -q

Ran 296 tests in 171.517s
OK (skipped=2)
```

临时执行器仅记录结果并约束文件、数据库和网络访问，没有改写项目源码或测试用例、筛选测试、替换业务函数或添加跳过规则。

归档安全测试全部发现并成功：

- `test_foreign_active_archived_and_missing_resources_have_identical_404`
- `test_owner_writes_succeed_until_class_is_archived`
- `test_link_targets_are_authorized_before_either_class_archive_check`
- `test_foreign_class_updates_match_missing_classes`

### 5.3 隔离检查

- 数据库连接共记录 11,461 次，全部位于本次临时工作区或内存库；无越界数据库访问。
- 写入和删除操作只允许发生在本次临时工作区，未发生越界文件写入；未读取原项目业务目录。
- 未连接服务器、Staging 或生产数据库，未调用真实 OCR/VLM 服务。
- 网络防护记录 1 次发生在测试开始前的 `socket.bind` 探测，属于 urllib3 导入时检测本机 IPv6 能力，已被阻止；没有外连。该现象与上一轮完整测试的已核验记录一致。
- 测试进程禁止启动外部子进程；Windows 下的 Linux/Gunicorn 用例按原有条件跳过，没有尝试启动业务容器。
- 测试前后核对候选跟踪文件的 SHA-256，原文件内容保持一致，没有因测试修改源码或测试资源。

临时证据文件（可能被系统后续清理，因此关键结果已写入本报告）：

- `C:\Users\cqhep\AppData\Local\Temp\moxieben-release02-vjmq5qc8\baseline-discovery.json`
- `C:\Users\cqhep\AppData\Local\Temp\moxieben-release02-vjmq5qc8\candidate-tree.txt`
- `C:\Users\cqhep\AppData\Local\Temp\moxieben-release02-vjmq5qc8\results.json`
- `C:\Users\cqhep\AppData\Local\Temp\moxieben-release02-vjmq5qc8\unittest.log`

## 6. 剩余跳过测试与未解决事项

| 测试/事项 | 原因 | 下一步 |
| --- | --- | --- |
| `test_app.WebsiteTests.test_word_preview_extracts_grammar_blanks_without_writing` | 缺少 `tests/fixtures/grammar_sample.docx`，本轮前已缺失 | 提供经授权、脱敏且符合原测试断言的 Word 样本后补跑；不创建空文件伪造覆盖。 |
| `test_server_runtime.ServerRuntimeTests.test_single_worker_start_stop_restart_preserves_private_data` | 当前为 Windows，原规则要求 Linux 和 Gunicorn | 在独立 Linux/CI 补跑，不复用业务容器或业务数据挂载。 |
| 题目文本的对外发布授权 | 文件本身不包含授权信息 | 负责人在对外公开前确认；隐私扫描不能证明版权许可。 |
| 最终提交的验证 | 本轮禁止提交，证据绑定暂存树而非新提交 | 经批准形成最终提交后，再核对其文件树并复验。 |

除此之外，本轮完整测试未出现失败或错误，OCR 样本相关测试没有因资源缺失新增跳过，`ModuleNotFoundError` 已消失。

## 7. Git 与敏感文件检查

- `.gitignore` 和三个必要文件已精确暂存；没有执行 commit 或 push，HEAD 未变。
- `git diff --check` 与 `git diff --cached --check` 均通过。
- 候选索引共 93 个文件，未发现数据库、真实 `.env`、私钥、日志、业务输出目录、备份、Word/Excel/CSV 业务文件、学生照片或其他未批准图片。
- `.env.example` 是已有配置模板；`static/images/teacher-desk.png` 是已有产品装饰资产，继续保留。
- 对候选文本进行高置信私钥/API 密钥格式检查，未发现匹配；新增脚本和 JSON 又经过全文人工审查。该检查针对当前候选，不代替未来新增文件审查。
- 未修改 `.dockerignore`：脚本和 fixtures 不在运行镜像白名单内，测试文件也继续排除。本轮只修复源码测试包，不改变运行镜像入口或部署行为。
- Git 提示脚本将按本机配置转换行尾，这是行尾规范化提示，不是测试失败；没有为此更改 Git 配置。

## 8. 是否进入 Linux/Gunicorn 阶段

**可以。** 干净候选树已具备可发现、可执行的完整测试源码，阻断性的模块缺失已解决。

下一阶段应在独立 Linux 环境使用同一候选内容、临时测试目录和合成凭据，安装服务端依赖及测试所需 pypdf，先运行：

```bash
python -m unittest -v test_server_runtime
python -m unittest discover -q
```

允许 Gunicorn 用例所需的本机回环随机端口和临时子进程，禁止外部 OCR/VLM 网络出口；不挂载 Staging 的 data 或 secrets，不继承业务 DATA_DIR 或凭据。Linux 具体结果、最终镜像构建和数据库兼容验收仍待该阶段执行，本报告不声称已通过。

本轮到此结束，不连接服务器、不执行真实迁移、不创建真实教师账号、不开放注册、不提交、不推送、不部署。
