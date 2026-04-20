# 司法鉴定意见书自动生成系统 - 数据库设计

> 基于 PRD v1.0  
> 日期：2026-04-19  
> 更新：v1.2 - 新增 material_groups 表，所有类型支持多文件，病历按医院分组

---

## ER 关系图

```
Case (1) ──→ (1) Person          被鉴定人
Case (1) ──→ (N) Material        上传材料（所有类型支持多文件）
Case (1) ──→ (N) MaterialGroup   材料分组（病历/影像按医院分组）
Case (1) ──→ (N) HospitalRecord  住院记录（从病历材料OCR提取）
Case (1) ──→ (N) ImagingReport   影像学报告（从影像材料OCR提取）
Case (1) ──→ (1) Report          报告内容（6大部分）
Case (1) ──→ (N) StyleLog        风格学习记录

MaterialGroup (1) ──→ (N) Material  一个医院分组下多张照片
Material (1) ──→ (0..N) HospitalRecord   病历材料可提取出住院记录
Material (1) ──→ (0..N) ImagingReport    影像材料可提取出报告
```

---

## 表结构设计

### 1. cases（案件表）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK, AUTO | 主键 |
| case_number | VARCHAR(50) | UNIQUE | 案件编号（后续确定规则） |
| status | VARCHAR(20) | NOT NULL, DEFAULT 'pending_upload' | 案件状态 |
| entrusting_unit | VARCHAR(200) | | 委托单位（从委托书提取） |
| entrustment_matter | TEXT | | 委托事项（从委托书提取） |
| acceptance_date | VARCHAR(20) | | 受理日期（鉴定人手动输入） |
| appraisal_date | VARCHAR(20) | | 鉴定日期 = 受理日期 |
| appraisal_location | VARCHAR(200) | DEFAULT '新乡医学院司法鉴定中心' | 鉴定地点 |
| on_site_personnel | VARCHAR(200) | | 在场人员（可空） |
| material_list | TEXT | | 鉴定材料清单（自动生成） |
| person_name | VARCHAR(50) | | 被鉴定人姓名冗余（列表页展示用） |
| created_at | DATETIME | DEFAULT NOW | 创建时间 |
| updated_at | DATETIME | DEFAULT NOW | 更新时间 |

**status 枚举值：**

| 值 | 中文 | 说明 | 可执行操作 |
|----|------|------|-----------|
| pending_upload | 待上传 | 案件刚创建 | 上传材料、删除材料 |
| recognizing | 识别中 | OCR 正在处理 | 等待、查看进度 |
| pending_review | 待修正 | OCR 完成，等待鉴定人查看 | 查看/修正内容 |
| reviewing | 修正中 | 鉴定人正在修改 | 编辑各部分、上传补充材料 |
| pending_confirm | 待确认 | 内容填写完毕 | 确认鉴定意见 |
| completed | 已完成 | 报告确认无误 | 查看、导出、重新打开 |

**状态流转规则：**
```
pending_upload → recognizing      （点击「开始识别」）
recognizing → pending_review      （OCR全部完成后自动）
pending_review → reviewing        （鉴定人第一次编辑时自动）
reviewing → pending_confirm       （点击「提交审核」）
pending_confirm → completed       （点击「确认完成」）
completed → reviewing             （点击「重新打开」）
```

---

### 2. persons（被鉴定人表）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK, AUTO | 主键 |
| case_id | INTEGER | FK→cases.id, UNIQUE | 关联案件（一对一） |
| name | VARCHAR(50) | | 姓名 |
| gender | VARCHAR(10) | | 性别 |
| birth_date | VARCHAR(20) | | 出生日期 |
| id_number | VARCHAR(30) | | 身份证号 |
| address | TEXT | | 住址 |

---

### 3. material_groups（材料分组表）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK, AUTO | 主键 |
| case_id | INTEGER | FK→cases.id | 关联案件 |
| material_type | VARCHAR(30) | NOT NULL | 材料类型（medical_record / imaging_report） |
| group_name | VARCHAR(200) | NOT NULL | 分组名称（如：新乡市中心医院） |
| sort_order | INTEGER | DEFAULT 0 | 排序序号 |
| created_at | DATETIME | DEFAULT NOW | 创建时间 |

**用途：** 病历和影像学报告按医院分组。一个医院=一个分组，组内上传多页照片。

---

### 4. materials（材料表）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK, AUTO | 主键 |
| case_id | INTEGER | FK→cases.id | 关联案件 |
| group_id | INTEGER | FK→material_groups.id | 分组ID（病历/影像按医院分组时使用） |
| material_type | VARCHAR(30) | NOT NULL | 材料类型 |
| description | VARCHAR(200) | | 文件描述（如：正面、反面、第3页） |
| page_number | INTEGER | | 页码（同一组内多页排序，自动计算） |
| file_path | VARCHAR(500) | NOT NULL | 文件存储路径 |
| original_filename | VARCHAR(200) | | 原始文件名 |
| ocr_text | TEXT | | OCR 原始识别文本 |
| ocr_status | VARCHAR(20) | DEFAULT 'pending' | OCR 状态 |
| created_at | DATETIME | DEFAULT NOW | 上传时间 |

**material_type 枚举值：**

| 值 | 中文 | 上传规则 | 说明 |
|----|------|---------|------|
| entrustment_letter | 委托书 | 多文件 | 支持多页，每张单独上传 |
| id_card | 身份证复印件 | 多文件 | 正面+反面各一张 |
| traffic_accident_cert | 道路交通事故认定书 | 多文件 | 可能多页 |
| appraisal_application | 鉴定申请书 | 多文件 | 可能多页 |
| medical_record | 医院病历 | **按医院分组** | 一个医院=一个分组，组内多页 |
| imaging_report | 影像学报告 | **按医院分组** | 同上 |

**上传逻辑：**
- 委托书/身份证/认定书/申请书：直接上传，支持单张和批量
- 病历/影像：先创建"医院分组"（输入医院名），然后在分组内上传多张照片
- group_id 为 null 的材料属于"非分组类型"
- page_number 在有 group_id 时自动计算（组内第N页）

**ocr_status 枚举值：**

| 值 | 中文 |
|----|------|
| pending | 待识别 |
| processing | 识别中 |
| completed | 已完成 |
| failed | 识别失败 |

---

### 5. hospital_records（住院记录表）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK, AUTO | 主键 |
| case_id | INTEGER | FK→cases.id | 关联案件 |
| material_id | INTEGER | FK→materials.id | 来源材料 |
| hospital_name | VARCHAR(200) | | 医院名称 |
| admission_number | VARCHAR(50) | | 住院号 |
| chief_complaint | TEXT | | 主诉 |
| present_illness_history | TEXT | | 现病史 |
| past_history | TEXT | | 既往史（可空） |
| physical_examination | TEXT | | 体格检查 |
| admission_diagnosis | TEXT | | 入院诊断 |
| treatment_process | TEXT | | 治疗过程（可空） |
| medication | TEXT | | 用药情况（可空） |
| discharge_diagnosis | TEXT | | 出院诊断 |
| discharge_orders | TEXT | | 出院医嘱 |
| admission_date | VARCHAR(20) | | 入院日期 |
| discharge_date | VARCHAR(20) | | 出院日期 |
| hospital_days | INTEGER | | 住院天数 |

---

### 6. imaging_reports（影像学报告表）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK, AUTO | 主键 |
| case_id | INTEGER | FK→cases.id | 关联案件 |
| material_id | INTEGER | FK→materials.id | 来源材料 |
| report_date | VARCHAR(20) | | 报告日期 |
| hospital_name | VARCHAR(200) | | 医院名称 |
| exam_type | VARCHAR(50) | | 检查类型（CT/X线/MRI等） |
| film_number | VARCHAR(50) | | 片子编号 |
| film_count | INTEGER | DEFAULT 1 | 片子数量 |
| report_content | TEXT | | 报告内容 |

**exam_type 枚举值：** CT、X线、MRI、其他

---

### 7. reports（报告表）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK, AUTO | 主键 |
| case_id | INTEGER | FK→cases.id, UNIQUE | 关联案件（一对一） |
| case_facts | TEXT | | 基本案情 |
| material_summary | TEXT | | 资料摘要 |
| appraisal_process | TEXT | | 鉴定过程 |
| analysis | TEXT | | 分析说明 |
| opinion | TEXT | | 鉴定意见 |
| opinion_confirmed | BOOLEAN | DEFAULT FALSE | 鉴定意见是否已确认 |
| generated_at | DATETIME | | 报告生成时间 |
| created_at | DATETIME | DEFAULT NOW | 创建时间 |
| updated_at | DATETIME | DEFAULT NOW | 更新时间 |

---

### 8. style_logs（风格学习记录表）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK, AUTO | 主键 |
| case_id | INTEGER | FK→cases.id | 关联案件 |
| section | VARCHAR(30) | NOT NULL | 修改的报告部分 |
| original_text | TEXT | | AI 自动生成的原文 |
| revised_text | TEXT | | 鉴定人修改后的文本 |
| diff_summary | TEXT | | 修改差异摘要（自动生成） |
| created_at | DATETIME | DEFAULT NOW | 记录时间 |

**section 枚举值：**

| 值 | 中文 |
|----|------|
| case_facts | 基本案情 |
| material_summary | 资料摘要 |
| appraisal_process | 鉴定过程 |
| analysis | 分析说明 |
| opinion | 鉴定意见 |

**用途：** 记录鉴定人对AI生成内容的修改，积累后用于学习鉴定人的写作风格，使后续生成更贴合个人习惯。

---

## 数据流转说明

### 上传阶段
```
用户按类型上传图片 → materials 表（material_type, description, file_path）

按类型分类：
├─ 委托书/身份证/认定书/申请书 → 单文件替换（已有则自动替换）
├─ 医院病历 → 可重复上传（自动标注"第N家医院"）
└─ 影像学报告 → 可重复上传（自动标注"第N次报告"）
```

### OCR 识别阶段
```
materials.ocr_status: pending → processing → completed
materials.ocr_text: 存储原始 OCR 文本

根据 material_type 提取结构化数据：
- id_card → persons 表
- entrustment_letter → cases.entrusting_unit, entrustment_matter, person_name
- medical_record → hospital_records 表
- imaging_report → imaging_reports 表
- traffic_accident_cert → 报告.case_facts 中的案情部分
```

### 报告生成阶段
```
从 cases + persons + hospital_records + imaging_reports 组装：

1. 基本情况：cases 字段 + persons 字段 + material_list
2. 基本案情：
   - 从 entrustment_letter 提取事故时间/地点/经过/责任
   - 从 hospital_records 提取就医经过
   - 拼接收尾句："现为处理案件需要，XX特委托我鉴定中心XX"
3. 资料摘要：从 hospital_records 逐条组装
4. 鉴定过程：
   - 格式语言（固定模板）
   - 从 persons.name 填入被鉴定人姓名
   - 从 imaging_reports 逐条生成复阅段落
5. 分析说明：
   - 外伤史确切（固定格式）
   - 诊断明确（根据实际资料判断X线/CT/手术）
   - 委托事项草稿（伤残等级/误工期/后续治疗费等）
6. 鉴定意见：根据分析说明生成草稿

→ reports 表各字段
```

### 鉴定人修正阶段
```
鉴定人直接编辑 reports 表各字段
cases.status: pending_review → reviewing

修正保存时，比较 original_text vs revised_text：
- 如有差异 → 写入 style_logs 表
```

### 确认完成
```
鉴定人确认 opinion → reports.opinion_confirmed = TRUE
cases.status: reviewing → pending_confirm → completed
```

---

## 索引设计

| 表 | 索引 | 说明 |
|------|------|------|
| cases | idx_cases_status | 按状态查询 |
| cases | idx_cases_created | 按时间排序 |
| materials | idx_materials_case_type | 按案件+类型查询（分组展示核心索引） |
| hospital_records | idx_hospital_case | 按案件查询 |
| imaging_reports | idx_imaging_case | 按案件查询 |
| style_logs | idx_style_case_section | 按案件+部分查询 |

---

## 关键业务逻辑

### 受理日期 → 鉴定日期同步
```
cases.acceptance_date 变更时 → 自动同步到 cases.appraisal_date
```

### 被鉴定人姓名冗余同步
```
persons.name 变更时 → 自动同步到 cases.person_name（列表页展示用）
```

### 非分组类型材料上传
```
委托书/身份证/认定书/申请书：直接上传多张照片
- 每张独立记录，支持手动指定 description（如"正面""反面""第2页"）
- 不需要按医院分组，直接平铺展示
```

### 分组类型材料上传（病历/影像）
```
1. 先创建 material_groups（输入医院名称，如"新乡市中心医院"）
2. 在分组内上传多张照片
3. page_number 自动递增（组内第1页、第2页...）
4. description 自动生成为"第N页"
5. 多家医院 = 多个分组，互不干扰
```

### 影像学复阅段落生成
```
对每条 imaging_reports 记录生成一段复阅文字：
格式："复阅XXXX年X月X日XXX医院XXX（被鉴定人）CT片（号XXXX）示：XXXX"
```
