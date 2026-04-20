# 司法鉴定意见书自动生成系统

## 项目状态

🟢 **后端已运行**: http://localhost:8000
📚 **API文档**: http://localhost:8000/docs

---

## 快速启动

### 后端（使用你的 PaddleOCR 环境）

```bash
cd /Users/anjing137/WorkBuddy/Claw/forensic_report_system/backend

# 激活已有的 PaddleOCR 环境
source /Users/anjing137/.venv_vlm/bin/activate

# 启动服务
python main.py
```

### 前端（待开发）

```bash
cd /Users/anjing137/WorkBuddy/Claw/forensic_report_system/frontend
npm install
npm run dev
```

---

## 后端 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/cases/` | GET | 获取案件列表 |
| `/api/cases/` | POST | 创建新案件 |
| `/api/cases/{id}` | GET | 获取单个案件 |
| `/api/cases/{id}` | PUT | 更新案件 |
| `/api/cases/{id}` | DELETE | 删除案件 |
| `/api/materials/upload/{case_id}` | POST | 上传材料 |
| `/api/medical-records/case/{case_id}` | POST | 添加住院记录 |
| `/api/reports/generate/{case_id}` | POST | 生成报告 |

---

## 技术栈

- **后端**: FastAPI + SQLAlchemy + SQLite
- **OCR**: PaddleOCR（你的 .venv_vlm 环境）
- **文档生成**: python-docx

---

## 下一步

1. [ ] 开发前端页面
2. [ ] 集成 OCR 识别功能
3. [ ] 添加报告模板管理
