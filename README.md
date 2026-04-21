# 司法鉴定意见书自动生成系统

> 版本：v1.0  
> 更新日期：2026-04-22

## 项目简介

司法鉴定意见书自动生成系统，通过 OCR 识别上传的材料图片，自动提取结构化数据，生成规范的鉴定意见书 Word 文档。

**核心流程：**
```
上传材料 → OCR识别 → 智能提取 → 鉴定人修正 → 生成Word报告
```

---

## 项目状态

🟢 **v1.0 已完成**

- [x] 后端 API（FastAPI）
- [x] 前端界面（Vue3 + Element Plus）
- [x] OCR 识别（PaddleOCR）
- [x] LLM 智能提取
- [x] PDF 导入与分割
- [x] Word 报告生成
- [x] 风格学习记录

**访问地址：**
- 前端界面：http://localhost:3000
- 后端 API：http://localhost:8000
- API 文档：http://localhost:8000/docs

---

## 快速启动

### 方式一：使用启动脚本

双击运行项目根目录下的启动文件：
- `启动鉴定系统.command` - 启动后端和前端
- `关闭鉴定系统.command` - 关闭所有服务

### 方式二：手动启动

**后端：**
```bash
cd /Users/anjing137/WorkBuddy/Claw/forensic_report_system/backend

# 激活 PaddleOCR 环境
source /Users/anjing137/.venv_vlm/bin/activate

# 启动服务
python main.py
# 或使用 uvicorn
uvicorn main:app --host 0.0.0.0 --port 8000
```

**前端：**
```bash
cd /Users/anjing137/WorkBuddy/Claw/forensic_report_system/frontend
npm install
npm run dev
```

**构建前端（生产环境）：**
```bash
cd frontend
npm run build
# 构建产物自动输出到 backend/static/ 目录
```

---

## 项目结构

```
forensic_report_system/
├── backend/
│   ├── main.py                 # FastAPI 入口
│   ├── forensic_report.db      # SQLite 数据库
│   ├── uploads/                # 上传文件存储
│   ├── reports/                # 生成的 Word 报告
│   ├── logs/                   # 运行日志
│   ├── static/                 # 前端构建产物
│   └── app/
│       ├── routers/            # API 路由（9个模块）
│       │   ├── cases.py        # 案件管理
│       │   ├── materials.py    # 材料上传/PDF转换
│       │   ├── medical_records.py
│       │   ├── imaging_reports.py
│       │   ├── reports.py      # 报告生成
│       │   ├── persons.py      # 被鉴定人
│       │   ├── llm_extract.py  # LLM智能提取
│       │   ├── style_logs.py   # 风格学习
│       │   └── settings.py     # 系统设置
│       ├── models/             # 数据模型
│       ├── schemas/            # Pydantic 模型
│       └── utils/
│           ├── ocr.py          # PaddleOCR 识别
│           ├── llm.py          # LLM 提取/生成
│           ├── pdf_converter.py # PDF转PNG
│           └── report_generator.py # Word生成
├── frontend/
│   └── src/
│       ├── views/
│       │   ├── Dashboard.vue   # 工作台
│       │   ├── CaseList.vue    # 案件列表
│       │   ├── CaseCreate.vue  # 新建案件
│       │   ├── CaseDetail.vue  # 案件详情（核心）
│       │   └── Templates.vue   # 报告模板
│       ├── api/                # API 调用
│       ├── router/             # 路由配置
│       └── store/              # 状态管理
├── PRD.md                      # 产品需求文档
├── DATABASE.md                 # 数据库设计
├── README.md                   # 本文档
├── START.md                    # 启动说明
├── 启动鉴定系统.command         # 一键启动
└── 关闭鉴定系统.command         # 一键关闭
```

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端框架 | FastAPI |
| 数据库 | SQLite + SQLAlchemy |
| 前端框架 | Vue 3 + Element Plus |
| 构建工具 | Vite |
| OCR 识别 | PaddleOCR（硅基流动） |
| LLM 提取 | OpenAI 兼容 API |
| PDF 转换 | pdftoppm (poppler) |
| 文档生成 | python-docx |

---

## API 接口概览

共 9 个路由模块，约 60 个 API 接口：

| 模块 | 路径 | 主要功能 |
|------|------|----------|
| 案件管理 | `/api/cases` | CRUD、状态流转、OCR识别控制 |
| 材料管理 | `/api/materials` | 上传、分组、PDF转换导入 |
| 住院记录 | `/api/medical-records` | CRUD |
| 影像报告 | `/api/imaging-reports` | CRUD |
| 鉴定报告 | `/api/reports` | 内容编辑、Word导出 |
| 被鉴定人 | `/api/persons` | CRUD |
| LLM提取 | `/api/llm-extract` | 智能提取、内容生成 |
| 风格学习 | `/api/style-logs` | 修改记录、统计 |
| 系统设置 | `/api/settings` | 鉴定人信息配置 |

详细 API 文档请访问：http://localhost:8000/docs

---

## 核心功能

### 1. 材料上传
- 支持 6 种材料类型：委托书、身份证、交通事故认定书、申请书、病历、影像报告
- 病历和影像按医院分组管理
- 支持单文件/批量上传

### 2. PDF 导入
- 上传 PDF 自动分割为 PNG 图片
- 缩略图预览，点击查看大图
- 选择图片导入到指定材料分类

### 3. OCR 识别
- PaddleOCR 自动识别材料文字
- 显示识别进度，支持单独/批量识别
- 可手动修正识别结果

### 4. LLM 智能提取
- 自动提取被鉴定人信息
- 自动提取住院记录结构化数据
- 自动生成报告各部分内容
- 支持风格学习，记录修改习惯

### 5. 报告生成
- 自动生成 6 大部分内容
- 在线编辑修正
- 一键导出 Word 文档

---

## 配置文件

后端环境变量（`backend/.env`）：
```bash
# 数据库路径
DATABASE_URL=sqlite:///./forensic_report.db

# 上传目录
UPLOAD_DIR=./uploads

# LLM API（如使用）
LLM_API_KEY=your_api_key
LLM_API_BASE=https://api.example.com/v1
```

---

## 日志查看

```bash
# 查看最近日志
tail -f backend/logs/app.log

# 或通过 API 查看
curl http://localhost:8000/api/logs?lines=100
```

---

## 相关文档

- [产品需求文档](./PRD.md) - 详细功能需求
- [数据库设计](./DATABASE.md) - 表结构说明
- [启动说明](./START.md) - 环境配置与启动步骤

---

## 开发者

- 开发时间：2026-04
- 技术支持：OpenClaw AI Assistant
