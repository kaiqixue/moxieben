# GitHub 私有仓库与 CI/CD

> 历史说明：项目现已建立 Git 和远端分支，下文“未初始化 Git”只描述初审状态。部署文件已在本地补齐，但本轮不自动提交/推送，也未新建 CI/CD workflow。发布前先审查本轮差异并上传选定版本。

当前工作目录未初始化 Git。可创建私有仓库管理代码，但应先在单独干净目录准备首次提交。私有仓库和镜像库都只接收核准的源码、配置模板及合成测试件。

## 首次提交

1. 白名单复制运行模块、静态资源、模板、依赖清单、审阅过的测试、文档和未来部署配置。逐一核对 `tests/fixtures/` 的来源，改用合成 OCR 与 Word 样本，移除个人路径。桌面或项目中的 Word 原稿、归档脚本、截图、识别对比结果和本地运维记录默认留在仓库外。
2. 扩充根目录 `.gitignore` 与 `.dockerignore`。二者独立检查，后者优先用构建输入白名单。忽略规则建议如下，允许的合成样本另行显式审查。

```gitignore
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
node_modules/
data/
uploads/
backups/
outputs/
tmp*/
test-results/
playwright-report/
*.log
*.sqlite*
*.db
*.db-*
*.dump
*.pem
*.key
.env
.env.*
!.env.example
secrets/
```

3. `.env.example` 只放占位和非敏感字段，例如 `APP_ENV=staging`、`APP_HOST=staging.example.invalid`、`APP_ORIGIN=https://staging.example.invalid`、`DATA_DIR=/var/lib/moxieben`、`OCR_PROVIDER=mock`。实际部署对 `mock` 需显式实现或由 Staging 测试入口注入，当前生产代码并未识别该配置值。Secret 文件路径可设占位，真实内容由服务器配置。
4. 审查已暂存文件、文本和二进制资产。使用秘密扫描工具和人工隐私审查。第一轮模式搜索只能发现部分密钥格式。禁止纳入任何学生姓名、名单、原图、OCR 内容、生产库、真实 Token 和 Key。若误提交，立即吊销或轮换，清理历史及制品，并按内部流程处理泄露。

## 分支与发布

- `main` 作为可发布分支，采用短期 `feat/*`、`fix/*`、`docs/*` 分支。PR 写清用途、验证和部署影响。提交例 `fix(auth): 校验答卷所属班级`。保护 main，要求测试通过及代码审阅，发布用 `vX.Y.Z` 标记。
- PR CI 在 Ubuntu/Python 3.11 安装依赖，运行 `python -m unittest discover -q`、Node 的 `--check` 和关键浏览器流程。用临时目录、模拟 OCR、合成数据。不得向 PR job 提供部署密钥或访问正式数据。开发依赖、运行依赖和 Linux 镜像分别验证。
- main 通过 CI 后构建 x86_64 镜像，扫描依赖和镜像，发布私有 GHCR。设置 Job 最小权限，发布 job 才授予 `packages: write`，第三方 Action 固定完整提交 SHA。Staging 自动部署同一摘要。通过受限部署账号或只在受控网络内接收 main 发布的专用 runner，串行执行代理、登录、上传、评分、导出和持久化冒烟检查。
- Production 手动发布，输入 Staging 已验收的镜像摘要，校验来源与测试结果。以环境审阅或独立运维审批落实产品负责人批准，审批后才取得生产权限。私有仓库的 GitHub 环境审批能力取决于订阅。若不可用，由受限生产运维账号在批准后手动执行，Staging workflow 持续无生产密钥。保留摘要、批准人、回滚点和发布时间。[GitHub 环境配置](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments) · [Actions 安全实践](https://docs.github.com/en/actions/reference/security/secure-use)

GitHub 和 GHCR 的凭据、Staging 部署权限与 Production 权限分别管理。业务 OCR Key 仅留在对应运行环境。测试报告和构建制品只包含合成数据、脱敏日志及镜像摘要。
