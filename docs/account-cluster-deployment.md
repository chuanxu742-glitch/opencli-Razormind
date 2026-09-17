# 账户浏览器运行时集群部署

`docker-compose.account-cluster.yml` 为每个**同时在线的账户运行栈**提供独立容器、Docker 网络命名空间、稳定节点 ID、Chromium profile volume 与 `RUNTIME_STATE_DIR` volume。一个节点网络命名空间同一时间只运行一个账户栈；控制面中的未分配、休眠账户清单不需要也不应各自常驻一个容器。OpenCLI 1.8.7 daemon 固定在容器内 `127.0.0.1:19825`，Browser Bridge 扩展不提供多路身份；因此不得以 `--scale` 扩展一个服务、不得共享 profile/state volume，也不得通过复用 daemon/CDP/noVNC 端口承载多个同时在线账户。

此配方使用当前 checkout 的 `agent/Dockerfile` 与 `agent/entrypoint.sh` managed account allocator。入口脚本仅在 Chromium、`AGENT_NODE_ID`、`AGENT_NODE_CREDENTIAL_ID`、`AGENT_NODE_CREDENTIAL` 与 `BROWSER_RUNTIME_BUNDLE_ID` 都存在时启用 allocator；该条件不证明控制面已接受凭据，也不证明网站账户已登录。

## 前置条件

- 本地构建组合使用 `!override` 保留原命名卷并替换镜像内挂载位置，要求 Docker Compose 2.24.4 或更高版本。构建版固定使用 `/home/agent` 和 `/var/lib/opencli/account-runtime`；旧 Chrome 镜像的目录环境变量不改变构建版的卷位置。

- 在同一 Docker Engine 上已有控制平面，并创建其可达的 Docker network；该 network 名称填入 `ACCOUNT_CLUSTER_CONTROL_NETWORK`。Compose 的 external network 不会创建它，避免意外把账户节点接到错误控制面。
- 必须从**当前 checkout** 构建包含 Chromium 的 agent image，并使用 `--iidfile` 产生的不可变本地 image ID。配方设置 `pull_policy: never` 且没有上游 release fallback，因而缺少该镜像时会失败而不是静默运行旧 allocator。
- 为每个同时在线节点分配**不同且稳定**的 node ID、credential ID 和随机 credential，并选择控制面已注册的 runtime bundle ID。节点重建时恢复同一组 ID 与同一组持久卷；休眠账户保留在控制面清单中，只有获得独立节点容量后才上线。
- 为每个节点准备已有外部 TLS 代理的独立 HTTPS 来源，代理转发到该节点容器内 `http://account-node-N:19823` 并支持 WebSocket；本配方不部署 TLS sidecar。`ACCOUNT_NODE_N_ADVERTISE_URL` 必填且必须是该 HTTPS 来源，不能直接广播容器内 HTTP 地址。证书必须能被实际访问端验证，使用私有 CA 时将 CA 纳入相关容器的信任链，不关闭证书校验。
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
ACCOUNT_NODE_1_ADVERTISE_URL=https://account-node-1.example.com
ACCOUNT_NODE_1_ID=browser-account-001
ACCOUNT_NODE_1_CREDENTIAL_ID=browser-account-001-credential
ACCOUNT_NODE_1_CREDENTIAL=replace-with-node-1-secret
ACCOUNT_NODE_1_BROWSER_RUNTIME_BUNDLE_ID=opencli-default
ACCOUNT_NODE_2_ADVERTISE_URL=https://account-node-2.example.com
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

预检在内存中运行 `docker compose config --format json`，检查解析后的实际 node/credential ID、独立节点秘密、HTTPS 节点入口、long-syntax network/mount、重复 mount target、named-volume 底层别名、不可变镜像和 HTTPS 控制面 URL。它只输出通过/失败状态，不输出解析后的环境或密钥。不要把环境文件或 `.account-cluster-agent.iid` 提交到版本库。

Compose 不发布 host ports：19823 agent API、19825 OpenCLI daemon、9222 CDP 和 6080 noVNC 都只在各自容器/网络内可达。管理访问应经控制面身份验证路径完成，不要为排障临时公开 CDP、noVNC 或 daemon。

## 增加或减少独立节点

仓库提供两个节点示例；预检接受至少一个、任意数量的 `account-node-N` 服务，N 为不带前导零的正整数，可以不连续。不得使用 `--scale` 或共用节点身份扩容。

1. 复制一份未跟踪的部署配方，例如 `account-cluster.local.yml`。新增 `account-node-3` 时，复制节点服务后将 hostname 改为 `account-node-3`，将全部 `ACCOUNT_NODE_2_` 变量改为 `ACCOUNT_NODE_3_`，并把两份卷名改为 `account_node_3_profile`、`account_node_3_runtime_state`，同时在顶层 volumes 中声明。不要更改已有节点的身份和卷。
2. 在部署环境中注入新节点独立的 ID、credential ID、随机 credential、HTTPS advertised URL 及 bundle ID。当前配方使用 `/opt/browser-runtime-bundles/opencli-default/4/manifest.json`；中心注册的 bundle 版本必须与实际镜像相符。新节点不能借用旧节点的卷。
3. 为新节点准备外部 TLS 代理路由。每个 advertised URL 使用不同的主机/端口来源，不使用 URL 路径复用节点；保持节点容器无 host port 映射。共享控制网络只提供可达性，不等同于防火墙隔离。
4. 用相同部署环境预检，再仅启动新服务，避免重建已经在线的账号：

```sh
uv run python scripts/validate_account_cluster_deployment.py --compose-file account-cluster.local.yml
docker compose -f account-cluster.local.yml up -d --no-deps account-node-3
```

只需要一个节点时，从私有配方移除另一个服务及不再需要的卷声明。已经运行的节点应先通过账号流程关闭浏览器并停止该节点，再调整部署；不要执行 `down -v` 删除账号持久数据。环境变量须由部署工具注入到上述两个命令的同一环境；不要手动打印 `docker compose config` 的完整输出。

当前注册机制使用 fleet bearer 认证反向 WebSocket，检查 node ID、boot ID、credential ID 与消息一致，以及节点 credential 非空。控制面没有独立节点秘密的签发/比对存储 API；节点 credential 在节点侧校验。预检要求每节点秘密不同，但不能证明已实现独立的中心节点身份认证。应限制 fleet token 的持有者和控制网络访问，不把 credential ID 当成独立认证凭据。

## Readiness 与认证

账号页面需要 HTTPS。门户使用 Secure、HttpOnly Cookie 和同源 WebSocket；普通 HTTP 开发页面不能接收二维码投影。若前端代理改写了 Host，在后端设置 `BROWSER_PORTAL_PUBLIC_ORIGIN` 为浏览器实际访问的完整来源，例如 `https://admin.example.com`（不含路径）。来源必须精确匹配，不从 `X-Forwarded-Host` 自动推断，也不支持通配符。

在反向代理终止 TLS 时，代理应覆盖 `X-Forwarded-Proto: https`，后端只信任实际代理的来源 IP；不要将受信代理配置为通配符。门户的精确 WebSocket 路径由专用 owner Cookie、来源、成员权限和会话校验认证，HTTP 签发与兑换仍要求控制台身份。短期画面路由正常到期后，页面重新申请授权；权限拒绝、异常断开或会话撤销不会自动重试。

节点通过已认证的反向 WebSocket 定期上报当前 boot 的容量及镜像内实际安装的登录规则；过期容量不能分配新会话。控制面在启动时运行 durable command dispatcher，登录启动确认后持续续租，关闭和保存沿用同一个租约，即使单槽节点已满也能执行。只有节点确认浏览器已停止后才释放槽位。

`opencli-default@4` 包含小红书、抖音和哔哩哔哩的官方扫码入口及固定身份接口探针。`identity_probe_supported` 表示实现了身份验证能力，不代表某个账号已登录；只有该会话实时返回的有效身份证据才能触发停止和 profile 保存。二维码消失、HTTP 成功、导航变化、昵称和人工确认都不等于登录成功。旧包账号不会自动升级或迁移已有 profile。

平台目录以当前安装的 OpenCLI 1.8.7 为依据：176 个适配平台中，66 个有固定官方浏览器入口，ONES 需要企业部署地址。目录区分三个扫码平台、63 个官方网页登录平台，以及 API、本地应用和缺少受管入口的适配器。后 63 个入口可在同一隔离会话内交互，但尚未接入可信身份探针，不能自动确认登录或提交已验证 profile；不能将目录覆盖数量当作已验证登录数量。目录位于 `backend/browser_login_catalog.json`，镜像和扩展内使用相同副本，官方规则通过固定目录哈希校验。

三个扫码规则仅在可见过期提示出现、未扫码、未进入二次验证且租约仍有效时申请换码；刷新前在同一标签页重新校验文档、视图与二维码版本。每个会话最多尝试三次，至少间隔 30 秒，导航不重置预算。耗尽后需关闭并重新打开登录会话。画面授权续签与二维码换码是两个独立过程。

扫码画面可通过“需要其他验证”切换为官方页面交互；平台验证码出现时也会进入交互状态。用户可点击、手动拖动滑块，或在明确聚焦的官方输入框中发送一次性文本。输入仅走临时二进制门户传输，画面中的输入框被遮罩；目标、文档、视图或焦点变化后旧输入被拒绝。鼠标拖动中断时释放原目标的按下状态。短信码、滑块和 App 确认仍由账号持有人完成。

`account-node-1` 和 `account-node-2` 的 healthcheck 只请求容器内 agent 的 `/health`；成功意味着 agent 进程可响应。它**不**表示：

- 节点 credential 已被控制面接受或 WebSocket 注册已完成；
- runtime bundle ID 与控制面分配一致；
- Chrome 扩展/用户脚本 self-check 已完成；
- 外部站点 session 已登录、未过期或拥有预期授权；
- TLS、控制面鉴权或底层存储加密已通过生产验收。

启动后，先在控制面确认各个不同 node ID 的注册与 bundle assignment，再为每个节点运行一次经授权的账户操作并保存脱敏审计结果。任何认证失败都应作为 credential、站点 session 或授权问题处置，不能通过复制 profile/state volume 到另一节点规避。

## 备份、恢复与轮换

停止目标节点后，作为一个原子单元备份它的两个 named volumes：`account_node_N_profile` 与 `account_node_N_runtime_state`。备份须加密、最小权限保存，并记录 image ID/digest、node ID、runtime bundle ID、备份时间和校验和；不得将 volume 内容写入工作流导出、日志或 issue 附件。

恢复时，先隔离旧容器，恢复到同名节点的两份卷，再以原 node ID、credential ID 和 bundle ID 启动。不要同时运行旧副本与恢复副本：profile 的 Chromium singleton lock 和同一节点身份都会造成不确定状态。节点 credential 轮换由部署者生成新随机秘密及 credential ID，更新单个节点环境并滚动重建该节点；这不是控制面独立凭据签发或吊销流程。不要复制另一个账户的卷。

## 生产验收边界

本地可运行 resolved Compose 预检与 focused pytest；它们验证配方隔离、无 host port、独立底层卷、解析后身份、不可变当前-checkout 镜像引用和 HTTPS URL 契约。它们不能证明真实容器、TLS 终端、控制面认证、站点授权或存储加密。

仓库的 `account-runtime-smoke` GitHub Actions 工作流会在 Linux 上构建当前 checkout 的 agent 镜像，运行两个隔离容器，检查真实 Chromium/CDP、OpenCLI 1.8.7 扩展连接、固定端口容量拒绝和停止后的进程及端口释放。它使用合成控制契约和受控登录站点，不证明控制面鉴权、真实平台登录或生产持久卷恢复。可从 Actions 手动运行候选分支，并保存对应提交与运行结果。

生产候选部署还须核对 image ID、创建 external network、启动两节点、验证 TLS 与控制面认证注册、验证每个在线账户的真实授权操作、核验底层卷加密，以及停止/恢复后的状态保留。Windows 本机 Docker Engine 不可用时，可使用上述 Linux 验证补充运行时证据，但不能据此跳过部署验收。


## 账号创建与登录排队

官方平台账号的保存不分配浏览器进程，也不要求当前存在空闲槽位。存在已部署节点时优先绑定空闲节点；尚无节点时先保存未绑定账号，首次登录在账号行锁内匹配实际部署的规则和节点。已有 profile 或 manifest 的账号不走自动重新绑定。

满载节点上的登录请求进入持久化命令队列。调度器仅在容量与租约验证通过后启动浏览器；页面显示等待调度并允许取消。取消尚未领取的请求直接终止排队，不额外分配浏览器去执行关闭。离线、过期心跳、未部署规则等仍会阻止启动。

运行容量按独立容器节点扩展，账号总数不等于常驻浏览器进程数。登录保存仍以官方身份验证及持久化结果为准；未经身份校验的平台不能因排队或打开窗口而标记为已认证。

## 按需 Docker 节点池（可选）

控制面设置 `ACCOUNT_NODE_POOL_CONFIG` 为本机受控 JSON 文件绝对路径后，会运行单实例池管理器。未设置时保持现有部署行为。这个管理器调用宿主机 Docker CLI；账号容器不挂载 Docker socket。运行管理器的账号必须有对应 Docker 权限。

配置字段：`docker`（CLI 绝对路径）、`image`（不可变 `sha256:` image ID）、`network`、`state_dir`（私有持久目录）、`env_template`（受控节点环境模板路径）、`trust_file`、`ca_file`、`advertise_base`（TLS代理入口）、`bundle_id`、`max_running`、`idle_seconds`。所有路径必须为绝对路径。环境模板包含fleet认证资料，不得提交版本库。`state_dir` 保存节点身份记录及每节点独立凭据，应仅允许运行管理器的账号访问。

- 有真实未启动的排队请求且池未满时，每轮最多创建一个隔离容器，等待新boot及真实能力报告后才分配任务。
- `max_running` 是弹性池同时运行的上限（1–32），不是固定常驻数量；既有手工节点不计入这个池。当前单容器限制2GiB内存、2 CPU、512进程，调整上限时需预留宿主资源。
- 只有从未取得过租约、无profile/manifest/已建立浏览器目标的新账号请求可转派。已有资料或历史租约账号保留原节点。
- 空闲达到 `idle_seconds`（30秒–24小时）后自动停止池自建容器。过期容量、未释放租约、未完成会话及待执行命令都会阻止回收。只停止容器，不删除卷或复制账号资料。
- 再次登录自动启动同一容器，保留node ID和数据卷，等待新的boot注册后再放行调度。
- 固定容器名称为 `opencli-account-pool-<32位随机hex>`；代理需将 `advertise_base/<hex>/...` 转发到该容器的19823端口。仅允许这段严格格式，不允许任意上游地址；所有控制面/节点访问仍验证TLS。

池管理器采用本机文件锁避免重复创建；持久化的stopping/stopped记录同时作为调度停止保护。**此本机模式要求所有调度进程使用同一个配置和同一个state目录**。不要用于读取不到共享停止保护的多控制面部署；分布式集群应另行实现数据库内的统一池租约。停机失败会保留保护并重试，不会把数据库租约过期当作浏览器已停止的证据。

验收应包含真实Docker创建、规则及登录页可用、空闲自动停止，以及保留同一容器ID/卷的再次唤醒；容器启动成功不代表平台登录或身份保存已经成功。

## 在账号集群中打开浏览器

账号行的“打开浏览器”在 Windows 桌面启动独立窗口，显示该账号已有的 Docker Chromium。项目网页负责管理账号；账号 profile、Cookie、登录状态和自动化仍留在原节点。资源休眠时沿用原节点与数据卷唤醒；容量不足时排队。已有扫码会话可原地提升为浏览器会话，不另建 profile；执行中或正在保存的实例不能被接管。“更多”中的“扫码登录”继续使用原二维码门户。

- 同一账号重复点击会复用原窗口；查看账号详情、刷新或关闭项目网页不会重新创建窗口或关闭已有窗口。
- 关闭独立桌面窗口后自动请求停止并保存浏览器数据；网页中的“关闭浏览器并保存”也可执行该操作。单次会话最长30分钟，到期也走保存流程。
- 浏览器会话通过可信身份验证后继续保持打开。未验证账号也可保存浏览器环境，但保存环境不等于平台身份已验证，不会因此开放认证执行权限。

独立窗口使用 [TigerVNC 原生查看器](https://tigervnc.org/doc/vncviewer.html)，需要 API 运行在用户已登录的 Windows 桌面会话中。该模式默认关闭，不用于替远程访问者打开服务器上的窗口。先运行 `scripts/install-browser-viewer.ps1`，它从官方发布页下载固定版本并校验 SHA-256 和 Authenticode 签名，输出可执行文件路径；也可用 `-DestinationDirectory` 指定目录。将该绝对路径设为 API 的 `BROWSER_NATIVE_VIEWER_EXECUTABLE`，然后重启 API。未配置时页面会显示原因，不自动创建浏览器会话。

每个运行中的账号有独立的显示、profile与动态VNC端口；容器VNC只监听其loopback，不能映射到宿主机。API通过已有TLS节点WebSocket接入该session，再建立只监听Windows本机loopback的一次性认证桥。原生窗口使用临时VNC口令，口令不进入URL、命令行、日志或浏览器存储；持续校验工作区权限和节点租约。查看器禁止剪贴板同步，关闭后清理连接、端口和进程。动态节点镜像配置应使用已验证的不可变image ID，并用`--init`回收Chromium退出后的子进程。

隔离边界是账号的浏览器数据、进程栈、端口、会话与租约。同一节点中的实例仍共享容器及Linux用户，不能把它当作不可信多租户的操作系统文件沙箱。完整桌面应交给受信任的工作区操作成员；需要抵御同节点恶意操作者时，应另设容器或操作系统隔离边界。

镜像强制禁止普通本地文件导航、文件选择器和文件系统API，并剥离浏览器子进程环境中的控制面凭据。为兼容现有CDP自动化，`DeveloperToolsAvailability`使用0；值2会同时禁止远程目标调试。上述浏览器策略不构成对DevTools所有本地资源操作的操作系统级限制。CDP不接受任意网页Origin，页面与节点地址、凭据均不应作为桌面连接参数公开。
