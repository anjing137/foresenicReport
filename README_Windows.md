# 司法鉴定意见书自动生成系统 v1.0

## 首次使用必读 ⚠️

**OCR 功能需要配置 API Key 才能使用！**

### 第一步：配置 API Key

1. 打开 `backend` 文件夹
2. 找到 `backend\.env.example`，复制一份，重命名为 `.env`
3. 编辑 `.env`，填入你的硅基流动 API Key：

```
SILICONFLOW_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

> API Key 获取地址：https://cloud.siliconflow.cn/
> PaddleOCR-VL 1.5 版本有免费额度

---

## 启动系统

双击运行 `启动.bat`，等待窗口显示 "系统已启动" 后即可使用。

访问地址：**http://localhost:8000**

---

## 常见问题

**Q: OCR 提示"未配置 API Key"**
A: 检查 `backend\.env` 文件是否存在，API Key 是否填写正确。

**Q: 启动后提示缺少 Python 模块**
A: 确保已安装 Python 3.9+，然后在 backend 目录下运行：
   ```
   pip install -r requirements.txt
   ```

**Q: 端口 8000 被占用**
A: 修改 `backend\start.bat`，把 `--port 8000` 改成其他端口（如 8001）。

---

## 技术栈

- 后端：FastAPI + SQLAlchemy + SQLite
- OCR：PaddleOCR-VL 1.5（硅基流动 API）
- LLM：Qwen3-8B（硅基流动 API，免费）
- 前端：Vue3 + Element Plus
