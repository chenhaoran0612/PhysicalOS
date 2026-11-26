// Shared by the browser acceptance tests since issue #49: 「工程助手」(with 协作流程), 「助手工具」 and
// 「自有服务」 are hidden for now (web/hidden_pages.js). The pages and what they do are still in the
// product, so the scripts that check them keep running with the pages in sight: one line after the page
// is created,
//   require('./hidden_pages_helper.cjs').showHiddenPages(page);
// Every document that page loads afterwards has the hidden pages back (the console checks
// window.__showHiddenPages once, when it loads). A page that is already open needs a reload:
//   await showHiddenPages(page); await page.reload();
// tests/hidden_pages_browser.cjs checks the console as people get it, without this.
async function showHiddenPages(page) {
  await page.addInitScript(() => { window.__showHiddenPages = true; });
}

module.exports = {showHiddenPages};
