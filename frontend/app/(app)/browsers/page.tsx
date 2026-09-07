"use client";

import { BrowserActPacksPanel } from "@/components/browsers/browser-act-packs-panel";
import { BrowserRuntimeBundlesPanel } from "@/components/browsers/browser-runtime-bundles-panel";
import { BrowserSpacesPanel } from "@/components/browsers/browser-spaces-panel";
import { ChromeInstancesPanel } from "@/components/browsers/chrome-instances-panel";
import { PageContainer } from "@/components/shell/page-container";
import { COMPUTE_TABS, RouteTabs } from "@/components/shell/route-tabs";

export default function BrowsersPage() {
  return (
    <PageContainer
      title="Chrome 池与站点绑定"
      description="管理本机 Docker 采集池、远程 Agent 路由与按站点的浏览器绑定；下方还提供随包动作预设的只读目录。"
      tabs={<RouteTabs tabs={COMPUTE_TABS} />}
    >
      <BrowserRuntimeBundlesPanel />
      <ChromeInstancesPanel />
      <BrowserSpacesPanel />
      <BrowserActPacksPanel />
    </PageContainer>
  );
}
