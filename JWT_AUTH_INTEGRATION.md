# EVMLogListener JWT 认证集成说明

## 已完成的改造

1. ✅ 添加 JWT 认证中间件 (`src/evm_chain_listener/middleware/auth_middleware.py`)
2. ✅ 更新 requirements.txt，添加 PyJWT 和 python-jose
3. ✅ 在 main.py 中集成认证中间件

## 认证策略

EVMLogListener 的 API 端点已添加 JWT 认证保护。

### 公开端点（无需认证）

- `GET /` - Web UI 主页
- `GET /health` - 健康检查
- `GET /ready` - 就绪检查
- `GET /docs` - API 文档
- `GET /openapi.json` - OpenAPI 规范
- `GET /redoc` - ReDoc 文档

### 受保护的端点（需要认证）

所有其他 API 端点都需要有效的 JWT token：

- `GET /api/logs` - 查询日志
- `GET /api/logs/stats` - 获取缓存统计
- `GET /api/logs/topics` - 获取唯一主题
- `GET /api/chains` - 获取链状态
- `GET /api/pusher/stats` - 获取推送器统计
- `POST /api/backtest/run` - 运行回测
- `GET /api/backtest/status/{task_id}` - 获取回测状态
- `GET /api/backtest/tasks` - 列出回测任务
- `GET /api/backtest/chains` - 列出可用链
- `GET /api/replay/recordings` - 列出录制
- `POST /api/replay/start` - 开始回放

### 使用方法

客户端需要在请求头中携带 JWT token：

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8002/api/logs
```

### 环境变量

确保设置与 AuthService 相同的 JWT 密钥：

```bash
JWT_SECRET_KEY=your-super-secret-jwt-key-change-in-production
JWT_ALGORITHM=HS256
```

## 测试

1. 启动 AuthService
2. 获取 JWT token
3. 使用 token 访问 EVMLogListener API

```bash
# 登录获取 token
TOKEN=$(curl -X POST http://localhost:8100/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123456"}' \
  | jq -r '.access_token')

# 使用 token 访问 API
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8002/api/logs
```

## 注意事项

- Web UI 主页（`/`）保持公开访问，但其 JavaScript 调用的 API 端点需要认证
- 健康检查端点（`/health`, `/ready`）保持公开，用于容器编排和监控
- API 文档（`/docs`, `/redoc`）保持公开，便于开发调试
