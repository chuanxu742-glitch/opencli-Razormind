chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (
    message?.type !== "opencli-script-host.invoke" ||
    message.pack !== "page-basics" ||
    message.version !== "1.0.0"
  ) {
    return false;
  }
  if (message.action !== "page.metadata") {
    sendResponse({ ok: false, error: "unknown page-basics action" });
    return false;
  }
  sendResponse({
    ok: true,
    metadata: {
      url: window.location.href,
      title: document.title,
      language: document.documentElement.lang || null,
    },
  });
  return false;
});
