import { expect, test, type Locator, type Page } from "@playwright/test";

// Screenshot comparisons: each test renders one element against the fake-model server and
// compares it with a stored image in `__screenshots__`. They catch layout and styling drift
// the behaviour tests in chat.spec.ts cannot. Elements, not pages: the server keeps every
// earlier test's conversations, so a page would depend on test order. Times are pinned (`pinTimes`)
// and spend totals masked, since both change between runs. Refresh images on purpose with
// `task frontend:test:update`, then look at the diff before committing.

async function start(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "New conversation", exact: true }).click();
  await expect(page.getByPlaceholder("Ask argus about your home infrastructure…")).toBeVisible();
}

// Times change between runs, and so does their width, which moves whatever sits next to them:
// pin their text instead of masking them.
async function pinTimes(times: Locator) {
  await times.evaluateAll((elements) => elements.forEach((element) => { element.textContent = "12:00"; }));
}

async function send(page: Page, message: string) {
  const input = page.getByPlaceholder("Ask argus about your home infrastructure…");
  await input.fill(message);
  await input.press("Enter");
}

test("conversation row: idle, hovered, renaming, confirming deletion", async ({ page }) => {
  await start(page);
  await send(page, "Visual row check");
  await expect(page.getByText("Read-only review complete: Visual row check", { exact: true })).toBeVisible();
  const row = page.locator(".thread-row", { hasText: "Visual row check" });
  await pinTimes(row.locator("time"));
  await page.mouse.move(0, 0);
  await expect(row).toHaveScreenshot("row-idle.png");

  await row.hover();
  await expect(row).toHaveScreenshot("row-hover.png");

  // While renaming or confirming, the title is no longer the row's text.
  await row.getByRole("button", { name: "Rename conversation" }).click();
  const title = page.getByRole("textbox", { name: "Conversation title" });
  await expect(page.locator(".thread-row", { has: title })).toHaveScreenshot("row-renaming.png");
  await title.press("Escape");

  await row.getByRole("button", { name: "Delete conversation" }).click();
  const confirm = page.getByRole("group", { name: "Confirm deletion" });
  await expect(page.locator(".thread-row", { has: confirm })).toHaveScreenshot("row-confirm-delete.png");
});

test("tool call card, collapsed and expanded", async ({ page }) => {
  await start(page);
  await send(page, "What is the router uptime?");
  const card = page.locator(".tool-call").filter({ hasText: "ssh_run" });
  await expect(card.getByText("router $ uptime")).toBeVisible();
  await expect(card).toHaveScreenshot("tool-call.png");
  await card.getByRole("button").click();
  await expect(card.locator("pre").last()).toContainText("exit 0");
  await expect(card).toHaveScreenshot("tool-call-expanded.png");
});

test("failed run notice", async ({ page }) => {
  await start(page);
  // A question of its own: the fake model fails each such question once.
  await send(page, "Fail once for the screenshot");
  const notice = page.getByRole("alert", { name: "Failed run" });
  await expect(notice.getByRole("button", { name: "Resume" })).toBeVisible();
  await expect(notice).toHaveScreenshot("failed-run.png");
});

test("message from an external agent", async ({ page, request }) => {
  const seeded = await (await request.post("/__test__/external-message")).json();
  await page.goto(`/?thread=${seeded.id}`);
  const message = page.locator(".external-message").first();
  await expect(message).toContainText("Check the storage pool");
  await pinTimes(message.locator(".message-time"));
  await expect(message).toHaveScreenshot("external-message.png");
});

test("an exchange with its day divider and times", async ({ page }) => {
  await start(page);
  await send(page, "Visual exchange check");
  await expect(page.getByText("Read-only review complete: Visual exchange check", { exact: true })).toBeVisible();
  const list = page.getByTestId("copilot-message-list");
  await expect(list.locator(".message-time")).toHaveCount(2);
  await pinTimes(list.locator(".message-time"));
  await expect(list).toHaveScreenshot("exchange.png");
});

test("collapsed sidebar rail", async ({ page }) => {
  await start(page);
  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  const sidebar = page.getByRole("complementary", { name: "Conversations" });
  await expect(page.getByRole("navigation")).toBeHidden();
  await page.mouse.move(400, 400);
  await expect(sidebar).toHaveScreenshot("sidebar-collapsed.png", { mask: [sidebar.locator(".spent")] });
  await page.getByRole("button", { name: "Expand sidebar" }).click();  // the choice is stored
});
