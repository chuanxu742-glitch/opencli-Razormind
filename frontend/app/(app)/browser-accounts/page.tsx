'use client'

import { BrowserAccountsPanel } from '@/components/browsers/browser-accounts-panel'
import { PageContainer } from '@/components/shell/page-container'
import { RouteTabs, COMPUTE_TABS } from '@/components/shell/route-tabs'

export default function BrowserAccountsPage() {
  return (
    <PageContainer
      title="账号集群"
      description="管理账号，在独立桌面窗口中打开各自的浏览器，继续使用已保存的登录环境。"
      tabs={<RouteTabs tabs={COMPUTE_TABS} />}
    >
      <BrowserAccountsPanel />
    </PageContainer>
  )
}
