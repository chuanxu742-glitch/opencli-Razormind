# 账户浏览器运行时集群部署

`docker-compose.account-cluster.yml` 为每个已管理浏览器账户创建一个独立容器、Docker 网络命名空间、稳定节点 ID、Chromium profile volume 与 `RUNTIME_STATE_DIR` volume。OpenCLI 1.8.7 daemon 固定在容器内 `127.0.0.1:19825`，Browser Bridge 扩展不提供多路身份；因此不得以 `--scale` 扩展一个服务、不得共享 profile/state volume，也不得通过复用 daemon/CDP/noVNC 端口承载多个账户。

此配方复用 `agent/Dockerfile` 已声明的 agent 镜像和 `agent/entrypoint.sh` 的 managed account allocator。它不增加运行时能力：入口脚本仅在 Chromium、`AGENT_NODE_ID`、`AGENT_NODE_CREDENTIAL_ID`、`AGENT_NODE_CREDENTIAL` 与 `BROWSER_RUNTIME_BUNDLE_ID` 都存在时启用 allocator；该条件不证明控制面已接受凭据，也不证明网站账户已登录。

## 前置条件

- 在同一 Docker Engine 上已有控制平面，并创建其可达的 Docker network；该 network 名称填入 `ACCOUNT_CLUSTER_CONTROL_NETWORK`。Compose 的 external network 不会创建它，避免意外把账户节点接到错误控制面。
- 使用已发布或已按本仓库现有构建方式生成的、含 Chromium 的 agent image。此配方不声称当前 Windows 主机或任意 Linux 主机已经通过真实容器启动验证。
- 为每个节点在控制面创建**不同且稳定**的 node ID、credential ID、credential 和已注册 runtime bundle ID。一个账户对应一个节点；节点重建时恢复同一组 ID 与同一组持久卷。
- Docker named volumes 必须位于受访问控制的加密存储之上。profile 与 runtime state 可能包含站点会话材料；它们不是可提交、可导出到工作流包、也不是可跨账户共享的数据。

## 两节点示例

将密钥放在受保护的部署 secret store 或部署主机的未跟踪环境文件中。以下变量名示例不含真实值：

```dotenv
ACCOUNT_CLUSTER_CONTROL_NETWORK=opencli_control
CENTRAL_API_URL=http://api:8000
API_AUTH_TOKEN=replace-with-control-plane-token
ACCOUNT_NODE_1_ID=browser-account-001
ACCOUNT_NODE_1_CREDENTIAL_ID=browser-account-001-credential
ACCOUNT_NODE_1_CREDENTIAL=replace-with-node-1-secret
ACCOUNT_NODE_1_BROWSER_RUNTIME_BUNDLE_ID=opencli-default
ACCOUNT_NODE_2_ID=browser-account-002
ACCOUNT_NODE_2_CREDENTIAL_ID=browser-account-002-credential
ACCOUNT_NODE_2_CREDENTIAL=replace-with-node-2-secret
ACCOUNT_NODE_2_BROWSER_RUNTIME_BUNDLE_ID=opencli-default
```

用部署工具注入这些变量，再启动：

```sh
docker compose -f docker-compose.account-cluster.yml up -d
```

不要把该环境文件提交到版本库。Compose 不发布 host ports：19823 agent API、19825 OpenCLI daemon、9222 CDP 和 6080 noVNC 都只在各自容器/网络内可达。管理访问应经控制面身份验证路径完成，不要为排障临时公开 CDP、noVNC 或 daemon。

## Readiness 与认证

`account-node-1` 和 `account-node-2` 的 healthcheck 只请求容器内 agent 的 `/health`；成功意味着 agent 进程可响应。它**不**表示：

- 节点 credential 已被控制面接受或 WebSocket 注册已完成；
- runtime bundle ID 与控制面分配一致；
- Chrome 扩展/用户脚本 self-check 已完成；
- 外部站点 session 已登录、未过期或拥有预期授权。

启动后，先在控制面确认两个不同 node ID 的注册与 bundle assignment，再为每个节点运行一次经授权的账户操作并保存审计结果。任何认证失败都应作为 credential、站点 session 或授权问题处置，不能通过复制 profile/state volume 到另一节点规避。

## 备份、恢复与轮换

停止目标节点后，作为一个原子单元备份它的两个 named volumes：`account_node_N_profile` 与 `account_node_N_runtime_state`。备份须加密、最小权限保存，并记录镜像 digest、node ID、runtime bundle ID、备份时间和校验和；不得将 volume 内容写入工作流导出、日志或 issue 附件。

恢复时，先隔离旧容器，恢复到同名节点的两份卷，再以原 node ID、credential ID 和 bundle ID 启动。不要同时运行旧副本与恢复副本：profile 的 Chromium singleton lock 和同一节点身份都会造成不确定状态。凭据轮换按控制面流程创建/激活新 credential，更新单个节点环境并滚动重建该节点；不要复制另一个账户的卷。

## 生产验收边界

静态验证可运行 `scripts/validate_account_cluster_deployment.py` 与 focused pytest；它们验证配方中的隔离、无 host port、独立卷和变量契约。当前环境 Docker Engine 不可用，故以下真实 Engine 验收未执行且仍是生产上线前必需项：拉取/构建准确镜像、`docker compose config`、创建 external network、启动两节点、检查容器健康、验证控制面注册、验证每个账户的真实认证操作，以及停止/恢复后的状态保留。