# 2026-11-02 TTS 503 恢复记录

## 原因与修复

公网 `POST /api/local-tts/synthesize` 返回 HTTP 503，错误码为
`LOCAL_TTS_UNAVAILABLE`。该错误发生在调用供应商之前，表示本地网关被禁用或
没有可用的供应商凭据；供应商合成失败使用的是 `LOCAL_TTS_SYNTHESIS_FAILED`。

生产容器 `st-preview-python` 将项目 `config/` 只读挂载到 `/app/config`，但宿主
目录为空，缺少 `tts.yml`。`load_tts_config()` 在 YAML 不存在时按默认配置关闭
TTS，因此重建镜像无法解决该问题。

已从同机迁移归档
`/opt/server-transfer/predeploy-20261024/full-rootfs/opt/python-project/lianhua-mahjong-backend/config/`
恢复原始 `tts.yml` 和引用的 `secrets/`，未上传本地凭据。恢复前备份位于
`/var/backups/tts-repair-20261102T132457Z/`，包含原配置目录、Compose 和挂载信息。
运行配置与凭据继续保持 Git 忽略，不写入镜像。

恢复后先独立调用火山合成成功，再确认后端 8000 端口没有已建立连接，仅重启
`st-preview-python`。其他运行容器 ID 前后一致。

## 部署与复查

当前生产项目目录是 `/opt/python-project/lianhua-mahjong-backend`。使用该目录的
现有 Compose 和数据链接，不用仓库默认 Compose 覆盖服务器网络与数据挂载。

更新或迁移前，确认 `config/tts.yml` 及其引用的凭据文件均已带到目标目录。
只创建空 `config/` 不够；这些文件被 Git 和 Docker 构建上下文排除，需要单独保留。

更新前可复用当前镜像和 Compose 挂载检查配置并真实合成：

```sh
cd /opt/python-project/lianhua-mahjong-backend
test -s config/tts.yml
sudo docker compose run --rm --no-deps backend python -m app.local_tts.check
```

该检查会请求一条测试语音，不输出密钥。已有容器可使用：

```sh
sudo docker exec st-preview-python python -m app.local_tts.check
```

进程启动时缓存服务配置；补回 YAML 后仍须在合适时机重启后端进程才能生效。
重启前检查活跃连接或安排维护窗口，避免中断牌局。

不要把含凭据的 YAML、凭据文件、完整环境变量或容器配置输出提交到 Git。

## 验证结果

- 后端现有 TTS 回归测试：20 项通过。
- 恢复配置后，火山真实合成成功，启动日志显示两个供应商均可用。
- 公网 `https://www.bestguo.top:58000/api/local-tts/synthesize` 首次合成 HTTP 200，
  `cached=false`；重复请求 HTTP 200，`cached=true`。
- 返回的 MP3 地址下载 HTTP 200，`audio/mpeg`，20,013 字节，MP3 文件头有效。
- 原前端 origin 的 CORS 响应正确，音频返回不可变缓存头。
