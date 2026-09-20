// The toolbar button opens the side panel; nothing else runs in the background. The extension is a
// protocol client and holds no browser automation of its own: the environment runs beside Chrome
// and reaches it through Chrome's agent connection.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});
