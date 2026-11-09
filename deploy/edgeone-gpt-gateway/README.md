# 麻将 GPT 入口

经典（`lotus-classic`）、翻精（`lotus-legacy`）、血流（`lotus-blood-flow`）和幺筒（`yaotong`）共用后端的 `relay_gpt` 供应商。它们发送同一种 Chat Completions 请求，入口把提示词交给 `gpt-6.1-sol`，流式返回模型回复与 token 用量。多人房间继续使用各自的决策窗口与超时回退。

## 部署到 EdgeOne Makers

1. 在独立的 Makers 项目导入后端仓库，将项目根目录设为 `deploy/edgeone-gpt-gateway`，按无框架、无构建命令配置。静态输出目录由 `edgeone.json` 指定为 `public`。
2. 选择「全球可用区（不含中国大陆）」并绑定自定义域名。项目配置把云函数部署在新加坡（`ap-singapore`），入口是 `POST /v1/chat/completions`。[Cloud Functions](https://pages.edgeone.ai/zh/document/cloud-functions) · [Node.js 路由与上下文](https://pages.edgeone.ai/zh/document/node-functions)
3. 在 Makers 生产环境变量中设置下表的值，再部署。

| 变量 | 值 |
| --- | --- |
| `OPENAI_API_KEY` | 调用 `gpt-6.1-sol` 的 OpenAI API Key，仅设置在 EdgeOne |
| `MAHJONG_GATEWAY_TOKEN` | 后端专用随机令牌，可用 `openssl rand -hex 32` 生成 |
| `MAHJONG_BACKEND_IP` | 国内后端发出 HTTPS 请求时的固定公网出口 IP，填单个 IPv4 或 IPv6 地址 |

入口同时校验 Makers 提供的 `clientIp` 和 Bearer 令牌。若服务器经过 NAT，填写 NAT 的公网出口 IP。请求体上限为 256 KiB，云函数执行上限为 120 秒。

## 验证并切换后端

先从国内后端服务器测试。下例使用预先注入的 `MAHJONG_GATEWAY_TOKEN` 环境变量。

```bash
curl --no-buffer --fail-with-body \
  'https://<EdgeOne 自定义域名>/v1/chat/completions' \
  -H "Authorization: Bearer ${MAHJONG_GATEWAY_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{"model":"gpt-6.1-sol","messages":[{"role":"user","content":"Reply with OK."}],"reasoning_effort":"low","max_completion_tokens":512,"stream":true}'
```

成功时应看到逐段返回的 `data:`、结尾的 `usage` 和 `[DONE]`。从同一服务器去掉 Bearer 后应返回 401。从其他公网出口用有效令牌重试，即使附带伪造的 IP 转发头，也应返回 403。验证通过后，在国内后端的 `.env` 更新现有 GPT 槽位。

```dotenv
LLM_PROVIDER_RELAY_GPT_BASE_URL=https://<EdgeOne 自定义域名>/v1
LLM_PROVIDER_RELAY_GPT_API_KEY=<与 EdgeOne 的 MAHJONG_GATEWAY_TOKEN 相同>
LLM_PROVIDER_RELAY_GPT_MODEL=gpt-6.1-sol
LLM_PROVIDER_RELAY_GPT_TYPE=openai
```

使用 Docker Compose 时，应用新环境变量需重建后端容器（`docker compose up -d --force-recreate backend`）。在房间里选现有 GPT 席位即可使用新入口。查看后端「LLM 上游用量」日志（`llm_event=usage`），确认输入、缓存和输出 token。

本地可在本目录运行 `node --test` 验证入口。测试使用模拟 OpenAI 响应。
