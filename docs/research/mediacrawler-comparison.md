# MediaCrawler 对照与采集稳定性改进

核对日期：2026-09-08。参考仓库：[NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)，固定参考版本为 `d6f7c5bb906b6dac40ddf343ef9e26438a3de092`。本地对照基于 `ad61fde0c6912525adcb5bd14525056d7b9be051` 及当前工作区；账号模块包含尚未提交的实现，不能据此宣称公开发布版已支持相同行为。

MediaCrawler 最值得借鉴的是平台任务表达、采集节奏和业务错误分类。OpenCLI 已有从采集到证据、工作流和交付的分层，这些能力适合在既有 Channel/Runner 中完善。上游使用非商业学习许可证，本轮独立实现通用 HTTP 行为，没有引入其代码或运行时依赖。参见[上游许可证](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/LICENSE)及本项目[参考能力独立实现原则](../adr/0013-reimplement-reference-capabilities-without-copying-restricted-code.md)。

## 能力对照

| 关注点 | MediaCrawler 的可核对行为 | 当前项目与适用结论 |
| --- | --- | --- |
| 平台任务表达 | 显式区分搜索、详情、创作者，并单独控制评论数量及子评论。[配置](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/config/base_config.py) | 当前通过 OpenCLI 的站点/命令目录暴露适配器。可在现有节点投影上增加经过验证的任务模板；模板只能展示适配器真实声明的能力。 |
| 职责分离 | 提供爬虫、登录、API 客户端及内容/评论/创作者存储抽象。[接口](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/base/base_crawler.py) | 已有 `AbstractChannel`、`FetchContext`、Runner、Normalizer、Sink，继续复用即可。 |
| 采集节奏 | 用并发上限、抓取数量和页间等待约束采集。[小红书采集器](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/media_platform/xhs/core.py) | 已有 HTTP 令牌桶、指数退避和进程内域名并发限制。本轮补齐等待时间与频率参数的边界处理。 |
| 登录与错误 | 采集前检查会话；客户端对不存在的内容和平台访问限制使用独立错误，并排除其中部分错误的自动重试。[客户端](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/media_platform/xhs/client.py) | 已有账号租约、登录观察和 `error_taxonomy`；验证码也有独立状态。后续应把具体平台错误可靠映射到现有状态，避免将页面加载成功视为登录成功。 |
| 增量与恢复 | 开源配置提供起始页；README 将断点续爬及多账号池列在 Pro 升级介绍中。[README](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/README.md) | 本项目已有游标持久化与落盘后提交。不能把 Pro 描述当作本次已审查的开源实现，也不能把 OpenCLI 的所有命令都标成支持续采。 |

本地对应入口：[Channel 契约](../../backend/channels/base.py)、[OpenCLI 适配器](../../backend/channels/opencli_channel.py)、[节点投影](../../backend/workflow/opencli_adapter_nodes.py)、[共享 Runner](../../backend/pipeline/channel_runner.py)、[域名并发限制](../../backend/pipeline/domain_limiter.py)、[错误分类](../../backend/pipeline/error_taxonomy.py)。

## 本轮改进的行为

[共享 HTTP 客户端](../../backend/pipeline/http_client.py)负责这些通用策略，因此使用 Runner `ctx.http` 的 RSS、REST API 和网页采集路径可共同受益：

- `Retry-After` 支持秒数和 HTTP 日期。未来日期转换成等待秒数，过去日期按零处理；历史日期格式中的无时区日期按 UTC 解释。依据 [RFC 9110 §10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after)。
- 负数、非有限数和畸形日期回退到已有指数退避及抖动，保留现有小数秒兼容性。
- 无效频率字符串使用已有默认值 `1.0/s`；缺失或未知单位、非正数、非有限数及计算下溢都不会进入令牌桶。
- 直接构造令牌桶时，速率必须是有限正数，容量必须是至少 `1` 的有限数；错误立即报告，避免运行到等待环节才暴露问题。合法极小正速率仍沿用既有 `1e-6/s` 下限，避免等待时间计算溢出。

重试状态集合、重试预算和永久 HTTP 错误的返回方式保持原有契约。日期等待依赖主机时钟；服务端给出的有效长等待仍被尊重。这一改进不会自动给浏览器子进程内的请求添加 HTTP 限流，域名并发上限也仍只覆盖单个进程。

## 保留的恢复边界

[Collector](../../backend/pipeline/collector.py)把运行中的游标放入暂存存储，由 [Pipeline](../../backend/pipeline/pipeline.py)在结果被存储端接受后提交。不能直接根据 Runner 中的 `store.save()` 推断生产路径提前推进数据库游标。

[OpenCLI Channel](../../backend/channels/opencli_channel.py)目前不声明通用分页/增量能力，因为不同站点命令没有统一的游标协议。浏览器登录态恢复与内容分页续采是两个不同保证；新增评论或创作者模板时，需要同时明确内容原生 ID、父子评论关系、停止条件和游标语义。

## 后续能力评估顺序

优先评估“站点 + 任务类型”的模板投影，使搜索、详情、评论和创作者任务更容易配置；其次评估平台错误到账号状态的映射。只有适配器明确提供可恢复游标时，才适合扩展增量采集。跨工作进程的限流则需要独立设计共享协调机制。这些是对照建议，本轮尚未实现，也不代表平台能力已通过真实账号验收。

## 验证入口

[HTTP 回归测试](../../tests/unit/pipeline/test_http_client.py)使用固定时钟与替代等待函数验证解析、实际重试顺序及预算，避免真实网络和长时间等待。

```powershell
uv run --no-sync pytest tests/unit/pipeline/test_http_client.py --no-cov -q
uv run --no-sync ruff check backend/pipeline/http_client.py tests/unit/pipeline/test_http_client.py
```

2026-09-08 验证结果：HTTP 专项 `49 passed`；包含 HTTP、Runner、Collector、游标提交、域名并发、RSS、API、网页及 OpenCLI 通道的相关回归共 `241 passed`（排除 live 测试）。两文件 Ruff、差异空白检查和独立审查通过。测试输出仍包含依赖弃用及模拟协程警告；本轮没有执行第三方平台的真实账号采集。
