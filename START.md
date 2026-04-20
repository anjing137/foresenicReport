# 司法鉴定意见书系统 - 启停命令

## 启动

```bash
# 后端
cd ~/WorkBuddy/Claw/forensic_report_system/backend && nohup ~/.venv_vlm/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > /tmp/forensic_backend.log 2>&1 &

# 前端
cd ~/WorkBuddy/Claw/forensic_report_system/frontend && nohup npm run dev > /tmp/forensic_frontend.log 2>&1 &
```

## 关闭

```bash
kill $(lsof -i :8000 -t)   # 后端
kill $(lsof -i :3000 -t)   # 前端
```

## 访问地址

- 前端页面：http://localhost:3000
- 后端API：http://localhost:8000
- API文档：http://localhost:8000/docs

## 查看状态

```bash
lsof -i :8000 | grep LISTEN   # 后端是否在跑
lsof -i :3000 | grep LISTEN   # 前端是否在跑
```

## 查看日志

```bash
tail -20 /tmp/forensic_backend.log
tail -20 /tmp/forensic_frontend.log
```
