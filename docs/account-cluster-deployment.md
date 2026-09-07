# 账户浏览器运行时集群部署

`docker-compose.account-cluster.yml` 为每个**同时在线的账户运行栈**提供独立容器、Docker 网络命名空间、稳定节点 ID、Chromium profile volume 与 `RUNTIME_STATE_DIR` volume。一个节点网络命名空间同一时间只运行一个账户栈；控制面中的未分配、休眠账户清单不需要也不应各自常驻一个容器。OpenCLI 1.8.7 daemon 固定在容器内 `127.0.0.1:19825`，Browser Bridge 扩展不提供多路身份；因此不得以 `--scale` 扩展一个服务、不得共享 profile/state volume，也不得通过复用 daemon/CDP/noVNC 端口承载多个同时在线账户。

此配方使用当前 checkout 的 `agent/Dockerfile` 与 `agent/entrypoint.sh` managed account allocator。入口脚本仅在 Chromium、`AGENT_NODE_ID`、`AGENT_NODE_CREDENTIAL_ID`、`AGENT_NODE_CREDENTIAL` 与 `BROWSER_RUNTIME_BUNDLE_ID` 都存在时启用 allocator；该条件不证明控制面已接受凭据，也不证明网站账户已登录。

## 前置条件

- 在同一 Docker Engine 上已有控制平面，并创建其可达的 Docker network；该 network 名称填入 `ACCOUNT_CLUSTER_CONTROL_NETWORK`。Compose 的 external network 不会创建它，避免意外把账户节点接到错误控制面。
- 必须从**当前 checkout** 构建包含 Chromium 的 agent image，并使用 `--iidfile` 产生的不可变本地 image ID。配方设置 `pull_policy: never` 且没有上游 release fallback，因而缺少该镜像时会失败而不是静默运行旧 allocator。
- 为每个同时在线节点在控制面创建**不同且稳定**的 node ID、credential ID、credential 和已注册 runtime bundle ID。节点重建时恢复同一组 ID 与同一组持久卷；休眠账户保留在控制面清单中，只有获得独立节点容量后才上线。
- 生产控制面 URL 必须使用经验证的 HTTPS。上线证据至少包括：证书链与主机名验证结果、控制面鉴权策略、两个节点凭据成功注册的脱敏审计记录，以及各账户授权操作结果。本文不把明文 HTTP 凭据传输视为已验证生产路径。
- Docker named volumes 必须位于受访问控制的加密存储之上。profile 与 runtime state 可能包含站点会话材料；部署者必须保存底层卷驱动/磁盘加密配置、密钥托管与恢复演练证据。环境变量或配置中的布尔值不能证明静态数据已加密。

## 构建当前 checkout

在将要运行账户节点的 Docker Engine 上，从仓库根目录执行。`--iidfile` 写入内容寻址 image ID；不要提交该临时文件。

```sh
docker build --pull --file agent/Dockerfile --build-arg INSTALL_CHROME=true \
  --iidfile .account-cluster-agent.iid \
  --tag opencli-account-agent:"$(git rev-parse HEAD)" .
export ACCOUNT_CLUSTER_AGENT_IMAGE="$(cat .account-cluster-agent.iid)"
```

`ACCOUNT_CLUSTER_AGENT_IMAGE` 必须是 `sha256:<64 hex>` 的本地 image ID，或经发布流程验证的 `repository@sha256:<64 hex>` registry digest；浮动 tag 和 `0.4.1` fallback 均不满足预检。

## 两节点示例

将密钥放在受保护的部署 secret store 或部署主机的未跟踪环境文件中。以下变量名示例不含真实值：

```dotenv
ACCOUNT_CLUSTER_CONTROL_NETWORK=opencli_control
ACCOUNT_CLUSTER_AGENT_IMAGE=sha256:replace-with-64-hex-image-id
CENTRAL_API_URL=https://control.example.com
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

用部署工具注入这些变量，先运行不会输出解析后 secret-bearing Compose model 的预检，再启动：

```sh
python scripts/validate_account_cluster_deployment.py
docker compose -f docker-compose.account-cluster.yml up -d
```

预检在内存中运行 `docker compose config --format json`，检查解析后的实际 node/credential ID、long-syntax network/mount、重复 mount target、named-volume 底层别名、不可变镜像和 HTTPS 控制面 URL。它只输出通过/失败状态，不输出解析后的环境或密钥。不要把环境文件或 `.account-cluster-agent.iid` 提交到版本库。

Compose 不发布 host ports：19823 agent API、19825 OpenCLI daemon、9222 CDP 和 6080 noVNC 都只在各自容器/网络内可达。管理访问应经控制面身份验证路径完成，不要为排障临时公开 CDP、noVNC 或 daemon。

## Readiness 与认证

`account-node-1` 和 `account-node-2` 的 healthcheck 只请求容器内 agent 的 `/health`；成功意味着 agent 进程可响应。它**不**表示：

- 节点 credential 已被控制面接受或 WebSocket 注册已完成；
- runtime bundle ID 与控制面分配一致；
- Chrome 扩展/用户脚本 self-check 已完成；
- 外部站点 session 已登录、未过期或拥有预期授权；
- TLS、控制面鉴权或底层存储加密已通过生产验收。

启动后，先在控制面确认两个不同 node ID 的认证注册与 bundle assignment，再为每个节点运行一次经授权的账户操作并保存脱敏审计结果。任何认证失败都应作为 credential、站点 session 或授权问题处置，不能通过复制 profile/state volume 到另一节点规避。

## 备份、恢复与轮换

停止目标节点后，作为一个原子单元备份它的两个 named volumes：`account_node_N_profile` 与 `account_node_N_runtime_state`。备份须加密、最小权限保存，并记录 image ID/digest、node ID、runtime bundle ID、备份时间和校验和；不得将 volume 内容写入工作流导出、日志或 issue 附件。

恢复时，先隔离旧容器，恢复到同名节点的两份卷，再以原 node ID、credential ID 和 bundle ID 启动。不要同时运行旧副本与恢复副本：profile 的 Chromium singleton lock 和同一节点身份都会造成不确定状态。凭据轮换按控制面流程创建/激活新 credential，更新单个节点环境并滚动重建该节点；不要复制另一个账户的卷。

## 生产验收边界

本地可运行 resolved Compose 预检与 focused pytest；它们验证配方隔离、无 host port、独立底层卷、解析后身份、不可变当前-checkout 镜像引用和 HTTPS URL 契约。它们不能证明真实容器、TLS 终端、控制面认证、站点授权或存储加密。

当前 Windows 环境没有 Docker Engine，真实 Engine 验收仍须在 Linux 生产候选环境执行并留存证据：构建准确 checkout、核对 image ID、创建 external network、启动两节点、检查容器健康、验证 TLS 与控制面认证注册、验证每个在线账户的真实授权操作、核验底层卷加密，以及停止/恢复后的状态保留。
