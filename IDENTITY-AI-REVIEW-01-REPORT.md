# 学生归属 AI 辅助复核实施记录

日期：2026-11-23。范围：身份匹配失败后的视觉复读、名单核对和教师确认。

## 已实现流程

1. 既有 OCR 与匹配规则先执行，已经归属和明确重复的照片不触发身份模型。
2. 自动 AI 复核启用时，未归属照片进入现有 SQLite 后台任务的 identity_review 阶段。无需逐张打开；不新增任务框架。
3. 优先按 OCR 身份标签定位区域，定位不可靠时发送整页。提示词只要求姓名、完整学号、班级，不提供名单、标准答案或评分任务；整页仍可能包含作答内容。
4. 模型输出经过 JSON 字段、类型、长度、枚举和重复键校验，再由本次应交名单匹配。
5. 自动归属要求高置信度、完整学号在本次有效名单中唯一，且与原 OCR 姓名、学号栏、班级没有冲突。姓名近似只提供候选。
6. 全批复读完成后统一检查重复或已有答卷，禁止多张照片抢占同一学生；第二页仍需教师明确追加。
7. 不足以自动归属的照片显示原图／身份区域、原 OCR、AI 复读、原因和候选，教师用原有确认接口一键保存。AI 归属沿用机器更正入口；已有人工批改、多页或目标占用时保留既有保护。

## 使用位置

- 设置 → AI 自动复核：沿用已保存的视觉模型地址、模型名称和密钥。
- 新上传：启用自动复核后接续原后台流程。
- 既有照片：作业 → 待核对 → “AI复核待归属照片”；上传页“更多操作 · 识别维护”也有入口。显式操作可在自动开关关闭但凭据完整时运行。
- 单张照片：待核对卡片 → “AI复核身份”；已有结果可明确重新复核。刷新或轮询本身不会调用模型。
- 接口失败停止本批后续请求并保留错误；教师检查配置后显式重试。中断请求标记 unknown，恢复任务不自动重复发送。

## 数据和接口

无需数据库结构迁移。复用 batch_photos.ocr_result.identity_review 保存最新结果和历史尝试；不替换 blocks、identified_text 或原始答卷。既有 review_events 保存归属与更正记录。

- POST /api/batch-photos/<id>/identity-review：空 JSON，缓存有效结果，防止重复调用。
- POST /api/batch-photos/<id>/identity-review/retry：空 JSON，明确重试并归档前次记录。
- GET /api/batch-photos/<id>/identity-review/image：需资源归属权限的身份区域预览。
- POST /api/assignments/<id>/identity-review-jobs：空 JSON，复用现有后台任务，不重复整页 OCR。显式重试记录原请求标识，避免崩溃恢复时再次收费。

沿用登录、教师级数据隔离、CSRF 和 Origin 校验。模型只返回文字，不信任模型提供学生数据库 ID。错误消息经过脱敏。

## 本轮涉及文件

identity_review.py；batch_photos.py；grading_jobs.py；teacher_workbench.py；app.py；static/classes.js；static/workspace.js；static/style.css；.gitignore；test_identity_review.py；test_identity_job.py；test_identity_stage_visibility.py；scripts/check_identity_review_ui.cjs。工作区其他翻译等既有修改保留。

## 验证证据

- `.venv/Scripts/python.exe -m unittest discover -p 'test_*.py'`：399 项，397 通过、2 跳过、0 失败，292.266 秒。跳过原因由原有测试的平台／外部文件条件决定，未把跳过视为通过。
- 专项覆盖：唯一学号、低置信度、姓名冲突、名单不存在、班级不一致、重复照片、原始 OCR 保留、教师并发确认、旧快照失效、失败重试历史、请求中断恢复、缓存幂等、禁用自动复核、后台续接、跨教师和未登录访问、CSRF、Origin、裁图预览、非法模型 JSON。
- `node scripts/check_identity_review_ui.cjs`：通过。采用虚构照片与接口，验证加载防重、失败提示、明确重试、网络结果不明时 GET 核对、一键确认、筛选和焦点保留、AI 归属更正入口、390／900／1440px 无页面溢出。
- 截图：outputs/identity-attention-390.png、identity-attention-900.png、identity-attention-1440.png、identity-upload-390.png，均为虚构验收内容。
- 本地服务重启前确认无运行任务；以只读方式比较 11 张业务表内容指纹，重启前后相同。沿用 local-data-dir.txt 固定数据库，不创建演示账号或替换数据。

## 尚需真实样本验证

本轮未把真实学生照片发送给模型，模型调用使用替身。真实姓名识别效果、供应商响应延迟与费用尚未验证；不能将工程测试通过表述为识别准确率已达标。未更改用户的 AI 自动复核开关，未提交或推送 Git，也未部署服务器。

后续更新：用户要求修复并跑完当前作业后，已执行获授权的真实复核；结果见 AI-REVIEW-TRUNCATION-FIX.md。此处“未发送”描述的是首次实现验收阶段，不再代表后续执行状态。
