# SellerSupport AI

跨境电商客服与商品改进工作台，连接咨询处理、双向翻译、售后审批、人工交接、问题归集和知识更新。

## 功能

- 客服工作台：工单处理、中英文回复、来源查证、客户反馈、本人改进任务和附件上传。
- 主管工作台：售后审批、执行对账、异常调度、改进管理、知识发布和服务质检。
- 知识库：按关键词、分类、市场和生效状态查询商品与政策，保留来源和版本。
- 流程管理：订单授权、审批记录、人工执行凭据、操作历史及规则回归。

应用使用本地单商家数据库，首次启动为空，需要先配置商家并录入资料。回复生成和双向翻译可接入模型服务；未配置时可使用规则查证及英文手工编辑。发送、退款和发货由操作人在外部渠道完成，再登记凭据；应用尚未连接这些渠道。

## 快速启动（Windows）

环境：Python 3.12+、Node.js 22.6+、npm。首次安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

随后双击根目录的 `启动项目.bat`。首次运行会安装前端依赖、构建页面，并创建本地数据库和随机初始凭据；后续启动复用已有环境和数据。

首次打开 [商家配置](http://127.0.0.1:8012/merchant-setup)，使用本机 `storage/workbench.credentials.json` 中的 `supervisor` 凭据读取配置，填写商家与人员信息后初始化。至少配置两名人员及一名主管，并设置各自的凭据。初始化后，用配置页的「用此身份进入工作台」登录；其他人员同样使用本人凭据进入。

- [工作台](http://127.0.0.1:8012/)
- [独立业务验证工作区](http://127.0.0.1:8019/merchant-setup)

停止服务：

```powershell
.\.venv\Scripts\python.exe scripts/run.py --stop
```

## 模型配置

复制 `.env.example` 为 `.env`，填写 `DEEPSEEK_API_KEY`、模型名称及预算。`SELLERSUPPORT_TRANSLATION_ENABLED` 控制双向翻译，`SELLERSUPPORT_AI_ENABLED` 控制回复生成。一键入口读取本地配置；主管可在「主管待办」切换本次服务的 AI 回复开关。

`.env`、本地归档、数据库、日志、依赖、构建和运行报告不提交到仓库。测试输入集中在 `tests/fixtures/`，供回归使用，正常启动不载入这些资料。

## 文档

- [使用说明](docs/使用说明.md)
- [开发说明](docs/开发说明.md)
- [产品与业务规则](docs/产品说明.md)
- [系统架构](docs/architecture/系统架构.md)

## 技术栈

FastAPI · Pydantic · SQLite · React · TypeScript · Vite

## 验证

```powershell
python -m pytest
npm --prefix frontend test
npm --prefix frontend run build
python scripts/check_repository.py
```
