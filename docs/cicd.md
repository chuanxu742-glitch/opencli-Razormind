# Fork CI/CD

## 合并代码

向 fork 提交 PR，等待 `CI Gate` 成功后合并。该检查汇总扩展、前端、后端、工作流契约、PostgreSQL 迁移、Rust 和整栈恢复验证；任何依赖失败、取消或跳过都不能通过。

`main` 的仓库设置要求 PR、分支与目标保持最新以及 GitHub Actions 提供的 `CI Gate`，管理员同样受约束，禁止强推和删除。单维护者仓库不要求第二位人工批准，但必须解决 review conversation。仓库设置独立于 Git 文件，其他 fork 需要由其管理员单独设置。

账号运行时 smoke 按文件路径触发，检查 Linux 镜像、进程关闭和双容器隔离。它不作为所有 PR 必须出现的检查，避免无关改动被缺席检查阻塞。

同一事件和分支上的重复 CI 可以取消旧运行；不同 PR 的验证互不取消。

## 发布镜像

1. 将准备发布的提交经 PR 合入 `main`。
2. 等待该提交的最新 `main` push CI 和 `CI Gate` 均成功。
3. 确认 `.env.docker.example` 的 `IMAGE_TAG` 与准备创建的 `v*` 标签版本一致。
4. 经维护者确认后，将发布标签指向该提交并推送到 fork。

发布流程先核对精确提交的 CI，再构建候选镜像，执行镜像和 Docker 重启恢复验证，最后推广版本及 `latest` 标签并创建 GitHub Release。CI 缺失、失败、进行中或 GitHub API 查询失败会阻止发布；完成 CI 后可重跑发布。

发布使用仓库所有者的 GHCR 命名空间。发布工作流串行执行且不取消正在执行的发布；GitHub concurrency 默认只保留一个等待运行，不应一次批量推送多个发布标签。

这些流程没有配置自动部署到生产服务器。镜像 smoke 使用受控测试场景，不能替代真实平台账号登录、授权和生产环境验收。

## 本地检查

```text
python -m pytest tests/unit/test_ci_workflow_contract.py --no-cov -q
node --test .github/scripts/require-release-ci.test.cjs
git diff --check
```

本地契约测试不发布镜像、不创建标签。完整前端、后端、PostgreSQL 和容器验证由 PR 的 GitHub Actions 执行。
