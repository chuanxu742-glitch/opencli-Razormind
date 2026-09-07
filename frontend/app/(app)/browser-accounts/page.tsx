'use client'

import { BrowserAccountsPanel } from '@/components/browsers/browser-accounts-panel'
import { PageContainer } from '@/components/shell/page-container'
import { COMPUTE_TABS, RouteTabs } from '@/components/shell/route-tabs'

export default function BrowserAccountsPage() {
  return (
    <PageContainer
      title="Browser accounts"
      description="Manage workspace-scoped account identity, verified login evidence, and same-session recovery. Credentials and transient portal input never enter the account record."
      tabs={<RouteTabs tabs={COMPUTE_TABS} />}
    >
      <BrowserAccountsPanel />
    </PageContainer>
  )
}
